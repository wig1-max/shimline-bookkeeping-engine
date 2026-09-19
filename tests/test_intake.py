"""Regression checks with synthetic data and temporary storage only."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi.testclient import TestClient
import app as service
from shimline import auth
from shimline import admin as admin_workspace


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / 'intake.db'
        service.UPLOADS_DIR = Path(self.temp.name) / 'uploads'
        service.UPLOADS_DIR.mkdir()
        service.SMTP_HOST = ''
        service.MAX_UPLOAD_MB = 1
        # Rate-limit counters live in the database now, so the fresh
        # temp DB above is what clears them between tests.
        admin_workspace.configure(
            db_factory=service._db, uploads_dir=lambda: service.UPLOADS_DIR,
            retention_after_close=30, retention_unclosed=90, cookie_secure=False,
        )
        conn = service._db()
        auth.create_user(conn, 'admin@example.invalid', 'Local Admin', 'local-test-password-only', 'owner')
        conn.close()
        self.client = TestClient(service.app)

    def login(self):
        response = self.client.post('/admin/login', data={
            'email': 'admin@example.invalid', 'password': 'local-test-password-only', 'next': '/admin'
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def paid_token(self, *, age_hours=0, used=False):
        token = f'local-token-{len(list(service.UPLOADS_DIR.iterdir()))}-{age_hours}-{used}'
        order_id = f'order_{service._token_hash(token)[:12]}'
        paid_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime('%Y-%m-%d %H:%M:%S')
        conn = service._db()
        conn.execute(
            "INSERT INTO payments (order_id, payment_id, amount, currency, status, paid_at, "
            "upload_token_hash, token_used_at) VALUES (?, 'pay_test', 19900, 'CAD', 'paid', ?, ?, ?)",
            (order_id, paid_at, service._token_hash(token), paid_at if used else None),
        )
        conn.commit()
        conn.close()
        return token

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_upload_and_admin_escape(self):
        payload = b'job,revenue\ndemo,100\n'
        response = self.client.post('/intake', data={'company': '<script>alert(1)</script>', 'email': 'test@example.invalid',
                                                     'upload_token': self.paid_token()},
                                    files={'pl_file': ('../../outside.csv', payload, 'text/csv'),
                                           'ar_file': ('../../outside.csv', payload, 'text/csv')})
        self.assertEqual(response.status_code, 200)
        sid = response.json()['id']
        self.assertEqual(len(list((service.UPLOADS_DIR / sid).iterdir())), 2)
        self.assertFalse((Path(self.temp.name) / 'outside.csv').exists())
        self.assertEqual(self.client.get('/admin', follow_redirects=False).status_code, 303)
        bad = self.client.post('/admin/login', data={'email': 'wrong@example.invalid', 'password': 'wrong'})
        self.assertEqual(bad.status_code, 401)
        self.login()
        admin = self.client.get('/admin')
        self.assertIn('&lt;script&gt;', admin.text)
        self.assertNotIn('<script>', admin.text)
        download = self.client.get(f'/admin/files/{sid}/pl_outside.csv')
        self.assertEqual(download.content, payload)
        self.assertIn('attachment', download.headers['content-disposition'])

        self.client.post('/admin/logout', data={'csrf_token': self.client.cookies.get('csrf', '')})
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f'/admin/files/{sid}/pl_outside.csv').status_code, 401)

    def test_oversized_file_cleans_partial_upload(self):
        token = self.paid_token()
        response = self.client.post('/intake', data={'upload_token': token}, files={'pl_file': ('good.csv', b'ok'),
                                    'ar_file': ('large.csv', b'x' * (1024 * 1024 + 1))})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list(service.UPLOADS_DIR.iterdir()), [])
        self.assertEqual(service._valid_upload_token(token)[:6], 'order_')

    def test_payment_token_is_required_and_single_use(self):
        response = self.client.post('/intake', data={'company': 'Unpaid'})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(list(service.UPLOADS_DIR.iterdir()), [])

        token = self.paid_token()
        paid = self.client.post('/intake', data={'company': 'Paid', 'upload_token': token})
        self.assertEqual(paid.status_code, 200)
        reused = self.client.post('/intake', data={'company': 'Replay', 'upload_token': token})
        self.assertEqual(reused.status_code, 402)

        expired = self.client.post('/intake', data={
            'company': 'Expired', 'upload_token': self.paid_token(age_hours=service.UPLOAD_TOKEN_HOURS + 1)
        })
        self.assertEqual(expired.status_code, 402)

        conn = service._db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM organizations').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM engagements').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_items').fetchone()[0], 8)
        conn.close()

    def test_rate_limit_and_cors(self):
        for _ in range(8):
            self.assertEqual(self.client.post('/intake', data={
                'company': 'Synthetic test', 'upload_token': self.paid_token()
            }).status_code, 200)
        self.assertEqual(self.client.post('/intake', data={
            'company': 'Synthetic test', 'upload_token': self.paid_token()
        }).status_code, 429)
        for origin, expected in [('https://shimline.ca', 200), ('https://untrusted.invalid', 400)]:
            response = self.client.options('/intake', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST'})
            self.assertEqual(response.status_code, expected)


if __name__ == '__main__':
    unittest.main()
