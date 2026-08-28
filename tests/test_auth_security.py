import hashlib
import os
import tempfile
import unittest

import db


class AuthSecurityTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def _password_hash_for(self, username):
        conn = db.get_db()
        row = conn.execute(
            'SELECT password_hash FROM accounts WHERE username = ?',
            (username,)
        ).fetchone()
        conn.close()
        return row['password_hash']

    def test_new_accounts_use_salted_password_hash(self):
        token, error = db.register('alice', 'pass1234')

        self.assertIsNone(error)
        self.assertTrue(token)
        stored = self._password_hash_for('alice')
        self.assertTrue(stored.startswith(('scrypt:', 'pbkdf2:')))
        self.assertNotEqual(hashlib.sha256('pass1234'.encode()).hexdigest(), stored)

    def test_legacy_sha256_account_can_login_and_is_upgraded(self):
        legacy_hash = hashlib.sha256('pass1234'.encode()).hexdigest()
        conn = db.get_db()
        conn.execute(
            'INSERT INTO accounts (username, password_hash) VALUES (?, ?)',
            ('legacy', legacy_hash),
        )
        conn.commit()
        conn.close()

        token, error = db.login('legacy', 'pass1234')

        self.assertIsNone(error)
        self.assertTrue(token)
        upgraded = self._password_hash_for('legacy')
        self.assertNotEqual(legacy_hash, upgraded)
        self.assertTrue(upgraded.startswith(('scrypt:', 'pbkdf2:')))

    def test_wrong_password_is_rejected(self):
        db.register('alice', 'pass1234')

        token, error = db.login('alice', 'wrong')

        self.assertIsNone(token)
        self.assertEqual('用户名或密码错误', error)


if __name__ == '__main__':
    unittest.main()