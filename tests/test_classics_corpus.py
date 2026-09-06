import unittest

from tools import build_classics_corpus as corpus


class ClassicsCorpusBuilderTests(unittest.TestCase):
    def test_luckclub_parser_keeps_original_and_drops_translation(self):
        text = '''
《》
www.luckclub.cn · 古籍典藏 · 内容仅供文化学习研究
第 0  章
目录
原 文
子平真诠 - 目录
001. 序
第 1  章
论用神
原 文
论用神
八字用神，专求月令。
用神既定，然后论相神。
白话译文
这里是现代白话解释，不得入库。
---
关键词
现代启示
第 2  章
论相神
原 文
论相神
相神者，所以辅用也。
www.luckclub.cn · 古籍典藏 · 内容仅供文化学习研究
'''

        chunks = corpus.parse_luckclub_text('子平真诠', 'zipingzhenquan', 'sample.txt', text)

        self.assertEqual(2, len(chunks))
        self.assertEqual('zipingzhenquan-ch001', chunks[0]['chunk_id'])
        self.assertEqual('论用神', chunks[0]['chapter_title'])
        combined = '\n'.join(row['text'] for row in chunks)
        self.assertIn('八字用神，专求月令。', combined)
        self.assertIn('相神者，所以辅用也。', combined)
        self.assertNotIn('目录', combined)
        self.assertNotIn('白话译文', combined)
        self.assertNotIn('现代白话解释', combined)
        self.assertNotIn('luckclub.cn', combined)
        self.assertNotIn('关键词', combined)
        self.assertNotIn('现代启示', combined)

    def test_chanwei_parser_splits_by_classical_headings(self):
        text = '''
【滴天髓阐微】
通神论 一、天道
欲识三元万法宗，先观帝载与神功。
原注：天有阴阳。

通神论 二、地道
坤元合德机缄通，五气偏全定吉凶。

六亲论 一、夫妻
夫妻因缘宿世来，喜神有意傍天财。
'''

        chunks = corpus.parse_chanwei_text('滴天髓阐微', 'ditiansui-chanwei', 'sample.txt', text)

        self.assertEqual(3, len(chunks))
        self.assertEqual('通神论 一、天道', chunks[0]['chapter_title'])
        self.assertEqual('ditiansui-chanwei-ch003', chunks[2]['chunk_id'])
        self.assertIn('夫妻因缘宿世来', chunks[2]['text'])


if __name__ == '__main__':
    unittest.main()
