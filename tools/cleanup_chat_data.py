#!/usr/bin/env python3
"""Clean stale chat checkpoints and metadata."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat_cleanup import cleanup_stale_chat_threads  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='Clean stale XuanJiGe chat data')
    parser.add_argument('--days', type=int, default=30, help='retention window in days')
    parser.add_argument('--limit', type=int, default=1000, help='maximum stale threads per run')
    parser.add_argument('--dry-run', action='store_true', help='show matching threads without deleting')
    args = parser.parse_args()

    summary = cleanup_stale_chat_threads(
        retention_days=args.days,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    printable = dict(summary)
    printable['thread_ids'] = printable['thread_ids'][:20]
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
