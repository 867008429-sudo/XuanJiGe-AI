import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module


class RegistrationInviteFlowTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = app_module.db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        app_module.db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        app_module.db.init_db()
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_auth_config_requires_invite_when_database_codes_exist(self):
        app_module.db.seed_invite_codes(['XJG-API-ONE'])
        with patch.object(app_module, 'REGISTRATION_INVITE_CODES', []):
            resp = self.client.get('/api/auth-config')

        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()['invite_required'])

    def test_register_consumes_database_invite_code(self):
        app_module.db.seed_invite_codes(['XJG-API-ONE'])
        with patch.object(app_module, 'REGISTRATION_INVITE_CODES', []):
            resp = self.client.post(
                '/api/register',
                json={
                    'username': 'alice',
                    'password': 'pass1234',
                    'invite_code': 'XJG-API-ONE',
                    'client_id': 'client-a',
                },
            )

        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertIn('token', resp.get_json())
        self.assertFalse(app_module.db.is_invite_code_available('XJG-API-ONE'))

    def test_register_rejects_missing_database_invite_code(self):
        app_module.db.seed_invite_codes(['XJG-API-ONE'])
        with patch.object(app_module, 'REGISTRATION_INVITE_CODES', []):
            resp = self.client.post(
                '/api/register',
                json={
                    'username': 'alice',
                    'password': 'pass1234',
                    'client_id': 'client-a',
                },
            )

        self.assertEqual(403, resp.status_code)
        self.assertIn('体验码', resp.get_json()['error'])

    def test_database_invites_take_precedence_over_env_invites(self):
        app_module.db.seed_invite_codes(['XJG-DB-ONLY'])
        with patch.object(app_module, 'REGISTRATION_INVITE_CODES', ['ENV-OPEN']):
            resp = self.client.post(
                '/api/register',
                json={
                    'username': 'alice',
                    'password': 'pass1234',
                    'invite_code': 'ENV-OPEN',
                    'client_id': 'client-a',
                },
            )

        self.assertEqual(403, resp.status_code)
        self.assertTrue(app_module.db.is_invite_code_available('XJG-DB-ONLY'))


if __name__ == '__main__':
    unittest.main()