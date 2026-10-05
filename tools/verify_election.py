#!/usr/bin/env python3
"""Independent, offline verifier for a FlexyVotes election.

Needs only Python 3.9+ and the `cryptography` package - no access to the
platform. Download the bundle from /verify/<election_id>/bundle.json, then:

    python tools/verify_election.py bundle.json [--tracker <your ballot tracker>]
                                                [--trusted-key <base64 public key>]

Checks: configuration hash + Ed25519 signature, certification signature,
bulletin-board Merkle root, published result hash, and (optionally) that a
voter's tracker is on the bulletin board.
"""
import argparse
import base64
import hashlib
import json
import sys


def b64d(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def verify_ed25519(payload, signature, public_key):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_key)).verify(b64d(signature), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def leaf(value):
    return hashlib.sha256(b'\x00' + bytes.fromhex(value)).hexdigest()


def pair(left, right):
    return hashlib.sha256(bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def merkle_root(trackers):
    level = [leaf(t) for t in trackers]
    if not level:
        return sha256_hex(b'')
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('bundle')
    parser.add_argument('--tracker')
    parser.add_argument('--trusted-key', action='append', default=[],
                        help='Base64 Ed25519 key you obtained independently (e.g. from the organizer).')
    args = parser.parse_args()
    with open(args.bundle, encoding='utf-8') as handle:
        bundle = json.load(handle)

    results = []

    def check(name, ok, detail=''):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    print(f"Election: {bundle['election']['title']} (status {bundle['election']['status']})")
    config = bundle.get('configuration')
    if config:
        encoded = canonical(config['config'])
        check('configuration hash', sha256_hex(encoded) == config['config_hash'], config['config_hash'][:16])
        check('configuration signature', verify_ed25519(encoded, config['signature'], config['public_key']))
    trackers = bundle['bulletin_board']['trackers']
    root = merkle_root(trackers)
    check('bulletin board root', root == bundle['bulletin_board']['merkle_root'], f'{len(trackers)} ballots')
    cert = bundle.get('certification')
    if cert:
        payload = canonical(cert['payload'])
        check('certification signature', verify_ed25519(payload, cert['signature'], cert['public_key']), cert['key_id'])
        check('certified bulletin root', cert['payload'].get('bulletin_root') == root)
        if config:
            check('certified configuration', cert['payload'].get('config_hash') == config['config_hash'])
        if bundle.get('result') is not None:
            stable = {k: v for k, v in bundle['result'].items() if k != 'generated_at'}
            check('result hash', sha256_hex(canonical(stable)) == cert['payload'].get('result_hash'))
        trusted = args.trusted_key or bundle.get('verification_keys', [])
        check('signing key trusted', cert['public_key'] in trusted,
              '(supplied)' if args.trusted_key else '(from bundle - obtain the key independently for full assurance)')
    else:
        print('[INFO] Results are not certified yet.')
    if args.tracker:
        check('your ballot is on the bulletin board', args.tracker.strip().lower() in trackers)
    print('ALL CHECKS PASSED' if all(results) else 'VERIFICATION FAILED')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
