"""Public price derivation is independent of account anchors; rejection remains fail-closed."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest

from monitor.public_ai import mapped_levels,privacy_ok
from monitor.public_research import evidence_for
from test_public_ai import NOW,content,quote
import test_public_ai as fixtures


def context():
    return {'snapshot_at':NOW.isoformat(),'jd_accumulation_quote':quote(873.46),
        'xauusd':4071.99,'daily_indicators':{'status':'fresh','fetched_at':NOW.isoformat(),
            'ma20_usd':4066.983,'ma50_usd':4197.609},
        'trading_accounts':{'accounts':{'high_full':{'cash':123456.78,'target_price':900.32,'stop_price':843.27}}},
        'live_readonly_accounts':{'accounts':{'zheshang':{'previous_advice':[{'price_cny':900.32}],
            'quote':{'buy_price_cny':873.57,'sell_price_cny':873.57}}}}}


class MappingTests(unittest.TestCase):
    def test_derived_prices_do_not_follow_old_protection_or_mutate_context(self):
        source=context();before=deepcopy(source)
        levels=mapped_levels(source)
        self.assertEqual(levels['ma50_cny'],900.41)
        self.assertEqual(levels['ma20_cny'],round(873.46*4066.983/4071.99,2))
        self.assertEqual(source,before)
        source['trading_accounts']={'accounts':{'hidden':{'target_price':9999,'cash':999999}}}
        source['live_readonly_accounts']={'accounts':{'hidden':{'quote':{'sell_price_cny':9999}}}}
        source['previous_public_analysis']={'risk':'旧目标900.32'}
        source['trigger_events']=[{'price_cny':900.32}]
        self.assertEqual(mapped_levels(source),levels)

    def test_derived_reference_and_old_private_anchor_remain_distinct(self):
        source=context()
        self.assertTrue(privacy_ok({'price_cny':900.41},source))
        self.assertFalse(privacy_ok({'price_cny':900.32},source))
        self.assertFalse(privacy_ok({'text':'目标900.32近似合理'},source))
        self.assertFalse(privacy_ok({'price_cny':873.57},source))
        # A supplied derived object cannot grant its own permission to disclose a private price.
        source['public_reference']={'mapped_levels':{'ma50_cny':900.32}}
        self.assertFalse(privacy_ok({'price_cny':900.32},source))
        self.assertTrue(privacy_ok({'price_cny':900.41},source))

    def test_unknown_stale_future_and_nonpublic_inputs_never_create_references(self):
        for change in (
            {'snapshot_at':None}, {'xauusd':None}, {'xauusd':False}, {'xauusd':0},
            {'xauusd':float('nan')}, {'xauusd':float('inf')}, {'xauusd':10**500},
            {'jd_accumulation_quote':{**quote(),'source':'private_bank'}},
            {'jd_accumulation_quote':quote(at=NOW-timedelta(seconds=121))},
            {'daily_indicators':{'status':'stale','fetched_at':NOW.isoformat(),'ma50_usd':4197}},
            {'daily_indicators':{'status':'fresh','fetched_at':(NOW-timedelta(seconds=121)).isoformat(),'ma50_usd':4197}},
            {'daily_indicators':{'status':'fresh','fetched_at':(NOW+timedelta(seconds=31)).isoformat(),'ma50_usd':4197}},
        ):
            source=context();source.update(change)
            with self.subTest(change=str(change)[:80]):self.assertEqual(mapped_levels(source),{})
        for bad in (None,False,0,-1,float('nan'),float('inf'),10**500):
            source=context();source['daily_indicators'].update(ma20_usd=bad,ma50_usd=bad)
            with self.subTest(value=str(bad)[:30]):self.assertEqual(mapped_levels(source),{})

    def test_public_evidence_explains_actual_mapping_and_keeps_historical_samples(self):
        source=context();source['jd_daily_samples']={'days':[{'date_bj':'2026-11-04','sampled_low_cny':843.27}]}
        value=content();value['decision_basis']['entry_scenario']={
            'entry_price_cny':873.46,'target_price_cny':900.41,'exit_price_cny':843.27,'basis':'公共假设'}
        evidence=evidence_for(value,source)['key_levels']
        self.assertIn('MA50',next(r['basis'] for r in evidence if r['label']=='情景目标'))
        self.assertIn('非京东技术位',next(r['basis'] for r in evidence if r['label']=='情景目标'))
        self.assertIn('2026-11-04',next(r['basis'] for r in evidence if r['label']=='情景退出'))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PublicTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.context=context()
        self.f.context['jd_daily_samples']={'days':[{'sampled_low_cny':843.27}]}
        self.f.store.set_state('web_snapshot',{'execution_quote':quote(873.46)})

    def scenario(self,target):
        value=content();value['decision_basis']['entry_scenario']={
            'entry_price_cny':873.46,'target_price_cny':target,'exit_price_cny':843.27,
            'basis':f'公共MA50换算目标{target}，为假设情景。'}
        value['decision_basis']['position_rationale']=f'目标{target}仅作反弹情景，较高仓位放大收益及损失。'
        value['decision_basis']['review_triggers']=[
            {'kind':'price','condition':'at_or_above','price_cny':target,'at':None,'event':None},
            {'kind':'time','condition':None,'price_cny':None,'at':(NOW+timedelta(hours=2)).isoformat(),'event':None}]
        return value

    def test_copied_private_target_across_scenario_review_and_prose_still_rejected(self):
        self.assertEqual(self.f.publish(self.scenario(900.32)),'unavailable')
        self.assertIsNone(self.f.read()['analysis'])

    def test_independent_public_reference_publishes_all_eleven_recommendations(self):
        before=deepcopy(self.f.context)
        self.assertEqual(self.f.publish(self.scenario(mapped_levels(self.f.context)['ma50_cny'])),'ready')
        result=self.f.read()
        self.assertEqual(result['status'],'current')
        self.assertEqual(len(result['recommendations']),11)
        self.assertEqual(result['decision_basis']['entry_scenario']['target_price_cny'],900.41)
        self.assertNotIn('900.32',json.dumps(result))
        self.assertEqual(self.f.context,before)

    def test_private_balances_and_identity_do_not_become_publishable(self):
        for text in ('余额123456.78','目标900.32','真实账户','参考873.57'):
            value=self.scenario(900.41);value['analysis']['risk']=text
            with self.subTest(text=text):
                self.assertEqual(self.f.publish(value,view_id=sum(map(ord,text))),'unavailable')
