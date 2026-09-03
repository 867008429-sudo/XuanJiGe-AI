import os
import sqlite3
import tempfile
import unittest

import db


def _default_price():
    """缺省道长（清虚）定价：测试与路由层共用的锚点。"""
    from chat_personas import DEFAULT_PERSONA_ID, PERSONAS
    return PERSONAS[DEFAULT_PERSONA_ID].price_incense


class ChatQuotaTests(unittest.TestCase):
    """chat_followups 香火计量：原子扣减 / 退款 / 剩余查询（persona-plan §3.1）。"""

    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_consume_creates_row_and_counts(self):
        price = _default_price()
        ok, used = db.consume_chat_quota(101, 'acct:1', price)
        self.assertTrue(ok)
        self.assertEqual(used, price)
        self.assertEqual(
            db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE - price
        )

    def test_consume_to_exhaustion_then_refused(self):
        price = _default_price()
        turns = db.CHAT_FREE_INCENSE // price  # 10 炷 ÷ 2 = 5 问，与旧条数语义等价
        for i in range(1, turns + 1):
            ok, used = db.consume_chat_quota(101, 'acct:1', price)
            self.assertTrue(ok)
            self.assertEqual(used, i * price)
        # 超出额度：拒绝且不超卖（used 不越过 limit）
        ok, used = db.consume_chat_quota(101, 'acct:1', price)
        self.assertFalse(ok)
        self.assertEqual(used, db.CHAT_FREE_INCENSE)
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), 0)

    def test_mixed_prices_no_overdraft(self):
        # 玄真 1 炷 + 掌门 5 炷 + 铁口 3 炷混用恰好花尽 10，之后 1 炷也拒绝
        for price in (1, 5, 1, 3):
            ok, used = db.consume_chat_quota(101, 'acct:1', price)
            self.assertTrue(ok)
        self.assertEqual(0, db.get_chat_quota_left(101, 'acct:1'))
        ok, used = db.consume_chat_quota(101, 'acct:1', 1)
        self.assertFalse(ok)
        self.assertEqual(used, db.CHAT_FREE_INCENSE)

    def test_insufficient_balance_partial_price_refused_atomically(self):
        # 余额 2 炷请教掌门（5 炷）：整单拒绝，绝不部分扣减
        ok, used = db.consume_chat_quota(101, 'acct:1', 8)
        self.assertTrue(ok)
        ok, used = db.consume_chat_quota(101, 'acct:1', 5)
        self.assertFalse(ok)
        self.assertEqual(used, 8)
        self.assertEqual(2, db.get_chat_quota_left(101, 'acct:1'))

    def test_refund_restores_actual_price(self):
        price = _default_price()
        db.consume_chat_quota(101, 'acct:1', price)
        refunded = db.refund_chat_quota(101, 'acct:1', price)
        self.assertTrue(refunded)
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE)

    def test_refund_high_price_after_partial_uses(self):
        # 连扣 2 问后退掌门价：守卫按实扣退，不清空他人账目
        db.consume_chat_quota(101, 'acct:1', 2)
        db.consume_chat_quota(101, 'acct:1', 2)
        self.assertTrue(db.refund_chat_quota(101, 'acct:1', 2))
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE - 2)

    def test_refund_never_goes_negative_or_over_refunds(self):
        # 未扣过就退：rowcount=0
        self.assertFalse(db.refund_chat_quota(101, 'acct:1', 2))
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE)
        # 扣 2 退 5：超额退款被守卫拒绝，used 原地不动
        db.consume_chat_quota(101, 'acct:1', 2)
        self.assertFalse(db.refund_chat_quota(101, 'acct:1', 5))
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE - 2)

    def test_invalid_price_raises(self):
        # 定价来自白名单注册表，非法值是编程错误：抛错不静默
        with self.assertRaises(ValueError):
            db.consume_chat_quota(101, 'acct:1', 0)
        with self.assertRaises(ValueError):
            db.consume_chat_quota(101, 'acct:1', -2)
        with self.assertRaises(ValueError):
            db.refund_chat_quota(101, 'acct:1', 0)

    def test_quota_isolated_per_hid_and_account(self):
        db.consume_chat_quota(101, 'acct:1', 2)
        db.consume_chat_quota(101, 'acct:1', 3)
        db.consume_chat_quota(102, 'acct:1', 1)
        db.consume_chat_quota(101, 'acct:2', 5)
        self.assertEqual(db.get_chat_quota_left(101, 'acct:1'), db.CHAT_FREE_INCENSE - 5)
        self.assertEqual(db.get_chat_quota_left(102, 'acct:1'), db.CHAT_FREE_INCENSE - 1)
        self.assertEqual(db.get_chat_quota_left(101, 'acct:2'), db.CHAT_FREE_INCENSE - 5)

    def test_exhausted_quota_refund_then_usable_again(self):
        price = _default_price()
        for _ in range(db.CHAT_FREE_INCENSE // price):
            db.consume_chat_quota(101, 'acct:1', price)
        db.refund_chat_quota(101, 'acct:1', price)
        ok, used = db.consume_chat_quota(101, 'acct:1', price)
        self.assertTrue(ok)
        self.assertEqual(used, db.CHAT_FREE_INCENSE)

    def test_wal_mode_enabled(self):
        conn = db.get_db()
        mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), 'wal')


