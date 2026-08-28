import unittest

import ai_service
from ai_client import validate_chat_payload
from ai_context import build_bazi_context
from ai_prompts import build_prompt
from ai_validator import validate_interpretation, validate_structured_outline


SAMPLE_PAIPAN = {
    'gender': 'male',
    'solar_date': '1995年8月16日 10:30',
    'lunar_date': '乙亥年 七月廿一 巳时',
    'day_master': '己',
    'day_master_wuxing': '土',
    'geju': '伤官格',
    'shenwang': '身弱',
    'qiyun_age': 3,
    'forward': False,
    'four_pillars': {
        'year': {
            'gan': '乙',
            'zhi': '亥',
            'gan_shishen': '七杀',
            'zhi_shishen': '正财',
            'zhi_canggan': ['壬', '甲'],
            'nayin': '山头火',
        },
        'month': {
            'gan': '甲',
            'zhi': '申',
            'gan_shishen': '正官',
            'zhi_shishen': '伤官',
            'zhi_canggan': ['庚', '壬', '戊'],
            'nayin': '泉中水',
        },
        'day': {
            'gan': '己',
            'zhi': '卯',
            'gan_shishen': '日主',
            'zhi_shishen': '七杀',
            'zhi_canggan': ['乙'],
            'nayin': '城头土',
        },
        'hour': {
            'gan': '己',
            'zhi': '巳',
            'gan_shishen': '比肩',
            'zhi_shishen': '正印',
            'zhi_canggan': ['丙', '庚', '戊'],
            'nayin': '大林木',
        },
    },
    'wuxing_count': {'金': 1, '木': 3, '水': 1, '火': 1, '土': 2},
    'wuxing_percent': {'金': 12, '木': 38, '水': 12, '火': 12, '土': 25},
    'xiyong': ['火(印星)', '土(比劫)'],
    'jishen': ['木(官杀)', '水(财星)', '金(食伤)'],
    'shensha': ['天乙贵人'],
    'zhi_relations': ['申巳刑'],
    'dayun': [
        {'start_age': 13, 'end_age': 22, 'start_year': 2008, 'end_year': 2017, 'gan': '壬', 'zhi': '午', 'gan_shishen': '正财', 'zhi_shishen': '偏印', 'nayin': '杨柳木'},
        {'start_age': 23, 'end_age': 32, 'start_year': 2018, 'end_year': 2027, 'gan': '辛', 'zhi': '巳', 'gan_shishen': '食神', 'zhi_shishen': '正印', 'nayin': '白蜡金'},
        {'start_age': 33, 'end_age': 42, 'start_year': 2028, 'end_year': 2037, 'gan': '庚', 'zhi': '辰', 'gan_shishen': '伤官', 'zhi_shishen': '劫财', 'nayin': '白蜡金'},
    ],
}


def valid_outline():
    sections = []
    for name in ['性格', '财运', '婚姻', '健康', '大运', '总评']:
        sections.append({
            'name': name,
            'claim': f'{name}围绕己土身弱与甲申月令展开',
            'evidence': ['己土日主身弱', '甲申月令见伤官', '当前辛巳大运'],
            'real_world_mapping': ['做事重秩序但容易被压力推着走', '适合在规则清楚的环境中积累专业'],
            'advice': ['先稳作息和节奏', '重大选择多做验证再行动'],
            'classic_hint': {'book': '滴天髓', 'meaning': '旺衰喜忌先分清'},
        })
    return {
        'summary': '己土身弱，甲申月令见伤官，当前辛巳大运重在以印护身。',
        'sections': sections,
        'current_dayun': {'label': '辛巳', 'focus': '辛巳大运以食神配正印为主线'},
        'risk_notes': ['不作医疗和投资承诺'],
    }


def long_interpretation():
    paragraph = (
        '己土日主生在甲申月令，盘中伤官、正官与七杀并见，身弱而喜火土来扶。'
        '当前辛巳大运带食神与正印，适合把表达、技能和证书化能力沉淀下来。'
        '这不是简单说顺利，而是说做事要先有规则、再有发挥空间，遇到压力时用流程托住自己。'
        '建议把重要选择拆成小试验，少凭一时情绪定终局。'
        '盘面里的乙亥、己卯、己巳彼此牵动，说明判断不能只看一个十神，要把月令、日支和大运合在一起看。'
        '落到现实里，就是先建立稳定节奏，再让才华表达出来，这样更容易把压力转成作品和结果。'
    )
    return ''.join(f'【{name}】{paragraph}' for name in ['性格', '财运', '婚姻', '健康', '大运', '总评'])


class AIProductizationTests(unittest.TestCase):
    def test_context_identifies_current_dayun(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)

        self.assertEqual(context['schema_version'], 'bazi-context-v1')
        self.assertEqual(context['birth']['current_age'], 31)
        self.assertEqual(context['luck']['current_dayun']['label'], '辛巳')
        self.assertIn('己土', context['evidence_terms'])

    def test_prompt_contains_generation_contract_and_current_dayun(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        system_prompt, user_prompt = build_prompt(SAMPLE_PAIPAN, context=context)

        self.assertIn('不得重新排盘', system_prompt)
        self.assertIn('输出前自检', system_prompt)
        self.assertIn('当前大运：辛巳', user_prompt)

    def test_model_payload_validator_catches_bad_params(self):
        issues = validate_chat_payload({
            'model': '',
            'messages': [{'role': 'user', 'content': ''}],
            'max_tokens': 0,
            'stream': 'yes',
        })

        self.assertGreaterEqual(len(issues), 4)

    def test_structured_outline_validator_accepts_complete_outline(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        issues = validate_structured_outline(valid_outline(), context=context)

        self.assertEqual([], issues)

    def test_structured_outline_validator_rejects_missing_evidence(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        outline = valid_outline()
        outline['sections'][0]['evidence'] = ['感觉比较稳']

        issues = validate_structured_outline(outline, context=context)

        self.assertIn('性格证据不足', issues)

    def test_interpretation_validator_accepts_evidence_rich_text(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        result = validate_interpretation(long_interpretation(), context=context)

        self.assertTrue(result['ok'], result['issues'])
        self.assertGreaterEqual(len(result['evidence_hits']), 3)

    def test_interpretation_validator_rejects_risky_claims(self):
        context = build_bazi_context(SAMPLE_PAIPAN, current_year=2026)
        text = long_interpretation() + '因此你必定发财，也一定离婚。'

        result = validate_interpretation(text, context=context)

        self.assertFalse(result['ok'])
        self.assertTrue(any('绝对化' in issue for issue in result['issues']))

    def test_ai_service_keeps_compatible_public_api(self):
        self.assertTrue(callable(ai_service.stream_interpretation))
        self.assertTrue(callable(ai_service.build_cache_key))
        self.assertEqual(ai_service.DEEPSEEK_MODEL, 'deepseek-v4-flash')


if __name__ == '__main__':
    unittest.main()
