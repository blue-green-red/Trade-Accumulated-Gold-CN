"""Proxy login failures remain isolated per visitor; direct callers cannot spoof IPs."""
import base64
import http.client
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from monitor import web


class ProxyAuthTests(unittest.TestCase):
    def test_only_loopback_may_supply_a_valid_forwarded_client_ip(self):
        handler = object.__new__(web.Handler)
        for peer, value, expected in (
            ('127.0.0.1', '198.51.100.1', '198.51.100.1'),
            ('::1', '2001:db8::1', '2001:db8::1'),
            ('198.51.100.2', '198.51.100.1', '198.51.100.2'),
            ('127.0.0.1', '198.51.100.1, 198.51.100.2', '127.0.0.1'),
            ('127.0.0.1', 'not-an-ip', '127.0.0.1'),
            ('127.0.0.1', '', '127.0.0.1'),
        ):
            with self.subTest(peer=peer, value=value):
                handler.client_address = (peer, 12345)
                handler.headers = {'X-Real-IP': value}
                self.assertEqual(handler._auth_client_ip(), expected)

    def test_get_and_post_lockouts_do_not_block_other_proxy_visitors(self):
        class Local(web.Handler):
            password = 'test-only'
            settings = SimpleNamespace(ai_analysts=('codex',))
            def log_message(self, *args): pass

        server = web.ThreadingHTTPServer(('127.0.0.1', 0), Local)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, ip, password):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            headers = {
                'X-Real-IP': ip,
                'X-Gold-Control': '1',
                'Content-Type': 'application/json',
                'Authorization': 'Basic ' + base64.b64encode(('viewer:' + password).encode()).decode(),
            }
            try:
                conn.request(method, '/api/test-unknown', body=b'{}' if method == 'POST' else None, headers=headers)
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()
        try:
            from collections import defaultdict, deque
            with patch.object(web, '_auth_failures', defaultdict(deque)), patch.object(web, 'AUTH_FAIL_LIMIT', 2):
                for method, blocked, other in (
                    ('GET', '198.51.100.11', '198.51.100.12'),
                    ('POST', '198.51.100.21', '198.51.100.22'),
                ):
                    with self.subTest(method=method):
                        self.assertEqual(request(method, blocked, 'wrong'), 401)
                        self.assertEqual(request(method, blocked, 'wrong'), 401)
                        self.assertEqual(request(method, blocked, 'test-only'), 429)
                        self.assertEqual(request(method, other, 'test-only'), 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
