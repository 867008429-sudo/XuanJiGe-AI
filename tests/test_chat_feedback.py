import json
import os
import sqlite3
import tempfile
import unittest

import app as app_module
import db
from tests.test_ai_productization import SAMPLE_PAIPAN


class ChatFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()
        self.client = app_module.app.test_client()
        self.token, _ = db.register('alice', 'pass1234')
        self.fingerprint = db.get_account_fingerprint(self.token)
        self.hid = self._save_history()
        self.thread_id = db.build_chat_thread_id(self.fingerprint, self.hid)
        db.save_chat_reply(self.thread_id, 'req-1', '这轮回复可评价。', 8, 2, 'qingxu')

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _save_history(self):
        db.save_history(
            self.fingerprint,
            '张三',
            'male',
            SAMPLE_PAIPAN['solar_date'],
            json.dumps(SAMPLE_PAIPAN, ensure_ascii=False),
        )
        return db.get_history(self.fingerprint)[0]['id']

    def _post_feedback(self, payload=None, token=None):
        headers = {}
        if token is None:
            headers['Authorization'] = f'Bearer {self.token}'
        elif token:
            headers['Authorization'] = f'Bearer {token}'
        body = {'hid': self.hid, 'request_id': 'req-1', 'rating': 1}
        if payload:
            body.update(payload)
        return self.client.post('/api/chat/feedback', json=body, headers=headers)

    def test_feedback_requires_login(self):
        resp = self._post_feedback(token='')

        self.assertEqual(401, resp.status_code)
        self.assertTrue(resp.get_json()['need_login'])

    def test_feedback_saves_and_updates_rating(self):
        first = self._post_feedback({'rating': 1})
        second = self._post_feedback({'rating': -1})

        self.assertEqual(200, first.status_code, first.get_data(as_text=True))
        self.assertEqual(200, second.status_code, second.get_data(as_text=True))
        saved = db.get_chat_feedback(self.thread_id, 'req-1')
        self.assertEqual(-1, saved['rating'])
        stats = db.get_chat_feedback_stats()
        self.assertEqual(1, stats['total'])
        self.assertEqual(0, stats['helpful'])
        self.assertEqual(1, stats['unhelpful'])

    def test_feedback_rejects_unknown_reply(self):
        resp = self._post_feedback({'request_id': 'missing-req'})

        self.assertEqual(404, resp.status_code)
        self.assertIsNone(db.get_chat_feedback(self.thread_id, 'missing-req'))

    def test_feedback_rejects_foreign_history(self):
        other_token, _ = db.register('bob', 'pass1234')

        resp = self._post_feedback(token=other_token)

        self.assertEqual(404, resp.status_code)

    def test_feedback_rejects_invalid_rating(self):
        resp = self._post_feedback({'rating': 0})

        self.assertEqual(400, resp.status_code)
        self.assertIsNone(db.get_chat_feedback(self.thread_id, 'req-1'))

    def test_feedback_table_omits_sensitive_columns(self):
        self._post_feedback({'rating': 1})

        conn = sqlite3.connect(db.DB_PATH)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(chat_feedback)')}
        conn.close()
        self.assertIn('rating', cols)
        for forbidden in ('message', 'reply_text', 'paipan_json', 'birth_info', 'solar_date'):
            self.assertNotIn(forbidden, cols)

    def test_delete_cascade_removes_feedback(self):
        self._post_feedback({'rating': 1})

        db.delete_chat_cascade(self.fingerprint, self.hid)

        self.assertIsNone(db.get_chat_feedback(self.thread_id, 'req-1'))


if __name__ == '__main__':
    unittest.main()
