"""Static caching must improve navigation without caching configuration data."""
import hashlib
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class StaticAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = patch.object(server.Handler, '_auth_ok', return_value=True)
        cls.auth.start()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.auth.stop()

    def read(self, path, **headers):
        with urlopen(Request(self.base + path, headers=headers), timeout=3) as r:
            return r.status, r.headers, r.read()

    def test_html_keeps_no_store_and_adds_current_content_versions(self):
        _, headers, html = self.read('/ta/assistant')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn(b'<base href="/ta/">', html)
        version = hashlib.sha256((server.WEB / 'app.js').read_bytes()).hexdigest()[:16]
        self.assertIn(('app.js?v=' + version).encode(), html)
        self.assertRegex(html.decode(), r'style\.css\?v=[a-f0-9]{16}')

    def test_matching_version_is_private_and_reusable(self):
        body = (server.WEB / 'style.css').read_bytes()
        version = hashlib.sha256(body).hexdigest()[:16]
        _, headers, received = self.read('/ta/style.css?v=' + version)
        self.assertEqual(received, body)
        self.assertIn('private', headers['Cache-Control'])
        self.assertIn('immutable', headers['Cache-Control'])
        self.assertTrue(headers['ETag'])

    def test_unversioned_or_stale_asset_requires_revalidation(self):
        for path in ['/ta/style.css', '/ta/style.css?v=old']:
            with self.subTest(path=path):
                _, headers, _ = self.read(path)
                self.assertEqual(headers['Cache-Control'], 'private, no-cache')

    def test_conditional_asset_request_returns_304_without_body(self):
        _, headers, _ = self.read('/ta/style.css')
        with self.assertRaises(HTTPError) as caught:
            self.read('/ta/style.css', **{'If-None-Match': headers['ETag']})
        self.assertEqual(caught.exception.code, 304)
        self.assertIsNone(caught.exception.headers.get('Content-Length'))
        self.assertEqual(caught.exception.read(), b'')

    def test_configuration_is_never_served_as_static_304(self):
        _, headers, _ = self.read('/ta/api/templates', **{'If-None-Match': '*'})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIsNone(headers['ETag'])

    def test_versions_follow_changed_files_and_skip_remote_links(self):
        from application.static_assets import version_links
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            css = root / 's.css'
            css.write_text('one')
            html = '<link href="s.css"><script src="https://example.com/x.js"></script>'
            first = version_links(html, root)
            css.write_text('two')
            second = version_links(html, root)
            self.assertNotEqual(first, second)
            self.assertIn('src="https://example.com/x.js"', second)
            self.assertRegex(second, re.compile(r's\.css\?v=[a-f0-9]{16}'))
