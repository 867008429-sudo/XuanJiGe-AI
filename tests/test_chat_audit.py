import json
import os
import sqlite3
import tempfile
import unittest

import chat_audit
import db
from ai_validator import RISKY_PHRASES


class ChatAuditTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _seed_reply(self, fingerprint='acct:1', hid=42, request_id='req-1',
                    reply='按盘面看宜稳，不作绝对承诺。', retrieved_chunk_ids=None):
        thread_id = db.build_chat_thread_id(fingerprint, hid)
        db.save_chat_reply(
            thread_id,
            request_id,
            reply,
            8,
            2,
            'qingxu',
            retrieved_chunk_ids=retrieved_chunk_ids,
        )
        return thread_id

    def test_heuristic_judge_catches_variant_outside_risky_phrases(self):
        self.assertNotIn('稳稳翻倍', RISKY_PHRASES)

        result = chat_audit.audit_reply_text('这个盘财气来了，稳稳翻倍，闭眼入。')

        self.assertEqual('fail', result['verdict'])
        self.assertIn('misleading_investment_claim', result['issue_codes'])

    def test_chunk_id_without_retrieved_metadata_fails_grounding(self):
        result = chat_audit.audit_reply_text('寒暖一节见【fake-book-ch001】，只作义理参考。')

        self.assertEqual('fail', result['verdict'])
        self.assertIn('unverified_classic_chunk_id', result['issue_codes'])

    def test_retrieved_chunk_id_passes_grounding(self):
        result = chat_audit.audit_reply_text(
            '寒暖一节见【ditiansui-chanwei-ch029】，只作义理参考。',
            retrieved_chunk_ids=['ditiansui-chanwei-ch029'],
        )

        self.assertEqual('pass', result['verdict'])
        self.assertEqual([], result['issue_codes'])

    def test_run_audit_uses_saved_retrieved_chunk_ids(self):
        thread_id = self._seed_reply(
            reply='寒暖一节见【ditiansui-chanwei-ch029】，只作义理参考。',
            retrieved_chunk_ids=['ditiansui-chanwei-ch029'],
        )

        summary = chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)

        self.assertEqual(1, summary['audited'])
        self.assertEqual(1, summary['passed'])
        log = db.get_chat_audit_logs(thread_id)[0]
        self.assertEqual('pass', log['verdict'])
        self.assertEqual([], json.loads(log['issue_codes_json']))

    def test_run_audit_flags_saved_fake_chunk_id(self):
        thread_id = self._seed_reply(
            reply='寒暖一节见【fake-book-ch001】，只作义理参考。',
            retrieved_chunk_ids=['ditiansui-chanwei-ch029'],
        )

        summary = chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)

        self.assertEqual(1, summary['audited'])
        self.assertEqual(1, summary['failed'])
        log = db.get_chat_audit_logs(thread_id)[0]
        issues = json.loads(log['issue_codes_json'])
        self.assertIn('unverified_classic_chunk_id', issues)
        self.assertNotIn('fake-book-ch001', log['issue_codes_json'])

    def test_audit_records_metadata_without_reply_text(self):
        thread_id = self._seed_reply(reply='这个盘财气来了，稳稳翻倍，闭眼入。')

        summary = chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)

        self.assertEqual(1, summary['audited'])
        self.assertEqual(1, summary['failed'])
        self.assertEqual(1, summary['logs_written'])
        logs = db.get_chat_audit_logs(thread_id)
        self.assertEqual(1, len(logs))
        log = logs[0]
        self.assertEqual('heuristic-v1', log['judge'])
        self.assertEqual('fail', log['verdict'])
        self.assertEqual(1, log['issue_count'])
        self.assertEqual(64, len(log['reply_sha256']))
        issues = json.loads(log['issue_codes_json'])
        self.assertEqual(['misleading_investment_claim'], issues)
        self.assertNotIn('稳稳翻倍', log['issue_codes_json'])

        conn = sqlite3.connect(db.DB_PATH)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(chat_audit_logs)')}
        conn.close()
        for forbidden in ('message', 'reply_text', 'paipan_json', 'birth_info', 'solar_date'):
            self.assertNotIn(forbidden, cols)

    def test_audit_skips_already_audited_reply(self):
        self._seed_reply(reply='这个盘要谨慎，不作医疗和投资承诺。')

        first = chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)
        second = chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)

        self.assertEqual(1, first['logs_written'])
        self.assertEqual(0, second['candidates'])
        self.assertEqual(0, second['logs_written'])

    def test_dry_run_does_not_write_audit_logs(self):
        thread_id = self._seed_reply(reply='这个盘财气来了，稳稳翻倍，闭眼入。')

        summary = chat_audit.run_chat_audit(
            sample_rate=1.0,
            limit=10,
            min_count=0,
            dry_run=True,
        )

        self.assertEqual(1, summary['audited'])
        self.assertEqual(1, summary['failed'])
        self.assertEqual(0, summary['logs_written'])
        self.assertEqual([], db.get_chat_audit_logs(thread_id))

    def test_delete_cascade_removes_audit_metadata(self):
        thread_id = self._seed_reply('acct:1', 42, 'req-1', '这个盘财气来了，稳稳翻倍。')
        chat_audit.run_chat_audit(sample_rate=1.0, limit=10, min_count=0)

        deleted = db.delete_chat_cascade('acct:1', 42)

        self.assertEqual(2, deleted)  # 1 幂等回复 + 1 抽审日志；无香火/观测日志行
        self.assertEqual([], db.get_chat_audit_logs(thread_id))


if __name__ == '__main__':
    unittest.main()
