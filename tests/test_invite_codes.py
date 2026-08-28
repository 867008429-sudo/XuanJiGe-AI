import os
import tempfile
import unittest

import db


class InviteCodeTests(unittest.TestCase):
    def setUp(self):
        self.old_db_path = db.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tempdir.name, 'test.db')
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_invite_code_is_consumed_by_registration(self):
        inserted = db.seed_invite_codes(['XJG-ONE'], note='unit test')
        self.assertEqual(1, inserted)
        self.assertTrue(db.has_invite_codes())
        self.assertTrue(db.is_invite_code_available('XJG-ONE'))

        token, error = db.register('alice', 'pass1234', invite_code='XJG-ONE')

        self.assertIsNone(error)
        self.assertTrue(token)
        self.assertFalse(db.is_invite_code_available('XJG-ONE'))
        stats = db.get_invite_code_stats()
        self.assertEqual(1, stats['total'])
        self.assertEqual(0, stats['available'])
        self.assertEqual(1, stats['used_up'])

    def test_used_invite_code_cannot_register_again(self):
        db.seed_invite_codes(['XJG-ONCE'])
        token, error = db.register('alice', 'pass1234', invite_code='XJG-ONCE')
        self.assertIsNone(error)
        self.assertTrue(token)

        token, error = db.register('bob', 'pass1234', invite_code='XJG-ONCE')

        self.assertIsNone(token)
        self.assertEqual('体验码已被使用或不存在', error)

    def test_multi_use_invite_code_tracks_remaining_uses(self):
        db.seed_invite_codes(['XJG-TWO'], max_uses=2)
        self.assertTrue(db.is_invite_code_available('XJG-TWO'))
        db.register('alice', 'pass1234', invite_code='XJG-TWO')
        self.assertTrue(db.is_invite_code_available('XJG-TWO'))
        db.register('bob', 'pass1234', invite_code='XJG-TWO')
        self.assertFalse(db.is_invite_code_available('XJG-TWO'))

    def test_disabled_invite_code_cannot_register(self):
        db.seed_invite_codes(['XJG-BLOCK'])
        changed = db.disable_invite_codes(['XJG-BLOCK'])
        self.assertEqual(1, changed)
        self.assertFalse(db.is_invite_code_available('XJG-BLOCK'))

        token, error = db.register('alice', 'pass1234', invite_code='XJG-BLOCK')

        self.assertIsNone(token)
        self.assertEqual('体验码已被使用或不存在', error)
        stats = db.get_invite_code_stats()
        self.assertEqual(1, stats['disabled'])


if __name__ == '__main__':
    unittest.main()