class ChatIncenseMigrationTests(unittest.TestCase):
    """条数→炷数存量迁移（persona-plan §3.2）：换算、幂等、新库直通。"""

    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _create_old_schema(self):
        """手工建旧版两张表（无 price/persona 列），模拟升级前存量库。"""
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript('''
            CREATE TABLE chat_followups (
                hid INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (hid, fingerprint)
            );
            CREATE TABLE chat_requests (
                thread_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                quota_left INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (thread_id, request_id)
            );
        ''')
        conn.execute(
            'INSERT INTO chat_followups (hid, fingerprint, used) VALUES (101, ?, 3)',
            ('acct:1',)
        )
        conn.execute(
            'INSERT INTO chat_requests (thread_id, request_id, reply_text, quota_left) '
            "VALUES ('acct:1:101', 'req-old-1', '旧回复', 2)"
        )
        conn.commit()
        conn.close()

    def test_migration_converts_and_is_idempotent(self):
        self._create_old_schema()
        db.init_db()  # 第一次：加列 + used*2 + quota_left*2

        # used=3 条 → 6 炷；剩 4 炷 = 还能按缺省价问 2 次，与旧"剩 2 次"等价
        self.assertEqual(4, db.get_chat_quota_left(101, 'acct:1'))
        conn = sqlite3.connect(db.DB_PATH)
        followup = conn.execute(
            'SELECT used FROM chat_followups WHERE hid = 101'
        ).fetchone()
        old_reply = conn.execute(
            'SELECT quota_left, price, persona FROM chat_requests '
            "WHERE request_id = 'req-old-1'"
        ).fetchone()
        conn.close()
        self.assertEqual(6, followup[0])
        self.assertEqual(4, old_reply[0])          # quota_left 2 → 4
        self.assertEqual(2, old_reply[1])          # price 列默认 2（缺省道长价）
        self.assertEqual('qingxu', old_reply[2])   # persona 默认（旧时代即清虚风格）

        db.init_db()  # 第二次：标记已存在，全部跳过
        conn = sqlite3.connect(db.DB_PATH)
        followup2 = conn.execute(
            'SELECT used FROM chat_followups WHERE hid = 101'
        ).fetchone()
        conn.close()
        self.assertEqual(6, followup2[0])  # 未被二次翻倍

    def test_fresh_database_has_columns_without_migration(self):
        db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(chat_requests)')}
        conn.close()
        self.assertIn('price', cols)
        self.assertIn('persona', cols)
        # 新库无存量行：used*2 是空操作，quota_left 全量保持 10
        self.assertEqual(db.CHAT_FREE_INCENSE, db.get_chat_quota_left(1, 'acct:x'))

    def test_migrated_rows_are_readable_by_new_api(self):
        self._create_old_schema()
        db.init_db()
        saved = db.get_chat_reply('acct:1:101', 'req-old-1')
        self.assertEqual('旧回复', saved['reply_text'])
        self.assertEqual(4, saved['quota_left'])
        self.assertEqual(2, saved['price'])
        self.assertEqual('qingxu', saved['persona'])


