#!/usr/bin/env python3
"""Build/smoke the local classics search index for R2."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from classics_search import ClassicsSearchIndex, DEFAULT_CORPUS_DIR  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='Build local classics BM25 index')
    parser.add_argument('--corpus-dir', default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument('--query', default='', help='optional smoke query')
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--manifest', default='', help='optional path to write index stats JSON')
    args = parser.parse_args()

    index = ClassicsSearchIndex.load(Path(args.corpus_dir))
    result = {'index': index.stats()}
    if args.query.strip():
        result['query'] = args.query
        result['hits'] = [hit.to_dict() for hit in index.search(args.query, k=args.k)]

    if args.manifest:
        out_path = Path(args.manifest)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result['index'], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
