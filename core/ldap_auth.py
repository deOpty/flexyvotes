"""LDAP / Active Directory authentication for institutional voters.

Organization.ldap_config example::

    {"server_uri": "ldaps://ad.university.edu:636",
     "bind_dn_template": "{username}@university.edu",      # AD UPN, or
     # "bind_dn_template": "uid={username},ou=people,dc=university,dc=edu",
     "search_base": "dc=university,dc=edu",
     "search_filter": "(sAMAccountName={username})",
     "identifier_attribute": "employeeID",
     "start_tls": false}
"""
import re
from urllib.parse import urlparse

USERNAME_RE = re.compile(r'^[A-Za-z0-9._@-]{1,128}$')


class LDAPAuthError(Exception):
    pass


def ldap_authenticate(config, username, password):
    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import escape_rdn

    username = (username or '').strip()
    if not USERNAME_RE.match(username):
        raise LDAPAuthError('Invalid username.')
    if not password:
        # An empty password would be an unauthenticated (anonymous) bind.
        raise LDAPAuthError('Password required.')
    uri = config.get('server_uri', '')
    parsed = urlparse(uri)
    if parsed.scheme not in ('ldaps', 'ldap') or not parsed.hostname:
        raise LDAPAuthError('LDAP server is misconfigured.')
    if parsed.scheme == 'ldap' and not config.get('start_tls'):
        raise LDAPAuthError('LDAP requires ldaps:// or StartTLS.')
    template = config.get('bind_dn_template', '')
    bind_user = template.format(username=escape_rdn(username) if '=' in template else username)
    server = ldap3.Server(parsed.hostname, port=parsed.port, use_ssl=parsed.scheme == 'ldaps',
                          get_info=ldap3.NONE, connect_timeout=5)
    try:
        connection = ldap3.Connection(server, user=bind_user, password=password, receive_timeout=10,
                                      raise_exceptions=True)
        if config.get('start_tls'):
            connection.open()
            connection.start_tls()
        if not connection.bind():
            raise LDAPAuthError('Invalid credentials.')
        attributes = {}
        if config.get('search_base'):
            search_filter = config.get('search_filter', '(uid={username})').format(username=escape_filter_chars(username))
            wanted = list({config.get('identifier_attribute', 'uid'), 'mail', 'cn', 'displayName'})
            connection.search(config['search_base'], search_filter, attributes=wanted, size_limit=1)
            if connection.entries:
                entry = connection.entries[0]
                for name in wanted:
                    if name in entry:
                        value = entry[name].value
                        attributes[name] = value[0] if isinstance(value, list) and value else value
        connection.unbind()
        return attributes
    except LDAPException as exc:
        raise LDAPAuthError('Directory authentication failed.') from exc
