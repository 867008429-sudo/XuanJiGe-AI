import unittest
from unittest.mock import patch

import app as app_module


class AdminStatsTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_stats_hidden_when_admin_token_is_not_configured(self):
        with patch.object(app_module, 'ADMIN_TOKEN', ''):
            resp = self.client.get('/api/stats')

        self.assertEqual(404, resp.status_code)
        self.assertIn('error', resp.get_json())

    def test_stats_rejects_wrong_admin_token(self):
        with patch.object(app_module, 'ADMIN_TOKEN', 'test-admin-secret'):
            resp = self.client.get('/api/stats', headers={'X-Admin-Token': 'wrong'})

        self.assertEqual(403, resp.status_code)

    def test_stats_accepts_admin_header_and_returns_operator_metrics(self):
        with patch.object(app_module, 'ADMIN_TOKEN', 'test-admin-secret'):
            resp = self.client.get('/api/stats', headers={'X-Admin-Token': 'test-admin-secret'})

        self.assertEqual(200, resp.status_code)
        data = resp.get_json()
        self.assertIn('total_accounts', data)
        self.assertIn('cache_rate_percent', data)
        self.assertIn('last_24h', data)
        self.assertIn('ai_requests', data['last_24h'])
        self.assertIn('top_registration_failure_reasons', data['last_24h'])
        self.assertIn('chat_feedback', data)
        self.assertIn('helpful_rate_percent', data['chat_feedback'])


if __name__ == '__main__':
    unittest.main()
