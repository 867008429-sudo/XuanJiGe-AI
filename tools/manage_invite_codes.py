#!/usr/bin/env python3
"""Generate and inspect XuanJiGe invite codes.

Usage:
  python tools/manage_invite_codes.py generate 100
  python tools/manage_invite_codes.py disable XJG-ABCD1234
  python tools/manage_invite_codes.py stats
"""
import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def make_code(prefix, length):
    body = ''.join(secrets.choice(ALPHABET) for _ in range(length))
    return f'{prefix}-{body}' if prefix else body


def generate_codes(args):
    if args.count <= 0:
        raise SystemExit('count must be greater than 0')
    codes = []
    seen = set()
    while len(codes) < args.count:
        code = make_code(args.prefix, args.length)
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)

    inserted = 0 if args.dry_run else db.seed_invite_codes(
        codes,
        max_uses=args.max_uses,
        note=args.note,
    )
    print(f'generated={len(codes)} inserted={inserted} max_uses={args.max_uses}')
    print('\n'.join(codes))


def show_stats(_args):
    stats = db.get_invite_code_stats()
    for key in ['total', 'available', 'used_up', 'disabled']:
        print(f'{key}={stats[key]}')


def disable_codes(args):
    changed = db.disable_invite_codes(args.codes)
    print(f'disabled={changed}')


def main():
    parser = argparse.ArgumentParser(description='Manage XuanJiGe invite codes')
    sub = parser.add_subparsers(dest='command', required=True)

    gen = sub.add_parser('generate', help='generate and seed invite codes')
    gen.add_argument('count', type=int, help='number of codes to generate')
    gen.add_argument('--prefix', default='XJG', help='code prefix')
    gen.add_argument('--length', type=int, default=10, help='random body length')
    gen.add_argument('--max-uses', type=int, default=1, help='uses allowed per code')
    gen.add_argument('--note', default='manual seed', help='operator note')
    gen.add_argument('--dry-run', action='store_true', help='print codes without saving')
    gen.set_defaults(func=generate_codes)

    stats = sub.add_parser('stats', help='show invite code stats')
    stats.set_defaults(func=show_stats)

    disable = sub.add_parser('disable', help='disable one or more invite codes')
    disable.add_argument('codes', nargs='+', help='invite codes to disable')
    disable.set_defaults(func=disable_codes)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
