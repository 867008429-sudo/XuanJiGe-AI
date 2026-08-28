import unittest

from ai_context import build_bazi_context
from ai_prompts import build_prompt, build_structured_prompt
from ai_validator import validate_interpretation, validate_structured_outline
from bazi_knowledge import ALLOWED_CLASSIC_BOOKS, KNOWLEDGE_VERSION, build_knowledge_packet, knowledge_to_prompt_text
from tests.test_ai_productization import SAMPLE_PAIPAN, valid_outline


class BaziKnowledgeTests(unittest.TestCase):
    def test_knowledge_packet_matches_chart_context(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        packet = build_knowledge_packet(context)
        ids = {item['id'] for item in packet['items']}

        self.assertEqual(KNOWLEDGE_VERSION, packet['version'])
        self.assertIn('滴天髓', packet['allowed_books'])
        self.assertIn('strength:身弱', ids)
        self.assertIn('geju:伤官格', ids)
        self.assertIn('ten_god:正官', ids)
        self.assertIn('ten_god:食神', ids)
        self.assertIn('wuxing:木:high', ids)
        self.assertIn('relation:刑', ids)

    def test_knowledge_prompt_is_traceable_and_disallows_fake_citations(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        packet = build_knowledge_packet(context)
        prompt_text = knowledge_to_prompt_text(packet)

        self.assertIn('可引用知识片段', prompt_text)
        self.assertIn(KNOWLEDGE_VERSION, prompt_text)
        self.assertIn('不要编造卷页', prompt_text)
        self.assertIn('触发=身弱', prompt_text)

    def test_final_and_structured_prompts_include_knowledge_packet(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        system_prompt, user_prompt = build_prompt(SAMPLE_PAIPAN, context=context)
        structured_system, structured_user = build_structured_prompt(SAMPLE_PAIPAN, context=context)

        self.assertIn('古籍引用只能来自用户提示中的可引用知识片段', system_prompt)
        self.assertIn('可引用知识片段', user_prompt)
        self.assertIn(KNOWLEDGE_VERSION, user_prompt)
        self.assertIn('classic_hint 只能从可引用知识片段', structured_system)
        self.assertIn('允许引用书名', structured_user)

    def test_structured_validator_rejects_unknown_classic_book(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        outline = valid_outline()
        self.assertIn(outline['sections'][0]['classic_hint']['book'], ALLOWED_CLASSIC_BOOKS)
        outline['sections'][0]['classic_hint']['book'] = '玄机秘本'

        issues = validate_structured_outline(outline, context=context)

        self.assertIn('性格古籍来源不在知识库', issues)


    def test_final_validator_rejects_unknown_classic_book(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        text = ''.join(
            f'【{name}】己土日主生在甲申月令，身弱喜火土，当前辛巳大运带食神与正印，适合把表达、技能和证书化能力沉淀下来。盘面里的乙亥、己卯、己巳彼此牵动，说明判断不能只看一个十神。现实中先建立稳定节奏，再让才华表达出来。——玄机秘本云：此处是伪造来源。'
            for name in ['性格', '财运', '婚姻', '健康', '大运', '总评']
        )

        result = validate_interpretation(text, context=context)

        self.assertFalse(result['ok'])
        self.assertTrue(any('知识库外古籍引用' in issue for issue in result['issues']))

if __name__ == '__main__':
    unittest.main()
