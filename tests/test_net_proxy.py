import os
import unittest
from unittest.mock import patch
from monitor.config import Settings
from net_proxy import apply_proxy,model_environment,PROXY_KEYS


class DataRoutingTests(unittest.TestCase):
    def test_parent_direct_child_model_proxy_without_mutating_parent(self):
        inherited={k:'http://invalid:9999' for k in PROXY_KEYS}
        with patch.dict(os.environ,{**inherited,'NO_PROXY':'localhost'},clear=True):
            self.assertEqual(apply_proxy(),'http://127.0.0.1:7890')
            for key in PROXY_KEYS:self.assertNotIn(key,os.environ)
            env=model_environment()
            self.assertEqual(env['https_proxy'],'http://127.0.0.1:7890')
            self.assertEqual(env['NO_PROXY'],'localhost')
            for key in PROXY_KEYS:self.assertNotIn(key,os.environ)
            self.assertEqual(apply_proxy(),'http://127.0.0.1:7890')

    def test_explicit_empty_disables_model_proxy_as_well(self):
        with patch.dict(os.environ,{'GOLD_TRADING_HTTP_PROXY':'','HTTPS_PROXY':'http://invalid'},clear=True):
            apply_proxy();self.assertEqual(model_environment(),dict(os.environ))
            for key in PROXY_KEYS:self.assertNotIn(key,model_environment())
