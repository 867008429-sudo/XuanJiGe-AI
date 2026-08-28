import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ai_service
import app as app_module
import db
from tools.backup_sqlite import backup_database
from tools.preflight import run_preflight


class RuntimeOperationsTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_health_reports_checks_without_leaking_api_key(self):
        old_db_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            db.DB_PATH = os.path.join(tempdir, 'xuanjige.db')
            with patch.object(ai_service, 'DEEPSEEK_API_KEY', 'sk-real-secret-for-test'):
                resp = self.client.get('/health')
            db.DB_PATH = old_db_path

        self.assertEqual(200, resp.status_code)
        body = resp.get_data(as_text=True)
        data = resp.get_json()
        self.assertEqual('ok', data['status'])
        self.assertIn('checks', data)
        self.assertIn('X-Content-Type-Options', resp.headers)
        self.assertIn('X-Frame-Options', resp.headers)
        self.assertNotIn('sk-real-secret-for-test', body)
        self.assertNotIn(tempdir, body)

    def test_health_degrades_when_database_directory_is_missing(self):
        old_db_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as tempdir:
            db.DB_PATH = os.path.join(tempdir, 'missing', 'xuanjige.db')
            resp = self.client.get('/health')
            db.DB_PATH = old_db_path

        self.assertEqual(503, resp.status_code)
        data = resp.get_json()
        self.assertEqual('degraded', data['status'])
        db_check = next(item for item in data['checks'] if item['name'] == 'database_path')
        self.assertFalse(db_check['ok'])

    def test_request_logging_omits_body_auth_and_client_id(self):
        with self.assertLogs('xuanjige.app', level='INFO') as captured:
            self.client.get(
                '/api/auth-config',
                headers={
                    'Authorization': 'Bearer secret-token-for-test',
                    'X-Client-Id': 'secret-client-id-for-test',
                },
            )

        logs = '\n'.join(captured.output)
        self.assertIn('path=/api/auth-config', logs)
        self.assertNotIn('secret-token-for-test', logs)
        self.assertNotIn('secret-client-id-for-test', logs)

    def test_ai_missing_key_log_omits_birth_context(self):
        sample = {'solar_date': '1995年8月16日 10:30', 'gender': 'male'}
        with patch.object(ai_service, 'DEEPSEEK_API_KEY', ''):
            with self.assertLogs('xuanjige.ai', level='WARNING') as captured:
                events = list(ai_service.stream_interpretation(sample))

        logs = '\n'.join(captured.output)
        self.assertTrue(any('DeepSeek API Key未配置' in event for event in events))
        self.assertIn('missing_api_key', logs)
        self.assertNotIn('1995年8月16日', logs)

    def test_backup_database_creates_restorable_sqlite_copy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = os.path.join(tempdir, 'source.db')
            backup_dir = os.path.join(tempdir, 'backups')
            conn = sqlite3.connect(source)
            conn.execute('CREATE TABLE demo (name TEXT)')
            conn.execute('INSERT INTO demo (name) VALUES (?)', ('xuanjige',))
            conn.commit()
            conn.close()

            result = backup_database(source=source, backup_dir=backup_dir, keep_last=3)
            backup_path = result['backup']

            self.assertTrue(os.path.exists(backup_path))
            restored = sqlite3.connect(backup_path)
            try:
                row = restored.execute('SELECT name FROM demo').fetchone()
            finally:
                restored.close()
            self.assertEqual(('xuanjige',), row)

    def test_preflight_report_is_non_secret_and_non_failing(self):
        report = run_preflight(port=8888)
        encoded = json.dumps(report, ensure_ascii=False)
        failed = [item['name'] for item in report['checks'] if item['required'] and not item['ok']]

        self.assertIn(report['status'], ('ok', 'warn'))
        self.assertEqual([], failed)
        self.assertNotIn('github_' + 'pat_', encoded)
        self.assertNotIn('Aa' + '867008429' + '!', encoded)


if __name__ == '__main__':
    unittest.main()

