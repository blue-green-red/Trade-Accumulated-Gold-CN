import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from monitor.jd_remote_browser import private_write,ensure_private,run,public_config,access_credentials,child_environment
from monitor.jd_readonly_probe import allowed


class JDRemoteBrowserTests(unittest.TestCase):
    def test_private_files_and_symlink_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private';ensure_private(p)
            self.assertEqual(p.stat().st_mode & 0o777,0o700)
            f=p/'status.json';private_write(f,json.dumps({'connected':False}))
            self.assertEqual(f.stat().st_mode & 0o777,0o600)
            self.assertFalse(json.loads(f.read_text())['connected'])
            link=p/'link';link.symlink_to(f)
            with self.assertRaises(RuntimeError):private_write(link,'no')
            self.assertFalse(json.loads(f.read_text())['connected'])

    def test_invalid_exposure_parameters_rejected_before_launch(self):
        for port,ttl in ((80,3600),(65536,3600),(1455,0),(1455,3601)):
            with self.assertRaises(ValueError):run(port,ttl)
        self.assertFalse(allowed('POST','https://api.jdjygold.com/gw2/generic/hj/h5/m/buyGold',login=True))
        self.assertTrue(allowed('POST','https://api.jdjygold.com/gw2/generic/jrm/newh5/m/indexDataStd',login=True))

    def test_unit_lifetime_and_resources_are_bounded_without_boot_autostart(self):
        text=(Path(__file__).resolve().parents[1]/'deploy/gold-jd-readonly.service').read_text()
        self.assertIn('RuntimeMaxSec=3600',text);self.assertIn('MemoryMax=650M',text)
        self.assertIn('KillMode=control-group',text);self.assertIn('Restart=no',text)
        self.assertNotIn('WantedBy=',text)

    def test_public_entry_requires_https_and_auth_without_exposing_vnc_or_cdp(self):
        text=public_config(1455,1456,'8.218.117.20')
        self.assertIn('listen 0.0.0.0:1455 ssl;',text)
        self.assertIn('error_page 497 =307 https://8.218.117.20:1455$request_uri;',text)
        self.assertIn('auth_basic_user_file',text)
        self.assertIn('proxy_pass http://127.0.0.1:1456;',text)
        self.assertNotIn('9225',text);self.assertNotIn('5905',text)
        with self.assertRaises(ValueError):public_config(1455,1456,'8.218.117.20;bad')

    def test_dashboard_auth_reuses_existing_password_and_never_disables_https_auth(self):
        with patch.dict('os.environ',{'GOLD_WEB_PASSWORD':'unit-dashboard-password'}):
            with patch('monitor.jd_remote_browser.secrets.token_urlsafe') as random:
                self.assertEqual(access_credentials('8.218.117.20',True),('unit-dashboard-password','dashboard'))
                random.assert_not_called()
        text=public_config(1455,1456,'8.218.117.20',True)
        self.assertIn('auth_basic "Gold dashboard login"',text);self.assertIn('auth_basic_user_file',text)
        with self.assertRaises(ValueError):access_credentials(None,True)
        with patch.dict('os.environ',{'GOLD_WEB_PASSWORD':''}):
            with self.assertRaises(ValueError):access_credentials('8.218.117.20',True)

    def test_browser_children_do_not_inherit_dashboard_or_api_credentials(self):
        with patch.dict('os.environ',{'GOLD_WEB_PASSWORD':'unit-password','PUSHPLUS_TOKEN':'unit-token','CLAWX_JR_API_KEY':'unit-key','SERVERCHAN_SENDKEY':'unit-sendkey','HTTPS_PROXY':'http://127.0.0.1:1','TEST_KEEP':'ok'}):
            env=child_environment(':105')
        self.assertEqual(env['DISPLAY'],':105');self.assertEqual(env['TEST_KEEP'],'ok')
        for key in ('GOLD_WEB_PASSWORD','PUSHPLUS_TOKEN','CLAWX_JR_API_KEY','SERVERCHAN_SENDKEY','HTTPS_PROXY'):self.assertNotIn(key,env)