class ChatRequestTests(unittest.TestCase):
    """chat_requests 幂等表 + 删盘级联（对齐 agent-plan §5；price/persona 记录账目）。"""

    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_thread_id_is_server_side_composition(self):
        self.assertEqual(db.build_chat_thread_id('acct:1', 42), 'acct:1:42')

    def test_reply_roundtrip_and_replay(self):
        thread = db.build_chat_thread_id('acct:1', 42)
        db.save_chat_reply(thread, 'req-uuid-1', '丁未年宜守不宜攻', 3, 2, 'tiekou')
        saved = db.get_chat_reply(thread, 'req-uuid-1')
        self.assertEqual(saved['reply_text'], '丁未年宜守不宜攻')
        self.assertEqual(saved['quota_left'], 3)
        self.assertEqual(saved['price'], 2)
        self.assertEqual(saved['persona'], 'tiekou')

    def test_reply_is_write_once_never_overwritten(self):
        # 审查修正后的语义：首次写入后重发不覆盖（回放永远拿首次回复与首次账目）
        thread = db.build_chat_thread_id('acct:1', 42)
        db.save_chat_reply(thread, 'req-uuid-1', '首次回复', 3, 2, 'qingxu')
        db.save_chat_reply(thread, 'req-uuid-1', '后来的覆盖尝试', 0, 5, 'zhangmen')
        saved = db.get_chat_reply(thread, 'req-uuid-1')
        self.assertEqual(saved['reply_text'], '首次回复')
        self.assertEqual(saved['quota_left'], 3)
        self.assertEqual(saved['price'], 2)
        self.assertEqual(saved['persona'], 'qingxu')

    def test_unknown_request_returns_none(self):
        self.assertIsNone(db.get_chat_reply('acct:1:42', 'req-never-seen'))

    def test_replay_is_isolated_per_thread(self):
        # 同一 request_id 出现在别的 thread 时不串扰
        db.save_chat_reply('acct:1:42', 'req-uuid-1', '盘A的回复', 3, 2, 'qingxu')
        self.assertIsNone(db.get_chat_reply('acct:1:43', 'req-uuid-1'))

    def test_last_chat_persona_empty_thread_returns_none(self):
        self.assertIsNone(db.get_last_chat_persona('acct:1:42'))

    def test_last_chat_persona_follows_latest_reply(self):
        """花名册 current 的数据源：最近一次落账的道长（persona-plan §5.1）。"""
        thread = db.build_chat_thread_id('acct:1', 42)
        db.save_chat_reply(thread, 'req-1', '回复1', 4, 2, 'qingxu')
        db.save_chat_reply(thread, 'req-2', '回复2', 3, 5, 'zhangmen')
        self.assertEqual('zhangmen', db.get_last_chat_persona(thread))

    def test_last_chat_persona_isolated_per_thread(self):
        # 别的 thread 的最近记录不串扰
        db.save_chat_reply('acct:1:42', 'req-1', '回复', 4, 2, 'tiekou')
        self.assertIsNone(db.get_last_chat_persona('acct:1:43'))

    def test_last_chat_persona_cleared_by_delete_cascade(self):
        thread = db.build_chat_thread_id('acct:1', 42)
        db.save_chat_reply(thread, 'req-1', '回复', 4, 2, 'tiekou')
        db.delete_chat_cascade('acct:1', 42)
        self.assertIsNone(db.get_last_chat_persona(thread))

    def test_delete_cascade_clears_quota_and_requests(self):
        fp = 'acct:1'
        hid = 42
        thread = db.build_chat_thread_id(fp, hid)
        db.consume_chat_quota(hid, fp, 2)
        db.save_chat_reply(thread, 'req-1', '回复1', 4, 2, 'qingxu')
        db.save_chat_reply(thread, 'req-2', '回复2', 3, 2, 'tiekou')
        # 别的盘/账号的数据不应被误删
        db.consume_chat_quota(43, fp, 1)
        db.save_chat_reply(db.build_chat_thread_id(fp, 43), 'req-x', '别盘回复', 4, 1, 'xuanzhen')
        db.save_chat_reply(db.build_chat_thread_id('acct:2', hid), 'req-y', '别账号回复', 4, 5, 'zhangmen')

        deleted = db.delete_chat_cascade(fp, hid)

        self.assertEqual(deleted, 3)  # 1 配额行 + 2 幂等行
        self.assertEqual(db.get_chat_quota_left(hid, fp), db.CHAT_FREE_INCENSE)
        self.assertIsNone(db.get_chat_reply(thread, 'req-1'))
        self.assertIsNone(db.get_chat_reply(thread, 'req-2'))
        # 邻近数据保留
        self.assertEqual(db.get_chat_quota_left(43, fp), db.CHAT_FREE_INCENSE - 1)
        self.assertIsNotNone(db.get_chat_reply(db.build_chat_thread_id(fp, 43), 'req-x'))
        self.assertIsNotNone(db.get_chat_reply(db.build_chat_thread_id('acct:2', hid), 'req-y'))

    def test_delete_cascade_is_idempotent(self):
        self.assertEqual(db.delete_chat_cascade('acct:9', 999), 0)


if __name__ == '__main__':
    unittest.main()
