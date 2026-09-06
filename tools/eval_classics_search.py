#!/usr/bin/env python3
"""Evaluate the R2 classics retrieval baseline on fixed query cases."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from classics_search import ClassicsSearchIndex, DEFAULT_CORPUS_DIR  # noqa: E402


EVAL_CASES = [
    {'query': '寒暖如何看命局气候', 'expected_books': ['滴天髓阐微'], 'expected_terms': ['寒暖']},
    {'query': '燥湿太过怎么调候', 'expected_books': ['滴天髓阐微'], 'expected_terms': ['燥湿']},
    {'query': '通关在八字里是什么意思', 'expected_books': ['滴天髓阐微'], 'expected_terms': ['通关']},
    {'query': '源流清浊怎么看', 'expected_books': ['滴天髓阐微'], 'expected_terms': ['源流', '清浊']},
    {'query': '顺逆旺衰如何取用', 'expected_books': ['滴天髓原文', '滴天髓阐微'], 'expected_terms': ['顺逆', '旺衰']},
    {'query': '夫妻子女六亲怎么论', 'expected_books': ['滴天髓阐微'], 'expected_terms': ['夫妻', '子女']},
    {'query': '父母兄弟六亲关系', 'expected_books': ['三命通会', '神峰通考', '滴天髓阐微'], 'expected_terms': ['父母', '兄弟', '六亲']},
    {'query': '甲木三春调候用神', 'expected_books': ['穷通宝鉴'], 'expected_terms': ['甲木', '三春']},
    {'query': '乙木三夏取用', 'expected_books': ['穷通宝鉴'], 'expected_terms': ['乙木', '三夏']},
    {'query': '丙火三秋喜忌', 'expected_books': ['穷通宝鉴'], 'expected_terms': ['丙火', '三秋']},
    {'query': '壬水三冬调候', 'expected_books': ['穷通宝鉴'], 'expected_terms': ['壬水', '三冬']},
    {'query': '十干体象', 'expected_books': ['渊海子平'], 'expected_terms': ['十干']},
    {'query': '十神生克财官印绶', 'expected_books': ['渊海子平', '三命通会'], 'expected_terms': ['财官', '印绶']},
    {'query': '六十甲子纳音', 'expected_books': ['三命通会', '渊海子平'], 'expected_terms': ['纳音']},
    {'query': '论用神成败救应', 'expected_books': ['子平真诠'], 'expected_terms': ['用神', '成败']},
    {'query': '论相神喜神忌神', 'expected_books': ['子平真诠'], 'expected_terms': ['相神', '喜神', '忌神']},
    {'query': '格局成败如何判断', 'expected_books': ['子平真诠'], 'expected_terms': ['格局', '成败']},
    {'query': '伤官配印格局', 'expected_books': ['子平真诠', '滴天髓阐微'], 'expected_terms': ['伤官', '印']},
    {'query': '羊刃七杀怎么取法', 'expected_books': ['三命通会', '渊海子平'], 'expected_terms': ['羊刃', '七杀']},
    {'query': '女命夫星婚姻怎么看', 'expected_books': ['神峰通考', '三命通会'], 'expected_terms': ['女命', '夫']},
]


def _case_hit(case, hits):
    expected_books = set(case['expected_books'])
    expected_terms = case.get('expected_terms') or []
    for hit in hits:
        chunk = hit.chunk
        if chunk.book not in expected_books:
            continue
        haystack = f'{chunk.chapter_title}\n{chunk.text}'
        if not expected_terms or any(term in haystack for term in expected_terms):
            return True
    return False


def run_eval(corpus_dir=DEFAULT_CORPUS_DIR, k=5):
    index = ClassicsSearchIndex.load(corpus_dir)
    cases = []
    passed = 0
    for case in EVAL_CASES:
        hits = index.search(case['query'], k=k)
        ok = _case_hit(case, hits)
        passed += int(ok)
        cases.append({
            'query': case['query'],
            'ok': ok,
            'expected_books': case['expected_books'],
            'top_hits': [
                {
                    'chunk_id': hit.chunk.chunk_id,
                    'book': hit.chunk.book,
                    'chapter_title': hit.chunk.chapter_title,
                    'score': round(hit.score, 4),
                    'matched_terms': list(hit.matched_terms),
                }
                for hit in hits[:3]
            ],
        })
    total = len(EVAL_CASES)
    return {
        'index': index.stats(),
        'k': k,
        'passed': passed,
        'total': total,
        'hit_rate': round(passed / total, 4) if total else 0.0,
        'cases': cases,
    }


def main():
    parser = argparse.ArgumentParser(description='Evaluate classics retrieval baseline')
    parser.add_argument('--corpus-dir', default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--min-hit-rate', type=float, default=0.8)
    parser.add_argument('--json', action='store_true', help='print full JSON report')
    args = parser.parse_args()

    report = run_eval(Path(args.corpus_dir), k=args.k)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"classics retrieval: {report['passed']}/{report['total']} "
            f"hit_rate={report['hit_rate']:.2%} k={report['k']}"
        )
        for case in report['cases']:
            status = 'PASS' if case['ok'] else 'FAIL'
            top = case['top_hits'][0] if case['top_hits'] else {}
            print(f"- {status} {case['query']} -> {top.get('book', '-')}/{top.get('chunk_id', '-')}")

    return 0 if report['hit_rate'] >= args.min_hit_rate else 1


if __name__ == '__main__':
    raise SystemExit(main())
