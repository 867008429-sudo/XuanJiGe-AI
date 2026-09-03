"""人格注册表与 prompt 分区测试（persona-plan.md §2，M1）。

M1 验收（persona-plan §10）：
1. 5 人格 prompt 拼接正确（契约区 + 风格区 + 自缚条款）
2. 契约区逐字一致断言过——全部人格的 system prompt 共享同一契约前缀
3. 图接线：persona_id 进 State、system 随人格变化、换人不清历史
"""
import os
import sqlite3
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage

import chat_graph
from chat_personas import (
    DEFAULT_PERSONA_ID,
    PERSONA_LIST,
    PERSONAS,
    SELF_BIND_CLAUSE,
    build_persona_style,
    get_persona,
)
from chat_graph import build_chat_contract, build_chat_graph, build_chat_system_prompt
from tests.test_ai_productization import SAMPLE_PAIPAN


EXPECTED_IDS = {'xuanzhen', 'qingxu', 'baiyun', 'tiekou', 'zhangmen'}
# 花名册顺序 = 前端卡片展示顺序（M3 起为产品契约，/api/chat/personas 依赖）
EXPECTED_ORDER = ['xuanzhen', 'qingxu', 'baiyun', 'tiekou', 'zhangmen']
EXPECTED_PRICES = {'xuanzhen': 1, 'qingxu': 2, 'baiyun': 2, 'tiekou': 2, 'zhangmen': 5}


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


FINAL_MSG = AIMessage(content='结论一句话。')


class PersonaRegistryTests(unittest.TestCase):
    def test_registry_contains_expected_five(self):
        self.assertEqual(set(PERSONAS), EXPECTED_IDS)
        self.assertEqual(EXPECTED_IDS, {p.persona_id for p in PERSONAS.values()})

    def test_roster_order_is_product_contract(self):
        """PERSONA_LIST 顺序 = 花名册/卡片展示顺序（M3 起路由层直接依赖）。"""
        self.assertEqual(EXPECTED_ORDER, [p.persona_id for p in PERSONA_LIST])
        # 注册表与花名册包含同一批人格，无漂移
        self.assertEqual({p.persona_id for p in PERSONA_LIST}, EXPECTED_IDS)

    def test_field_constraints(self):
        for p in PERSONAS.values():
            with self.subTest(persona=p.persona_id):
                self.assertGreaterEqual(p.price_incense, 1)
                self.assertGreater(p.max_tokens, 0)
                self.assertTrue(0 < p.temperature <= 1.5)
                self.assertTrue(p.title and p.tagline and p.style_prompt)
                # 掌门走 pro 档，其余复用缺省档（M3 接线）
                if p.persona_id == 'zhangmen':
                    self.assertEqual('deepseek-v4-pro', p.model)
                else:
                    self.assertEqual('', p.model)

    def test_prices_match_plan(self):
        for pid, price in EXPECTED_PRICES.items():
            self.assertEqual(price, PERSONAS[pid].price_incense)

    def test_default_persona_is_qingxu(self):
        self.assertEqual('qingxu', DEFAULT_PERSONA_ID)
        self.assertIn(DEFAULT_PERSONA_ID, PERSONAS)

    def test_get_persona_whitelist_and_fallback(self):
        # 空值回落缺省：未升级的旧客户端零改动兼容
        self.assertIs(PERSONAS['qingxu'], get_persona(None))
        self.assertIs(PERSONAS['qingxu'], get_persona(''))
        self.assertIs(PERSONAS['tiekou'], get_persona('tiekou'))
        # 未命中返回 None，路由层据此转 400（用户可控面只有白名单键）
        self.assertIsNone(get_persona('huangdi'))
        self.assertIsNone(get_persona('QINGXU'))  # 大小写敏感：不静默纠偏


