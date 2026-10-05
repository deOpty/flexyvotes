"""OpenID Connect single sign-on (authorization code flow + PKCE).

Providers: Google and Microsoft (platform-level credentials from settings) and
per-organization institutional IdPs (Organization.sso_config). ID tokens are
verified against the provider's JWKS (signature, issuer, audience, expiry and
nonce) before any claim is trusted.
"""
import base64
import hashlib
import json
import time
from urllib.parse import urlencode, urlparse

import jwt
from django.conf import settings
from django.core.cache import cache

from . import crypto
from .http import safe_request

SESSION_KEY = 'fv_sso'
ALLOWED_ALGORITHMS = ['RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'PS256']


class SSOError(Exception):
    pass


def provider_config(provider_key, organization=None):
    if provider_key in settings.SSO_PROVIDERS:
        config = dict(settings.SSO_PROVIDERS[provider_key])
        if not config.get('client_id'):
            raise SSOError(f'{config["name"]} sign-in is not configured.')
        config['extra_hosts'] = []
        config['key'] = provider_key
        return config
    if provider_key.startswith('org-') and organization is not None:
        sso = organization.sso_config or {}
        if not (sso.get('issuer') and sso.get('client_id')):
            raise SSOError('Single sign-on is not configured for this organization.')
        host = urlparse(sso['issuer']).hostname
        return {'key': provider_key, 'name': sso.get('name') or organization.name, 'issuer': sso['issuer'].rstrip('/'),
                'client_id': sso['client_id'], 'client_secret': sso.get('client_secret', ''),
                'allowed_domains': sso.get('allowed_domains', []), 'extra_hosts': [host] if host else []}
    raise SSOError('Unknown sign-in provider.')


def enabled_platform_providers():
    return [(key, cfg['name']) for key, cfg in settings.SSO_PROVIDERS.items() if cfg.get('client_id')]


def _json(url, config, **kwargs):
    response = safe_request('GET', url, integration=f'sso:{config["key"]}', extra_hosts=config['extra_hosts'],
                            timeout=10, **kwargs)
    if response.status_code != 200:
        raise SSOError('Identity provider is unavailable.')
    return response.json()


def discovery(config):
    key = f'sso:discovery:{config["issuer"]}'
    document = cache.get(key)
    if document is None:
        issuer = config['issuer'].replace('/common/', '/organizations/') if 'microsoftonline' in config['issuer'] else config['issuer']
        document = _json(f'{issuer}/.well-known/openid-configuration', config)
        cache.set(key, document, 3600)
    return document


def _jwks(config, uri, refresh=False):
    key = f'sso:jwks:{uri}'
    keys = None if refresh else cache.get(key)
    if keys is None:
        keys = _json(uri, config)
        cache.set(key, keys, 3600)
    return keys


def redirect_uri(provider_key):
    return f'{settings.SITE_URL}/auth/sso/{provider_key}/callback/'


def build_authorize_url(request, provider_key, *, purpose, next_url='/', organization=None, event_id=None):
    config = provider_config(provider_key, organization)
    document = discovery(config)
    state = crypto.random_token(24)
    nonce = crypto.random_token(24)
    verifier = crypto.random_token(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    flows = request.session.get(SESSION_KEY, {})
    # Keep at most a handful of in-flight logins per browser.
    flows = dict(list(flows.items())[-4:])
    flows[state] = {'provider': provider_key, 'nonce': nonce, 'verifier': verifier, 'purpose': purpose,
                    'next': next_url, 'organization': organization.pk if organization else None,
                    'event': event_id, 'started': int(time.time())}
    request.session[SESSION_KEY] = flows
    params = {
        'response_type': 'code', 'client_id': config['client_id'], 'redirect_uri': redirect_uri(provider_key),
        'scope': 'openid email profile', 'state': state, 'nonce': nonce,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account',
    }
    return f'{document["authorization_endpoint"]}?{urlencode(params)}'


def _decode_id_token(config, document, id_token, nonce):
    header = jwt.get_unverified_header(id_token)
    if header.get('alg') not in ALLOWED_ALGORITHMS:
        raise SSOError('Unsupported token algorithm.')
    keys = _jwks(config, document['jwks_uri'])
    match = next((k for k in keys.get('keys', []) if k.get('kid') == header.get('kid')), None)
    if match is None:
        keys = _jwks(config, document['jwks_uri'], refresh=True)
        match = next((k for k in keys.get('keys', []) if k.get('kid') == header.get('kid')), None)
    if match is None:
        raise SSOError('Token signing key not found.')
    signing_key = jwt.PyJWK.from_dict(match).key
    unverified = jwt.decode(id_token, options={'verify_signature': False})
    expected_issuer = document.get('issuer', config['issuer'])
    if '{tenantid}' in expected_issuer:  # Microsoft multi-tenant metadata
        expected_issuer = expected_issuer.replace('{tenantid}', str(unverified.get('tid', '')))
    claims = jwt.decode(id_token, signing_key, algorithms=ALLOWED_ALGORITHMS, audience=config['client_id'],
                        issuer=expected_issuer, leeway=60, options={'require': ['exp', 'iat', 'sub']})
    if not crypto.constant_time_equals(claims.get('nonce', ''), nonce):
        raise SSOError('Sign-in response did not match this session.')
    if 'email_verified' not in claims and config['key'] == 'microsoft' and claims.get('email'):
        # Entra ID only issues work/school emails it has verified.
        claims['email_verified'] = True
    return claims


def handle_callback(request, provider_key):
    """Validate the IdP response and return (claims, flow)."""
    if request.GET.get('error'):
        raise SSOError('Sign-in was cancelled or refused by the identity provider.')
    state = request.GET.get('state', '')
    flows = request.session.get(SESSION_KEY, {})
    flow = flows.pop(state, None)
    request.session[SESSION_KEY] = flows
    if flow is None or flow['provider'] != provider_key or time.time() - flow['started'] > 600:
        raise SSOError('This sign-in link has expired. Please try again.')
    organization = None
    if flow.get('organization'):
        from .models import Organization

        organization = Organization.objects.filter(pk=flow['organization']).first()
    config = provider_config(provider_key, organization)
    document = discovery(config)
    response = safe_request('POST', document['token_endpoint'], integration=f'sso:{provider_key}',
                            extra_hosts=config['extra_hosts'], timeout=10, data={
                                'grant_type': 'authorization_code', 'code': request.GET.get('code', ''),
                                'redirect_uri': redirect_uri(provider_key), 'client_id': config['client_id'],
                                'client_secret': config['client_secret'], 'code_verifier': flow['verifier'],
                            })
    if response.status_code != 200:
        raise SSOError('The identity provider rejected the sign-in.')
    try:
        id_token = response.json()['id_token']
        claims = _decode_id_token(config, document, id_token, flow['nonce'])
    except (KeyError, ValueError, json.JSONDecodeError, jwt.PyJWTError) as exc:
        raise SSOError('The sign-in response could not be verified.') from exc
    domains = [d.lower() for d in config.get('allowed_domains', []) if d]
    email = (claims.get('email') or '').lower()
    if domains and email.rsplit('@', 1)[-1] not in domains:
        raise SSOError('Use your institutional account to sign in.')
    return claims, flow
