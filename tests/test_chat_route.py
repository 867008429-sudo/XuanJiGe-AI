"""chat 追问链路的路由层测试（/api/chat 及配套端点）。

图行为全部用 FakeGraph 打桩（真实图行为在 test_chat_graph.py 覆盖），
这里只验证路由契约：鉴权、参数校验、归属校验、幂等回放、
原子配额、失败退款、删盘级联。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)

import app as app_module
import chat_graph
import db
from tests.test_ai_productization import SAMPLE_PAIPAN


TOOL_CHUNK = AIMessageChunk(
    content='',
    tool_call_chunks=[{'name': 'query_liunian', 'args': '{"year"', 'id': 'c1', 'index': 0}],
)
REPLY_TOKENS = ['丁未年', '，宜守成。']
REPLY_TEXT = ''.join(REPLY_TOKENS)


class FakeSnapshot:
    def __init__(self, values):
        self.values = values


class FakeGraph:
    """打桩 build_chat_graph 的返回值：记录 stream/get_state 调用。"""

    def __init__(self, events=None, error=None, state_values=None):
        self.events = list(events if events is not None else [
            (TOOL_CHUNK, {}),
            *[(AIMessageChunk(content=t), {}) for t in REPLY_TOKENS],
        ])
        self.error = error
        self.state_values = state_values
        self.stream_calls = []
        self.get_state_calls = []

    def stream(self, inp, config=None, stream_mode=None, **kwargs):
        self.stream_calls.append(
            {'input': inp, 'config': config, 'stream_mode': stream_mode}
        )
        for event in self.events:
            yield event
        if self.error:
            raise self.error

    def get_state(self, config=None):
        self.get_state_calls.append(config)
        return FakeSnapshot(self.state_values or {})


def parse_sse(text):
    """把 SSE 文本解析为事件 dict 列表。"""
    events = []
    for block in text.split('\n\n'):
        block = block.strip()
        if not block.startswith('data: '):
            continue
        events.append(json.loads(block[len('data: '):]))
    return events


class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'xuanjige.db')
        db.init_db()

        self.client = app_module.app.test_client()
        self.token, _ = db.register('alice', 'pass1234')
        self.fingerprint = db.get_account_fingerprint(self.token)
        self.hid = self._save_history()

        self.fake = FakeGraph()
        self.build_patcher = patch.object(
            chat_graph, 'build_chat_graph', return_value=self.fake
        )
        self.build_patcher.start()
        # 删盘级联会调 delete_chat_thread → get_checkpointer（生产单例），
        # 测试打桩记录调用即可，不碰真实 data/chat.db
        self.delete_thread_calls = []
        self.delete_patcher = patch.object(
            chat_graph,
            'delete_chat_thread',
            side_effect=self.delete_thread_calls.append,
        )
        self.delete_patcher.start()

    def tearDown(self):
        self.delete_patcher.stop()
        self.build_patcher.stop()
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _save_history(self):
        db.save_history(
            self.fingerprint, '张三', 'male',
            SAMPLE_PAIPAN['solar_date'],
            json.dumps(SAMPLE_PAIPAN, ensure_ascii=False),
        )
        return db.get_history(self.fingerprint)[0]['id']

    def _post_chat(self, message='2027年我的流年如何？', request_id='req-001',
                   hid=None, token='sentinel', persona=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = f'Bearer {self.token if token == "sentinel" else token}'
        payload = {'hid': self.hid if hid is None else hid,
                   'message': message, 'request_id': request_id}
        if persona is not None:
            payload['persona'] = persona
        resp = self.client.post(
            '/api/chat',
            json=payload,
            headers=headers,
        )
        # 流式响应的 generator 是惰性的：消费 body 才会真正执行
        # （包括 save_chat_reply / refund 等收尾动作）
        resp.get_data(as_text=True)
        return resp

    # ---------- 鉴权与参数校验 ----------

    def test_requires_login(self):
        resp = self._post_chat(token=None)
        self.assertEqual(401, resp.status_code)
        self.assertTrue(resp.get_json()['need_login'])

    def test_invalid_token_rejected(self):
        resp = self._post_chat(token='not-a-real-token')
        self.assertEqual(401, resp.status_code)

    def test_missing_message_rejected(self):
        resp = self._post_chat(message='   ')
        self.assertEqual(400, resp.status_code)
        self.assertIn('追问内容', resp.get_json()['message'])

    def test_missing_request_id_rejected(self):
        resp = self._post_chat(request_id='')
        self.assertEqual(400, resp.status_code)
        self.assertIn('request_id', resp.get_json()['message'])

    def test_overlong_message_rejected(self):
        resp = self._post_chat(message='问' * (app_module.CHAT_MAX_INPUT_CHARS + 1))
        self.assertEqual(400, resp.status_code)
        self.assertEqual('输入过长', resp.get_json()['error'])

    def test_invalid_hid_rejected(self):
        self.assertEqual(400, self._post_chat(hid=-1).status_code)
        self.assertEqual(400, self._post_chat(hid=0).status_code)

    def test_hid_not_owned_returns_404(self):
        resp = self._post_chat(hid=9999)
        self.assertEqual(404, resp.status_code)
        self.assertEqual([], self.fake.stream_calls)

    # ---------- persona 参数（persona-plan §5.2，M3） ----------

    def test_unknown_persona_rejected_before_quota(self):
        resp = self._post_chat(persona='huangdi')
        self.assertEqual(400, resp.status_code)
        self.assertIn('道长', resp.get_json()['message'])
        # 白名单校验在扣费之前：香火未动
        self.assertEqual(10, db.get_chat_quota_left(self.hid, self.fingerprint))

    def test_persona_case_sensitive_rejected(self):
        self.assertEqual(400, self._post_chat(persona='QINGXU').status_code)

    def test_non_string_persona_rejected(self):
        resp = self._post_chat(persona={'id': 'tiekou'})
        self.assertEqual(400, resp.status_code)

    def test_blank_persona_falls_back_to_default(self):
        """旧客户端不传/传空串：缺省道长（清虚），行为与 M2 之前完全一致。"""
        resp = self._post_chat(persona='')
        self.assertEqual(200, resp.status_code)
        self.assertEqual(8, db.get_chat_quota_left(self.hid, self.fingerprint))
        self.assertEqual(
            'qingxu', chat_graph.build_chat_graph.call_args.kwargs['persona_id']
        )

    def test_build_graph_receives_persona_id(self):
        self._post_chat(persona='zhangmen')
        self.assertEqual(
            'zhangmen', chat_graph.build_chat_graph.call_args.kwargs['persona_id']
        )
        # 请求体其他字段照常透传
        kwargs = chat_graph.build_chat_graph.call_args.kwargs
        self.assertEqual('张三', kwargs['name'])

    # ---------- 正常流 ----------

    def test_happy_path_stream(self):
        resp = self._post_chat()

        self.assertEqual(200, resp.status_code)
        self.assertIn('text/event-stream', resp.headers['Content-Type'])
        # 缺省道长 2 炷/问，10 炷余额扣后剩 8
        self.assertEqual('8', resp.headers['X-Quota-Left'])

        events = parse_sse(resp.get_data(as_text=True))
        self.assertEqual(
            [{'type': 'tool', 'name': 'query_liunian'},
             {'type': 'token', 'text': REPLY_TOKENS[0]},
             {'type': 'token', 'text': REPLY_TOKENS[1]},
             {'type': 'done', 'quota_left': 8, 'price': 2, 'persona': 'qingxu',
              'request_id': 'req-001', 'cached': False}],
            events,
        )

        # 图收到 dict 形式 human 消息 + 服务端拼接的 thread_id
        self.assertEqual(1, len(self.fake.stream_calls))
        call = self.fake.stream_calls[0]
        self.assertEqual(
            call['input']['messages'], [{'role': 'user', 'content': '2027年我的流年如何？'}]
        )
        self.assertEqual(call['input']['chat_quota_left'], 8)
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        self.assertEqual(call['config']['configurable']['thread_id'], thread_id)
        self.assertEqual(call['stream_mode'], 'messages')

        # 香火已扣 2 炷，回复已落幂等表（含实扣与人格账目）
        self.assertEqual(8, db.get_chat_quota_left(self.hid, self.fingerprint))
        saved = db.get_chat_reply(thread_id, 'req-001')
        self.assertEqual(REPLY_TEXT, saved['reply_text'])
        self.assertEqual(8, saved['quota_left'])
        self.assertEqual(2, saved['price'])
        self.assertEqual('qingxu', saved['persona'])
        log = db.get_chat_usage_logs(thread_id)[0]
        self.assertEqual('ok', log['status'])
        self.assertEqual('qingxu', log['persona'])
        self.assertEqual(2, log['price'])
        self.assertEqual(8, log['quota_left'])
        self.assertEqual(0, log['cached'])
        self.assertEqual(1, log['tool_calls'])
        self.assertEqual(0, log['safety_triggered'])

    def test_usage_metadata_logged_without_sse_leak(self):
        """P3 token 入账：usage-only chunk 不外发，只进入 usage 账本。"""
        self.fake = FakeGraph(events=[
            (AIMessageChunk(
                content='',
                usage_metadata={'input_tokens': 11, 'output_tokens': 22, 'total_tokens': 33},
            ), {}),
            (AIMessageChunk(content='丁未年'), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        events = parse_sse(resp.get_data(as_text=True))
        self.assertEqual(
            [{'type': 'token', 'text': '丁未年'},
             {'type': 'done', 'quota_left': 8, 'price': 2, 'persona': 'qingxu',
              'request_id': 'req-001', 'cached': False}],
            events,
        )
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        log = db.get_chat_usage_logs(thread_id)[0]
        self.assertEqual(11, log['prompt_tokens'])
        self.assertEqual(22, log['completion_tokens'])
        self.assertEqual(33, log['total_tokens'])
        self.assertGreater(log['cost_usd'], 0)

        conn = sqlite3.connect(db.DB_PATH)
        usage = conn.execute(
            "SELECT endpoint, cache_hit, tokens_used, cost_usd FROM usage_logs "
            "WHERE endpoint = '/api/chat' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(('/api/chat', 0, 33), usage[:3])
        self.assertGreater(usage[3], 0)

    def test_persona_charged_its_own_price(self):
        """请教掌门真人按注册表定价扣 5 炷，done 事件如实记账。"""
        resp = self._post_chat(persona='zhangmen')

        self.assertEqual(200, resp.status_code)
        self.assertEqual(5, db.get_chat_quota_left(self.hid, self.fingerprint))
        events = parse_sse(resp.get_data(as_text=True))
        done = events[-1]
        self.assertEqual(5, done['price'])
        self.assertEqual('zhangmen', done['persona'])
        self.assertEqual(5, done['quota_left'])

        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        saved = db.get_chat_reply(thread_id, 'req-001')
        self.assertEqual(5, saved['price'])
        self.assertEqual('zhangmen', saved['persona'])

    def test_mixed_personas_sum_up_correctly(self):
        """混请教不同价位的道长，余额按各自定价累计扣减。"""
        self._post_chat(persona='xuanzhen')              # 1 炷
        self._post_chat(persona='zhangmen', request_id='req-002')  # 5 炷
        self._post_chat(persona='tiekou', request_id='req-003')    # 2 炷
        # 10 - 1 - 5 - 2 = 2 炷
        self.assertEqual(2, db.get_chat_quota_left(self.hid, self.fingerprint))

    def test_superuser_chat_does_not_consume_quota(self):
        self.assertTrue(db.set_account_superuser('alice'))

        resp = self._post_chat(persona='zhangmen')

        self.assertEqual(200, resp.status_code)
        self.assertEqual(str(db.SUPERUSER_CHAT_QUOTA_LEFT), resp.headers['X-Quota-Left'])
        self.assertEqual('true', resp.headers['X-Unlimited-Quota'])
        self.assertEqual(
            db.SUPERUSER_CHAT_QUOTA_LEFT,
            db.get_chat_quota_left(self.hid, self.fingerprint),
        )
        events = parse_sse(resp.get_data(as_text=True))
        done = events[-1]
        self.assertTrue(done['unlimited'])
        self.assertEqual(db.SUPERUSER_CHAT_QUOTA_LEFT, done['quota_left'])
        self.assertEqual(5, done['price'])
        self.assertEqual('zhangmen', done['persona'])
        self.assertEqual(
            db.SUPERUSER_CHAT_QUOTA_LEFT,
            self.fake.stream_calls[0]['input']['chat_quota_left'],
        )

        conn = db.get_db()
        row = conn.execute(
            'SELECT used FROM chat_followups WHERE hid = ? AND fingerprint = ?',
            (self.hid, self.fingerprint),
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_superuser_chat_quota_endpoint_reports_unlimited(self):
        self.assertTrue(db.set_account_superuser('alice'))

        resp = self.client.get(
            f'/api/chat/quota?hid={self.hid}',
            headers={'Authorization': f'Bearer {self.token}'},
        )

        self.assertEqual(200, resp.status_code)
        self.assertEqual({
            'hid': self.hid,
            'quota_left': db.SUPERUSER_CHAT_QUOTA_LEFT,
            'limit': db.CHAT_FREE_INCENSE,
            'unlimited': True,
        }, resp.get_json())
    def test_idempotent_replay_does_not_recharge_or_restream(self):
        self._post_chat()
        used_before = db.get_chat_quota_left(self.hid, self.fingerprint)

        resp = self._post_chat()  # 同 request_id 重发

        self.assertEqual(200, resp.status_code)
        self.assertEqual('true', resp.headers['X-Cache-Hit'])
        self.assertEqual('8', resp.headers['X-Quota-Left'])
        # 图未被再次调用、香火未再扣
        self.assertEqual(1, len(self.fake.stream_calls))
        self.assertEqual(used_before, db.get_chat_quota_left(self.hid, self.fingerprint))

        events = parse_sse(resp.get_data(as_text=True))
        tokens = [e['text'] for e in events if e['type'] == 'token']
        self.assertEqual(REPLY_TEXT, ''.join(tokens))
        done = [e for e in events if e['type'] == 'done']
        self.assertEqual(1, len(done))
        self.assertTrue(done[0]['cached'])
        self.assertEqual(8, done[0]['quota_left'])
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        log = db.get_chat_usage_logs(thread_id)[0]
        self.assertEqual('replay', log['status'])
        self.assertEqual(1, log['cached'])
        self.assertEqual('qingxu', log['persona'])

    def test_replay_done_carries_cached_price_and_persona(self):
        """断线重发同 request_id：回放的是当时那位道长的回复与账目（§5.3）。"""
        self._post_chat(persona='tiekou')

        # 重发前换了道长也只回放：幂等表契约是不改写历史
        resp = self._post_chat(persona='baiyun')

        self.assertEqual(200, resp.status_code)
        events = parse_sse(resp.get_data(as_text=True))
        done = events[-1]
        self.assertTrue(done['cached'])
        self.assertEqual(2, done['price'])
        self.assertEqual('tiekou', done['persona'])
        # 香火未被再次扣减
        self.assertEqual(8, db.get_chat_quota_left(self.hid, self.fingerprint))

    def test_different_request_id_charges_again(self):
        self._post_chat(request_id='req-001')
        resp = self._post_chat(request_id='req-002')
        self.assertEqual(200, resp.status_code)
        # 两次真实追问 × 2 炷 = 4 炷，剩 6
        self.assertEqual(6, db.get_chat_quota_left(self.hid, self.fingerprint))
        self.assertEqual(2, len(self.fake.stream_calls))

    # ---------- 配额与失败路径 ----------

    def test_quota_exhausted_returns_403_without_stream(self):
        # 10 炷按缺省价 2 炷扣尽 = 5 问，与旧"每盘 5 次"等价
        for _ in range(db.CHAT_FREE_INCENSE // 2):
            db.consume_chat_quota(self.hid, self.fingerprint, 2)

        resp = self._post_chat()

        self.assertEqual(403, resp.status_code)
        body = resp.get_json()
        self.assertEqual(0, body['quota_left'])
        self.assertEqual('香火不足', body['error'])
        self.assertEqual(2, body['persona_price'])
        self.assertEqual([], self.fake.stream_calls)
        log = db.get_chat_usage_logs(db.build_chat_thread_id(self.fingerprint, self.hid))[0]
        self.assertEqual('quota_exhausted', log['status'])
        self.assertEqual(0, log['quota_left'])
        self.assertEqual(2, log['price'])

    def test_403_detail_when_price_exceeds_balance(self):
        """余额够问便宜道长、不够请教掌门：403 明细让用户失败得明白（§5.2）。"""
        db.consume_chat_quota(self.hid, self.fingerprint, 7)  # 剩 3 炷 < 掌门 5 炷

        resp = self._post_chat(persona='zhangmen')

        self.assertEqual(403, resp.status_code)
        body = resp.get_json()
        self.assertEqual('香火不足', body['error'])
        self.assertEqual(3, body['quota_left'])
        self.assertEqual(5, body['persona_price'])
        self.assertIn('掌门真人', body['message'])
        self.assertIn('5 炷香', body['message'])
        self.assertIn('3 炷', body['message'])
        # 同余额换便宜道长（玄真 1 炷）可直接通过
        ok_resp = self._post_chat(persona='xuanzhen')
        self.assertEqual(200, ok_resp.status_code)

    def test_ai_error_refunds_quota_and_sends_error_event(self):
        self.fake = FakeGraph(error=RuntimeError('boom'))
        # patcher 已 start：直接改 mock 的 return_value 才会生效
        chat_graph.build_chat_graph.return_value = self.fake
        resp = self._post_chat()

        self.assertEqual(200, resp.status_code)  # SSE 已开流，错误在流内表达
        events = parse_sse(resp.get_data(as_text=True))
        self.assertEqual({'type': 'error', 'message': events[-1]['message']}, events[-1])
        self.assertEqual('error', events[-1]['type'])

        # 香火已按实扣退款；无回复落库（重发可重试）
        self.assertEqual(10, db.get_chat_quota_left(self.hid, self.fingerprint))
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        self.assertIsNone(db.get_chat_reply(thread_id, 'req-001'))
        log = db.get_chat_usage_logs(thread_id)[0]
        self.assertEqual('error', log['status'])
        self.assertEqual('RuntimeError', log['error_type'])
        self.assertEqual(10, log['quota_left'])

    def test_empty_ai_reply_refunds_quota_and_is_not_cached(self):
        self.fake = FakeGraph(events=[
            (AIMessageChunk(
                content='',
                usage_metadata={
                    'input_tokens': 12,
                    'output_tokens': 34,
                    'total_tokens': 46,
                },
            ), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        self.assertEqual(200, resp.status_code)
        events = parse_sse(resp.get_data(as_text=True))
        self.assertEqual('error', events[-1]['type'])
        self.assertEqual(app_module.CHAT_EMPTY_REPLY_MESSAGE, events[-1]['message'])
        self.assertEqual(10, events[-1]['quota_left'])
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        self.assertEqual(10, db.get_chat_quota_left(self.hid, self.fingerprint))
        self.assertIsNone(db.get_chat_reply(thread_id, 'req-001'))
        log = db.get_chat_usage_logs(thread_id)[0]
        self.assertEqual('empty_reply', log['status'])
        self.assertEqual('EmptyReply', log['error_type'])
        self.assertEqual(46, log['total_tokens'])
        self.assertEqual(10, log['quota_left'])

    def test_stream_safety_fuse_blocks_cross_chunk_risky_phrase(self):
        """P3 流中兜底：危险短语跨 token 出现时，完整短语不得外发或落库。"""
        self.fake = FakeGraph(events=[
            (AIMessageChunk(content='这事'), {}),
            (AIMessageChunk(content='稳赚'), {}),
            (AIMessageChunk(content='不赔，别犹豫。'), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        self.assertEqual(200, resp.status_code)
        events = parse_sse(resp.get_data(as_text=True))
        text = ''.join(e.get('text', '') for e in events if e['type'] == 'token')
        self.assertIn('这事', text)
        self.assertIn('不能下绝对断语', text)
        self.assertNotIn('稳赚', text)
        self.assertNotIn('不赔', text)
        self.assertEqual('done', events[-1]['type'])
        # 安全保险丝截断的是输出，不是 AI 调用失败：不退款，但落安全回复供幂等回放
        self.assertEqual(8, db.get_chat_quota_left(self.hid, self.fingerprint))
        saved = db.get_chat_reply(db.build_chat_thread_id(self.fingerprint, self.hid), 'req-001')
        self.assertIsNotNone(saved)
        self.assertIn('不能下绝对断语', saved['reply_text'])
        self.assertNotIn('稳赚', saved['reply_text'])
        log = db.get_chat_usage_logs(db.build_chat_thread_id(self.fingerprint, self.hid))[0]
        self.assertEqual('safety', log['status'])
        self.assertEqual(1, log['safety_triggered'])
        self.assertEqual(0, log['cached'])

    def test_stream_safety_fuse_blocks_single_chunk_risky_phrase(self):
        self.fake = FakeGraph(events=[
            (AIMessageChunk(content='你必定发财，这事不用犹豫。'), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        events = parse_sse(resp.get_data(as_text=True))
        text = ''.join(e.get('text', '') for e in events if e['type'] == 'token')
        self.assertIn('不能下绝对断语', text)
        self.assertNotIn('必定发财', text)
        self.assertEqual('done', events[-1]['type'])

    def test_grounding_allows_retrieved_chunk_id(self):
        self.fake = FakeGraph(events=[
            (AIMessageChunk(
                content='',
                tool_call_chunks=[{
                    'name': 'search_classics',
                    'args': '{"query"',
                    'id': 'call_search',
                    'index': 0,
                }],
            ), {}),
            (ToolMessage(
                content='1. chunk_id=ditiansui-chanwei-ch029；可引用格式=【ditiansui-chanwei-ch029】',
                name='search_classics',
                tool_call_id='call_search',
            ), {}),
            (AIMessageChunk(content='寒暖一节见【ditiansui-chanwei-ch029】，只作义理参考。'), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        events = parse_sse(resp.get_data(as_text=True))
        text = ''.join(e.get('text', '') for e in events if e['type'] == 'token')
        self.assertIn({'type': 'tool', 'name': 'search_classics'}, events)
        self.assertIn('【ditiansui-chanwei-ch029】', text)
        saved = db.get_chat_reply(db.build_chat_thread_id(self.fingerprint, self.hid), 'req-001')
        self.assertIn('【ditiansui-chanwei-ch029】', saved['reply_text'])
        self.assertEqual(['ditiansui-chanwei-ch029'], saved['retrieved_chunk_ids'])
        log = db.get_chat_usage_logs(db.build_chat_thread_id(self.fingerprint, self.hid))[0]
        self.assertEqual('ok', log['status'])
        self.assertEqual(1, log['tool_calls'])
        self.assertEqual(0, log['safety_triggered'])

    def test_grounding_fuse_blocks_cross_chunk_fake_chunk_id(self):
        self.fake = FakeGraph(events=[
            (AIMessageChunk(content='寒暖可见【fake-'), {}),
            (AIMessageChunk(content='book-ch001】，照此断。'), {}),
        ])
        chat_graph.build_chat_graph.return_value = self.fake

        resp = self._post_chat()

        events = parse_sse(resp.get_data(as_text=True))
        text = ''.join(e.get('text', '') for e in events if e['type'] == 'token')
        self.assertIn(app_module.CHAT_GROUNDING_FALLBACK, text)
        self.assertNotIn('fake-book-ch001', text)
        self.assertEqual('done', events[-1]['type'])
        self.assertEqual(8, db.get_chat_quota_left(self.hid, self.fingerprint))
        saved = db.get_chat_reply(db.build_chat_thread_id(self.fingerprint, self.hid), 'req-001')
        self.assertNotIn('fake-book-ch001', saved['reply_text'])
        self.assertIn(app_module.CHAT_GROUNDING_FALLBACK, saved['reply_text'])
        self.assertEqual([], saved['retrieved_chunk_ids'])
        log = db.get_chat_usage_logs(db.build_chat_thread_id(self.fingerprint, self.hid))[0]
        self.assertEqual('safety', log['status'])
        self.assertEqual(1, log['safety_triggered'])

    def test_chat_config_error_returns_503(self):
        self.build_patcher.stop()
        with patch.object(
            chat_graph, 'build_chat_graph',
            side_effect=chat_graph.ChatConfigError('no key'),
        ):
            resp = self._post_chat()
        self.build_patcher.start()

        self.assertEqual(503, resp.status_code)
        # 配置缺失不应扣香火
        self.assertEqual(10, db.get_chat_quota_left(self.hid, self.fingerprint))

    # ---------- 配套端点 ----------

    def test_chat_quota_endpoint(self):
        resp = self.client.get(
            f'/api/chat/quota?hid={self.hid}',
            headers={'Authorization': f'Bearer {self.token}'},
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual(
            {'hid': self.hid, 'quota_left': 10, 'limit': db.CHAT_FREE_INCENSE},
            resp.get_json(),
        )

    def test_chat_quota_endpoint_requires_login(self):
        resp = self.client.get(f'/api/chat/quota?hid={self.hid}')
        self.assertEqual(401, resp.status_code)

    # ---------- 道长花名册（persona-plan §5.1，M3） ----------

    def _get_personas(self, hid=None, token='sentinel'):
        url = '/api/chat/personas'
        if hid is not None:
            url += f'?hid={hid}'
        headers = {}
        if token is not None:
            headers['Authorization'] = f'Bearer {self.token if token == "sentinel" else token}'
        return self.client.get(url, headers=headers)

    def test_personas_endpoint_requires_login(self):
        self.assertEqual(401, self._get_personas(token=None).status_code)

    def test_personas_catalog_without_hid(self):
        """纯花名册：不查余额全 affordable，current 落缺省道长。"""
        resp = self._get_personas()
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(5, len(body))
        # 顺序 = 注册顺序 = 前端卡片顺序（产品契约）
        self.assertEqual(
            ['xuanzhen', 'qingxu', 'baiyun', 'tiekou', 'zhangmen'],
            [p['persona_id'] for p in body],
        )
        by_id = {p['persona_id']: p for p in body}
        self.assertEqual(1, by_id['xuanzhen']['price'])
        self.assertEqual(5, by_id['zhangmen']['price'])
        self.assertEqual('江湖直白，结论先行', by_id['xuanzhen']['tagline'])
        self.assertTrue(all(p['affordable'] for p in body))
        # 无历史记录：current = 缺省（清虚）
        self.assertEqual(
            ['qingxu'], [p['persona_id'] for p in body if p['current']]
        )

    def test_personas_with_hid_marks_affordable_by_balance(self):
        db.consume_chat_quota(self.hid, self.fingerprint, 8)  # 剩 2 炷

        resp = self._get_personas(hid=self.hid)
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        by_id = {p['persona_id']: p for p in body}
        # 2 炷余额：≤2 炷的道长可请教，掌门（5）置灰
        self.assertTrue(by_id['xuanzhen']['affordable'])
        self.assertTrue(by_id['qingxu']['affordable'])
        self.assertFalse(by_id['zhangmen']['affordable'])

    def test_personas_current_follows_last_persona(self):
        """请教过铁口后，花名册 current 跟随最近请教的道长（刷新恢复选中的依据）。"""
        self._post_chat(persona='tiekou')

        resp = self._get_personas(hid=self.hid)
        body = resp.get_json()
        self.assertEqual(
            ['tiekou'], [p['persona_id'] for p in body if p['current']]
        )

    def test_personas_current_falls_back_to_default_when_unknown(self):
        """历史 persona 已下架（白名单外）：current 回落缺省，不 500 不卡死。"""
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        db.save_chat_reply(thread_id, 'legacy-req', '旧回复', 8, 2, 'huangdi')

        resp = self._get_personas(hid=self.hid)
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertEqual(
            ['qingxu'], [p['persona_id'] for p in body if p['current']]
        )

    def test_personas_with_hid_validates_ownership(self):
        self.assertEqual(400, self._get_personas(hid=0).status_code)
        self.assertEqual(404, self._get_personas(hid=9999).status_code)

    def test_chat_history_endpoint_filters_tool_traffic(self):
        self.fake.state_values = {
            'messages': [
                HumanMessage('2027年我的流年如何？'),
                AIMessage(content='', tool_calls=[{
                    'id': 'c1', 'name': 'query_liunian', 'args': {'year': 2027},
                }]),
                ToolMessage(content='{"year":2027}', name='query_liunian', tool_call_id='c1'),
                AIMessage(content='丁未年，宜守成。'),
            ]
        }
        resp = self.client.get(
            f'/api/chat/history?hid={self.hid}',
            headers={'Authorization': f'Bearer {self.token}'},
        )

        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        # 只回放 human + AI 终稿；工具调用与工具结果不外发
        self.assertEqual([
            {'role': 'user', 'content': '2027年我的流年如何？'},
            {'role': 'assistant', 'content': '丁未年，宜守成。'},
        ], body['messages'])
        self.assertEqual(10, body['quota_left'])
        # get_state 用的是服务端拼接的 thread_id
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        self.assertEqual(
            thread_id, self.fake.get_state_calls[0]['configurable']['thread_id']
        )

    def test_chat_history_endpoint_requires_login(self):
        resp = self.client.get(f'/api/chat/history?hid={self.hid}')
        self.assertEqual(401, resp.status_code)

    # ---------- 排盘返回 hid（chat 会话的入口） ----------

    def test_paipan_response_carries_history_id(self):
        resp = self.client.post(
            '/api/paipan',
            json={'name': '李四', 'year': 1990, 'month': 1, 'day': 1,
                  'hour': 10, 'minute': 30, 'gender': 'male'},
            headers={'Authorization': f'Bearer {self.token}'},
        )
        self.assertEqual(200, resp.status_code)
        body = resp.get_json()
        self.assertIn('history_id', body)
        # 返回的 history_id 归属当前账号，可直接用于 chat
        self.assertEqual(
            body['history_id'],
            db.get_history(self.fingerprint)[0]['id'],
        )

    # ---------- 删盘级联 ----------

    def test_delete_history_cascades_chat_data(self):
        self._post_chat()
        thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        self.assertIsNotNone(db.get_chat_reply(thread_id, 'req-001'))

        resp = self.client.delete(
            f'/api/history/{self.hid}',
            headers={'Authorization': f'Bearer {self.token}'},
        )

        self.assertEqual(200, resp.status_code)
        # 追问香火行、幂等记录、checkpoint 线程全部清除
        self.assertEqual(10, db.get_chat_quota_left(self.hid, self.fingerprint))
        self.assertIsNone(db.get_chat_reply(thread_id, 'req-001'))
        self.assertEqual([thread_id], self.delete_thread_calls)


if __name__ == '__main__':
    unittest.main()