class PromptPartitionTests(unittest.TestCase):
    def test_contract_is_shared_prefix_of_every_persona(self):
        """契约区逐字一致：每位道长的 system prompt 共享同一契约前缀。"""
        contract = build_chat_contract()
        for p in PERSONAS.values():
            prompt = build_chat_system_prompt(p)
            with self.subTest(persona=p.persona_id):
                self.assertTrue(prompt.startswith(contract))
                # 风格区紧跟契约区之后，自缚条款在风格段开头
                style_part = prompt[len(contract):]
                self.assertTrue(style_part.startswith('\n\n' + SELF_BIND_CLAUSE))

    def test_contract_carries_rules_and_safety(self):
        contract = build_chat_contract()
        # 工具规则与安全边界（对抗越界的判据，M5 评测依赖这些锚点）
        for anchor in (
            '工具使用规则', 'query_liunian', 'lookup_classics',
            '禁止自行推算历法', '禁止编造原文',
            '回答规范', '投资收益保证', '疾病诊断', '恐吓式表达',
            '先答用户所问', '不用 markdown 标题',
        ):
            self.assertIn(anchor, contract)

    def test_self_bind_clause_wraps_every_style(self):
        for p in PERSONAS.values():
            style = build_persona_style(p)
            with self.subTest(persona=p.persona_id):
                self.assertTrue(style.startswith(SELF_BIND_CLAUSE))
                self.assertIn(p.style_prompt, style)

    def test_default_prompt_uses_qingxu_style(self):
        # persona=None 回落缺省：与显式传 qingxu 的输出逐字一致
        self.assertEqual(
            build_chat_system_prompt(get_persona('qingxu')),
            build_chat_system_prompt(None),
        )

    def test_persona_openings_are_distinct(self):
        # 五位道长的自述各不相同（辨识度下限；盲评见 M5）
        intros = {
            p.persona_id: p.style_prompt[:6] for p in PERSONAS.values()
        }
        self.assertEqual(len(set(intros.values())), len(intros))


class GraphPersonaWiringTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(
            os.path.join(self.tempdir.name, 'chat.db'), check_same_thread=False
        )
        conn.execute('PRAGMA journal_mode = WAL')
        self.saver = chat_graph.SqliteSaver(conn)
        self.saver.setup()
        chat_graph._checkpointer = self.saver

    def tearDown(self):
        chat_graph.reset_checkpointer()
        self.tempdir.cleanup()

    def make_graph(self, persona_id, responses=None):
        self.model = FakeModel(responses or [FINAL_MSG])
        return build_chat_graph(
            SAMPLE_PAIPAN, name='张三', persona_id=persona_id,
            model=self.model, checkpointer=self.saver,
        )

    def test_system_prompt_follows_persona_and_state_records_it(self):
        graph = self.make_graph('tiekou')
        out = graph.invoke(
            {'messages': [HumanMessage('看下流年')]},
            config=chat_graph.chat_invoke_config('acct:9:900'),
        )
        self.assertEqual('tiekou', out['persona_id'])
        first = self.model.calls[0][0]
        self.assertEqual('system', first.type)
        self.assertIn('铁口神算', first.content)
        self.assertIn('工具使用规则', first.content)  # 契约区照常在场

    def test_unknown_persona_raises_value_error(self):
        # 路由层校验白名单在前；走到这里是编程错误，宁可崩不可静默
        with self.assertRaises(ValueError):
            self.make_graph('huangdi')

    def test_persona_switch_keeps_history_and_updates_state(self):
        """换道长：会话历史保留、system 换新人格、persona_id 更新为当前（§4.2）。"""
        g1 = self.make_graph('xuanzhen', [FINAL_MSG])
        g1.invoke(
            {'messages': [HumanMessage('看下流年')]},
            config=chat_graph.chat_invoke_config('acct:9:901'),
        )
        # 用户中途改请教白云师太：新图编译（同一 checkpointer 同一 thread）
        g2 = self.make_graph('baiyun', [AIMessage(content='换个角度看，宜静养。')])
        out2 = g2.invoke(
            {'messages': [HumanMessage('那我该注意什么')]},
            config=chat_graph.chat_invoke_config('acct:9:901'),
        )
        # 消息历史跨人格连续：2 human + 2 ai = 4，无丢失
        self.assertEqual(4, len(out2['messages']))
        self.assertEqual('baiyun', out2['persona_id'])
        # 第二回合模型收到的 system 已是白云师太，且能看到全部历史
        first, history = self.model.calls[0][0], self.model.calls[0][1:]
        self.assertIn('白云师太', first.content)
        self.assertEqual(3, len(history))  # 首轮 2 条 + 本轮 human 1 条
        # 契约区不变：换人换的是风格，不是规则
        self.assertIn('工具使用规则', first.content)

    def test_default_graph_uses_qingxu(self):
        # 不传 persona_id 的旧调用路径行为不变（向后兼容验收）
        model = FakeModel([FINAL_MSG])
        graph = build_chat_graph(
            SAMPLE_PAIPAN, name='张三', model=model, checkpointer=self.saver,
        )
        out = graph.invoke(
            {'messages': [HumanMessage('看下流年')]},
            config=chat_graph.chat_invoke_config('acct:9:902'),
        )
        self.assertEqual('qingxu', out['persona_id'])
        self.assertIn('清虚道长', model.calls[0][0].content)


if __name__ == '__main__':
    unittest.main()
