"""Проверки реализованной основы. Не подменяют приёмку A1–A7."""
import os
import tempfile
import unittest
from pathlib import Path

SANDBOX = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = SANDBOX.name
os.environ['APP_ENV'] = 'test'
os.environ['PROVIDER_MODE'] = 'mock'

from backend.db import Database
from backend.security import AppError, encrypt, decrypt, hash_password, verify_password, check_totp, new_totp_secret, totp
from backend.ocr import build_mrz, parse_mrz, transliterate

class FoundationTests(unittest.TestCase):
    def test_encryption_roundtrip(self):
        value = encrypt('4512 123456', 'passport')
        self.assertNotIn('123456', value)
        self.assertEqual(decrypt(value, 'passport'), '4512 123456')

    def test_encryption_context_binding(self):
        value = encrypt('4512 123456', 'passport')
        with self.assertRaises(Exception): decrypt(value, 'different-field')

    def test_password_hash(self):
        hashed = hash_password('TestOnlyPassword123!')
        self.assertTrue(verify_password('TestOnlyPassword123!', hashed))
        self.assertFalse(verify_password('incorrect', hashed))

    def test_totp_replay_blocked(self):
        secret = new_totp_secret()
        code = totp(secret)
        step = check_totp(secret, code)
        with self.assertRaises(AppError): check_totp(secret, code, step)

    def test_mrz_latin_and_checks(self):
        raw = build_mrz('СЕМЁНОВ', 'ИЛЬЯ', '720123456', '1990-02-03', '2030-04-05')
        result = parse_mrz(raw)
        self.assertTrue(result['mrz_valid'])
        self.assertEqual(result['fields']['last_name_latin'], 'SEMENOV')
        self.assertEqual(result['fields']['first_name_latin'], 'ILIA')
        self.assertEqual(result['fields']['birth_date'], '1990-02-03')

    def test_mrz_corruption_flagged(self):
        raw = build_mrz('ИВАНОВ', 'ИВАН', '720123456', '1990-02-03', '2030-04-05')
        lines = raw.splitlines()
        lines[1] = '8' + lines[1][1:]
        self.assertFalse(parse_mrz('\n'.join(lines))['mrz_valid'])

    def test_mrz_length_rejected(self):
        with self.assertRaises(AppError): parse_mrz('P<RUSINVALID')

    def test_transliteration(self):
        self.assertEqual(transliterate('ЮЛИЯ ЩУКИНА-ЁЛКИНА'), 'IULIIA SHCHUKINA-ELKINA')

    def test_schema_and_immutable_audit(self):
        db = Database('sqlite:///' + str(Path(SANDBOX.name) / 'foundation.sqlite3'))
        try:
            db.migrate()
            with db.atomic():
                row = db.audit(None, 'test', 'persons', None, {'fields': ['number']})
            self.assertTrue(db.verify_audit())
            with self.assertRaises(Exception):
                with db.atomic(): db.execute('DELETE FROM audit_log WHERE id=?', (row['id'],))
            self.assertTrue(db.verify_audit())
        finally: db.close()

if __name__ == '__main__': unittest.main()
