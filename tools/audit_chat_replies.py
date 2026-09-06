#!/usr/bin/env python3
"""Run the offline chat reply audit."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat_audit import JUDGE_NAME, run_chat_audit  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='Audit saved XuanJiGe chat replies')
    parser.add_argument('--sample-rate', type=float, default=0.05, help='fraction of replies to audit')
    parser.add_argument('--limit', type=int, default=100, help='maximum replies per run')
    parser.add_argument('--min-count', type=int, default=1, help='minimum replies to audit when any exist')
    parser.add_argument('--judge', default=JUDGE_NAME, help='audit judge implementation')
    parser.add_argument('--dry-run', action='store_true', help='judge without writing audit logs')
    args = parser.parse_args()

    summary = run_chat_audit(
        sample_rate=args.sample_rate,
        limit=args.limit,
        min_count=args.min_count,
        dry_run=args.dry_run,
        judge=args.judge,
    )
    printable = dict(summary)
    printable['results'] = printable['results'][:20]
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
