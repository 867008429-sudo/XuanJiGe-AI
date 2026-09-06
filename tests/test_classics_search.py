import json
from pathlib import Path
import tempfile
import unittest

from classics_search import ClassicsSearchIndex, search_classics


class ClassicsSearchTests(unittest.TestCase):
    def build_temp_index(self):
        tempdir = tempfile.TemporaryDirectory()
        path = Path(tempdir.name) / 'sample.jsonl'
        rows = [
            {
                'chunk_id': 'a-ch001',
                'book': '子平真诠',
                'chapter_no': 1,
                'chapter_title': '论用神',
                'text': '八字用神，专求月令。格局成败，先看相神。',
                'source_file': 'sample-a.txt',
            },
            {
                'chunk_id': 'b-ch001',
                'book': '滴天髓阐微',
                'chapter_no': 1,
                'chapter_title': '通神论 寒暖',
                'text': '天道有寒暖，地道有燥湿，命局贵在调停。',
                'source_file': 'sample-b.txt',
            },
        ]
        with path.open('w', encoding='utf-8', newline='\n') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.addCleanup(tempdir.cleanup)
        return ClassicsSearchIndex.load(tempdir.name)

    def test_exact_domain_terms_rank_expected_chunk(self):
        index = self.build_temp_index()
        hits = index.search('寒暖燥湿调候', k=2)

        self.assertEqual('b-ch001', hits[0].chunk.chunk_id)
        self.assertIn('寒暖', hits[0].matched_terms)
        self.assertIn('燥湿', hits[0].excerpt)

    def test_book_filter_keeps_results_groundable(self):
        index = self.build_temp_index()
        hits = index.search('用神月令', k=5, book='子平真诠')

        self.assertEqual(1, len(hits))
        self.assertEqual('子平真诠', hits[0].chunk.book)
        self.assertEqual('a-ch001', hits[0].chunk.chunk_id)

    def test_empty_query_returns_no_hits(self):
        index = self.build_temp_index()
        self.assertEqual([], index.search('   '))

    def test_default_corpus_search_returns_chunk_ids(self):
        corpus_dir = Path(__file__).resolve().parents[1] / 'data' / 'classics'
        if not corpus_dir.exists():
            self.skipTest('R1 classics corpus is not generated')

        hits = search_classics('寒暖燥湿怎么判断', k=5, corpus_dir=corpus_dir)

        self.assertTrue(hits)
        self.assertTrue(all(hit['chunk_id'] for hit in hits))
        self.assertTrue(any(hit['book'] == '滴天髓阐微' for hit in hits))
        combined = '\n'.join(hit['text'] + hit['source_file'] for hit in hits)
        self.assertNotIn('luckclub', combined.lower())
        self.assertNotIn('白话译文', combined)


if __name__ == '__main__':
    unittest.main()
