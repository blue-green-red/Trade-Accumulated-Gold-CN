import gzip
import http.client
import threading
import unittest
from monitor.web import Handler, ThreadingHTTPServer


class CompressionTests(unittest.TestCase):
    def test_gzip_negotiation_and_lengths(self):
        body=b'{"data":"'+b'x'*5000+b'"}'
        class Local(Handler):
            def do_GET(self):self._send(200,body,'application/json')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Local)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            for encoding,want in [('gzip',True),('gzip;q=0',False),('gzip;q=invalid',False),('identity',False)]:
                c=http.client.HTTPConnection('127.0.0.1',server.server_port)
                c.request('GET','/',headers={'Accept-Encoding':encoding})
                r=c.getresponse();data=r.read()
                self.assertEqual(len(data),int(r.getheader('Content-Length')))
                self.assertEqual(r.getheader('Vary'),'Accept-Encoding')
                self.assertEqual(r.getheader('Content-Encoding')=='gzip',want)
                self.assertEqual(gzip.decompress(data) if want else data,body)
                if want:self.assertLess(len(data),len(body)//10)
                c.close()
        finally:server.shutdown();server.server_close();t.join()

    def test_dashboard_cache_shares_work_expires_and_invalidates(self):
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from monitor.config import Settings
        from monitor.web import cached_dashboard, invalidate_dashboard
        with tempfile.TemporaryDirectory() as tmp:
            s=replace(Settings(),db_path=Path(tmp)/'test.db')
            with patch('monitor.web.dashboard',return_value={'data':'same'}) as build,patch('monitor.web.time.monotonic',return_value=100):
                threads=[threading.Thread(target=lambda:cached_dashboard(s)) for _ in range(5)]
                for t in threads:t.start()
                for t in threads:t.join()
                self.assertEqual(build.call_count,1)
                invalidate_dashboard(s);cached_dashboard(s)
                self.assertEqual(build.call_count,2)
            with patch('monitor.web.dashboard',return_value={'data':'new'}) as build,patch('monitor.web.time.monotonic',return_value=103):
                self.assertIn(b'new',cached_dashboard(s));build.assert_called_once()
            invalidate_dashboard(s)
