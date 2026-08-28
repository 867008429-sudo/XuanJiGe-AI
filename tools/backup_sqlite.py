#!/usr/bin/env python3
"""Create a consistent SQLite backup for XuanJiGe."""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def _next_backup_path(source_path, backup_dir):
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    base_name = f'{source_path.stem}-{timestamp}'
    candidate = backup_dir / f'{base_name}.db'
    suffix = 1
    while candidate.exists():
        candidate = backup_dir / f'{base_name}-{suffix}.db'
        suffix += 1
    return candidate


def _prune_backups(source_path, backup_dir, keep_last):
    if keep_last is None or keep_last <= 0:
        return []
    backups = sorted(
        backup_dir.glob(f'{source_path.stem}-*.db'),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for old_backup in backups[keep_last:]:
        old_backup.unlink()
        removed.append(old_backup)
    return removed


def backup_database(source=None, backup_dir=None, keep_last=None):
    source_path = Path(source or db.DB_PATH).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f'Database not found: {source_path}')
    if not source_path.is_file():
        raise ValueError(f'Database path is not a file: {source_path}')

    target_dir = Path(backup_dir or os.environ.get('BACKUP_DIR', ROOT / 'backups')).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _next_backup_path(source_path, target_dir)

    source_conn = sqlite3.connect(str(source_path))
    target_conn = sqlite3.connect(str(target_path))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()

    removed = _prune_backups(source_path, target_dir, keep_last)
    return {
        'source': str(source_path),
        'backup': str(target_path),
        'bytes': target_path.stat().st_size,
        'removed': [str(item) for item in removed],
    }


def main():
    parser = argparse.ArgumentParser(description='Backup the XuanJiGe SQLite database')
    parser.add_argument('--source', default=db.DB_PATH, help='SQLite database path')
    parser.add_argument('--dest-dir', default=os.environ.get('BACKUP_DIR', str(ROOT / 'backups')), help='backup directory')
    parser.add_argument('--keep-last', type=int, default=14, help='number of backups to keep; 0 disables pruning')
    args = parser.parse_args()

    result = backup_database(args.source, args.dest_dir, args.keep_last)
    print(f"backup={result['backup']}")
    print(f"bytes={result['bytes']}")
    if result['removed']:
        print(f"removed={len(result['removed'])}")


if __name__ == '__main__':
    main()
