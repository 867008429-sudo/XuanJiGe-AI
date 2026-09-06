import os
import sqlite3
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage

import chat_graph
from ai_validator import validate_interpretation
from chat_graph import build_chat_contract, build_chat_graph, build_chat_system_prompt
from chat_personas import PERSONA_LIST, PERSONAS, SELF_BIND_CLAUSE
from chat_tools import build_chat_tools
from tests.test_ai_productization import SAMPLE_PAIPAN, long_interpretation


class FakeBound:
    def __init__(self, parent):
        self.parent = parent

    def invoke(self, msgs):
        self.parent.calls.append(list(msgs))
        if not self.parent.responses:
            raise AssertionError('FakeModel 脚本耗尽')
        return self.parent.responses.pop(0)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def bind_tools(self, tools):
        return FakeBound(self)


TOOL_CALL_LIUNIAN_2027 = AIMessage(
    content='',
    tool_calls=[{
        'id': 'call_m5_liunian',
        'name': 'query_liunian',
        'args': {'year': 2027},
    }],
)


class PersonaM5FactConsistencyTests(unittest.TestCase):
    """M5：风格可变，事实来源必须在人格间收敛。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(
            os.path.join(self.tempdir.name, 'chat.db'), check_same_thread=False
        )
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.saver = chat_graph.SqliteSaver(self.conn)
        self.saver.setup()

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_same_liunian_fact_is_identical_across_personas(self):
        facts_by_persona = {}

        for persona in PERSONA_LIST:
            model = FakeModel([
                TOOL_CALL_LIUNIAN_2027,
                AIMessage(content=f'{persona.title}按同一工具事实作答。'),
            ])
            graph = build_chat_graph(
                SAMPLE_PAIPAN,
                name='张三',
                persona_id=persona.persona_id,
                model=model,
                checkpointer=self.saver,
            )
            out = graph.invoke(
                {'messages': [HumanMessage('2027 年流年如何？')]},
                config=chat_graph.chat_invoke_config(f'acct:m5:{persona.persona_id}'),
            )
            facts_by_persona[persona.persona_id] = out['messages'][2].content

        self.assertEqual(1, len(set(facts_by_persona.values())))
        fact_text = next(iter(facts_by_persona.values()))
        self.assertIn('2027年 流年柱 丁未', fact_text)
        self.assertIn('日主己为偏印', fact_text)

    def test_persona_switch_keeps_prior_fact_and_changes_style(self):
        thread_id = 'acct:m5:switch'
        first_model = FakeModel([
            AIMessage(content='2027 丁未年，年干对己土日主为偏印。')
        ])
        first_graph = build_chat_graph(
            SAMPLE_PAIPAN,
            name='张三',
            persona_id='xuanzhen',
            model=first_model,
            checkpointer=self.saver,
        )
        first_graph.invoke(
            {'messages': [HumanMessage('先看 2027 年')]},
            config=chat_graph.chat_invoke_config(thread_id),
        )

        second_model = FakeModel([
            AIMessage(content='换个角度看，仍以前面丁未偏印这条事实为前情。')
        ])
        second_graph = build_chat_graph(
            SAMPLE_PAIPAN,
            name='张三',
            persona_id='baiyun',
            model=second_model,
            checkpointer=self.saver,
        )
        out = second_graph.invoke(
            {'messages': [HumanMessage('那我换白云师太问，具体注意什么？')]},
            config=chat_graph.chat_invoke_config(thread_id),
        )

        system_msg = second_model.calls[0][0].content
        history_text = '\n'.join(str(getattr(m, 'content', '')) for m in second_model.calls[0][1:])
        self.assertEqual('baiyun', out['persona_id'])
        self.assertIn('白云师太', system_msg)
        self.assertNotIn('玄真散人', system_msg)
        self.assertIn('2027 丁未年', history_text)
        self.assertIn('偏印', history_text)


class PersonaM5SafetyBoundaryTests(unittest.TestCase):
    """M5：高风险诱导不应被人格风格稀释或覆盖。"""

    def test_safety_contract_precedes_every_persona_style(self):
        contract = build_chat_contract()
        for persona in PERSONA_LIST:
            prompt = build_chat_system_prompt(persona)
            with self.subTest(persona=persona.persona_id):
                self.assertTrue(prompt.startswith(contract))
                self.assertLess(prompt.index('禁止：投资收益保证'), prompt.index(SELF_BIND_CLAUSE))
                self.assertIn('健康话题只做养生级提醒', prompt)

    def test_high_risk_personas_have_explicit_style_rails(self):
        self.assertIn('不恐吓、不贩卖焦虑', PERSONAS['tiekou'].style_prompt)
        self.assertIn('不因宽慰而软化事实', PERSONAS['baiyun'].style_prompt)
        self.assertIn('每层都回扣盘面证据', PERSONAS['zhangmen'].style_prompt)

    def test_existing_validator_catches_adversarial_risky_claims(self):
        cases = {
            'investment': '这个组合意味着稳赚不赔，可以重仓。',
            'medical': '按盘面看已经诊断为严重疾病。',
            'marriage': '这段关系一定离婚，没有余地。',
        }
        base = long_interpretation()

        for case, suffix in cases.items():
            with self.subTest(case=case):
                result = validate_interpretation(base + suffix, paipan_data=SAMPLE_PAIPAN)
                self.assertFalse(result['ok'])
                self.assertTrue(any('绝对化或医疗化' in issue for issue in result['issues']))

    def test_classics_miss_keeps_fake_reference_from_becoming_fact(self):
        tools = {tool.name: tool for tool in build_chat_tools(SAMPLE_PAIPAN)}
        out = tools['search_classics'].invoke({'query': '滴天髓稳赚不赔原文'})

        self.assertIn('查无此文', out)
        self.assertIn('不得编造原文', out)
        self.assertNotIn('命中 1', out)
        self.assertNotIn('chunk_id=', out)


if __name__ == '__main__':
    unittest.main()
