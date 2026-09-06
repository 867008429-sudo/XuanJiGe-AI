import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import chat_cleanup
import db


class ChatCleanupTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _age_thread(self, thread_id, created_at):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(
                'UPDATE chat_requests SET created_at = ? WHERE thread_id = ?',
                (created_at, thread_id),
            )
            conn.execute(
                'UPDATE chat_usage_logs SET created_at = ? WHERE thread_id = ?',
                (created_at, thread_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _seed_thread(self, fingerprint='acct:1', hid=42, request_id='req-1'):
        thread_id = db.build_chat_thread_id(fingerprint, hid)
        db.consume_chat_quota(hid, fingerprint, 2)
        db.save_chat_reply(thread_id, request_id, '安全回复', 8, 2, 'qingxu')
        db.log_chat_usage(
            fingerprint=fingerprint,
            hid=hid,
            thread_id=thread_id,
            request_id=request_id,
            persona='qingxu',
            price=2,
            quota_left=8,
            status='ok',
        )
        return thread_id

    def test_cleanup_deletes_stale_thread_metadata_but_not_quota(self):
        old_thread = self._seed_thread('acct:1', 42, 'old')
        fresh_thread = self._seed_thread('acct:1', 43, 'fresh')
        self._age_thread(old_thread, '2026-01-01 00:00:00')

        with patch.object(chat_cleanup.chat_graph, 'delete_chat_thread') as delete_thread:
            summary = chat_cleanup.cleanup_stale_chat_threads(retention_days=30)

        delete_thread.assert_called_once_with(old_thread)
        self.assertEqual(1, summary['threads_found'])
        self.assertEqual(1, summary['threads_cleaned'])
        self.assertEqual(2, summary['metadata_rows_deleted'])
        self.assertIsNone(db.get_chat_reply(old_thread, 'old'))
        self.assertEqual([], db.get_chat_usage_logs(old_thread))
        # retention cleanup 不重置香火；否则用户等保留期过后会恢复免费额度。
        self.assertEqual(8, db.get_chat_quota_left(42, 'acct:1'))
        self.assertIsNotNone(db.get_chat_reply(fresh_thread, 'fresh'))

    def test_dry_run_reports_stale_threads_without_deleting(self):
        old_thread = self._seed_thread('acct:1', 42, 'old')
        self._age_thread(old_thread, '2026-01-01 00:00:00')

        with patch.object(chat_cleanup.chat_graph, 'delete_chat_thread') as delete_thread:
            summary = chat_cleanup.cleanup_stale_chat_threads(retention_days=30, dry_run=True)

        delete_thread.assert_not_called()
        self.assertEqual([old_thread], summary['thread_ids'])
        self.assertEqual(0, summary['metadata_rows_deleted'])
        self.assertIsNotNone(db.get_chat_reply(old_thread, 'old'))

    def test_latest_activity_protects_thread_from_cleanup(self):
        thread_id = self._seed_thread('acct:1', 42, 'old-request')
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(
                'UPDATE chat_requests SET created_at = ? WHERE thread_id = ?',
                ('2026-01-01 00:00:00', thread_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual([], db.list_stale_chat_threads(retention_days=30))

    def test_recent_feedback_protects_thread_from_cleanup(self):
        thread_id = self._seed_thread('acct:1', 42, 'old-request')
        db.save_chat_feedback('acct:1', 42, 'old-request', 1)
        self._age_thread(thread_id, '2026-01-01 00:00:00')

        self.assertEqual([], db.list_stale_chat_threads(retention_days=30))


if __name__ == '__main__':
    unittest.main()
