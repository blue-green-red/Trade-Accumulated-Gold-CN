"""Synthetic accounts: whole-account risk and conditional decision consistency."""
from copy import deepcopy
from datetime import timedelta
import unittest
import os
from unittest.mock import patch

from monitor.jd_live_risk import account_risk,effect,annotate,buy_sizing,validate_downside
from monitor.jd_live_proposals import proposal_signature,format_proposals
from monitor.jd_live_semantics import notification_parts
from monitor.public_ai import privacy_ok
import test_live_proposals as fixtures


class ParticipationPreferenceTests(unittest.TestCase):
    def test_real_profile_and_saved_advice_share_simulation_preference(self):
        from monitor.jd_live_readonly import model_input
        from monitor.risk_profiles import profile
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        with patch.dict(os.environ,{'GOLD_JD_LIVE_RISK_PROFILE':'high'}):
            value=model_input(f.s.conn,fixtures.NOW)
            self.assertEqual(value['risk_profile']['participation_preference'],profile('high_empty')['participation_preference'])
            self.assertEqual(value['risk_profile']['trade_risk_pct'],4)
            f.saved(f.value(suggested_orders=[]))
            self.assertEqual(f.latest()['risk_profile']['participation_preference'],'participate')
            self.assertEqual(f.s.conn.execute('SELECT count(*) FROM jd_live_orders').fetchone()[0],0)


def source():
    return {'status':'fresh','cash_cny':700000.,'total_grams':200.,'sell_fee_rate':.004,
        'quote':{'status':'fresh','sell_price_cny':875.}}


def scenario(price=840,role='stress_test',action='hold'):
    return {'price_cny':price,'price_role':role,'preferred_action':action,
        'basis':'公共观察价，仅作压力测试，不代表原假设失效。',
        'buy_case':'若交易假设仍成立，可比较更低入场价的分批价值。',
        'hold_case':'当前较低仓位能承受该情景，继续保留参与。',
        'sell_case':'尚无假设失效或自选风险上限的依据，不默认减仓。'}


class AccountRiskTests(unittest.TestCase):
    def test_known_position_net_assets_and_reference_budget(self):
        s=source();before=deepcopy(s);position,risk=account_risk(s,{'trade_risk_pct':4})
        self.assertEqual(position,20.)
        self.assertEqual(risk['gross_assets_cny'],875000.)
        self.assertEqual(risk['net_assets_cny'],874300.)
        self.assertEqual(risk['risk_budget_cny'],34972.)
        self.assertEqual(risk['gold_down_1pct_net_loss_cny'],1743.)
        self.assertEqual(s,before)
        self.assertNotIn('historical_drawdown_pct',risk)

    def test_unknown_stale_zero_cash_and_zero_holdings(self):
        s=source();s['cash_cny']=None
        p,r=account_risk(s,{'trade_risk_pct':4});self.assertIsNone(p);self.assertIsNone(r['net_assets_cny'])
        s=source();s['sell_fee_rate']=None
        p,r=account_risk(s,{'trade_risk_pct':4});self.assertEqual(p,20);self.assertIsNone(r['risk_budget_cny'])
        for change in ({'status':'stale'},{'quote':{'status':'historical','sell_price_cny':875.}}):
            s=source();s.update(change);p,r=account_risk(s,{'trade_risk_pct':4})
            self.assertIsNone(p);self.assertIsNone(r['risk_budget_cny'])
        s=source();s.update(cash_cny=0,quote=None)
        p,r=account_risk(s,{'trade_risk_pct':4});self.assertEqual(p,100);self.assertIsNone(r['net_assets_cny'])
        s=source();s.update(total_grams=0,quote=None)
        p,r=account_risk(s,{'trade_risk_pct':4});self.assertEqual(p,0);self.assertEqual(r['net_assets_cny'],700000)
        s.update(cash_cny=0);p,r=account_risk(s,{'trade_risk_pct':4});self.assertIsNone(p);self.assertEqual(r['risk_budget_cny'],0)

    def test_hypothetical_exit_keeps_same_fee_baseline(self):
        e=effect(source(),840,'sell',100)
        self.assertEqual(e['account_change_cny'],-6972.)
        self.assertEqual(e['account_change_pct'],-.7974)
        self.assertEqual(e['position_after_pct'],9.6812)
        self.assertEqual(e['sale_fee_cny'],336.)
        # Selling at today's quote has no extra net-valuation loss from paying an already-reserved fee.
        self.assertEqual(effect(source(),875,'sell',100)['account_change_cny'],0)
        self.assertGreater(effect(source(),850,'buy',10)['position_after_pct'],20)
        for s in ({**source(),'cash_cny':None},{**source(),'sell_fee_rate':None},{**source(),'status':'stale'}):
            self.assertIsNone(effect(s,840,'sell',100))
        self.assertIsNone(effect(source(),840,'sell',201))
        self.assertIsNone(effect(source(),850,'buy',1000))

    def test_buy_sizing_accounts_for_reserved_cash_existing_holdings_and_exit_fees(self):
        value={'action':'buy','price_condition':'at_or_below','price_cny':920,'grams':2,
               'input_data_status':{'account':'fresh'},'input_quote_status':'fresh',
               'input_buy_quote_cny':1000,'input_quote_cny':1000,'input_sell_fee_rate':.004,
               'input_state':{'cash_cny':10000,'total_grams':10},'input_participation_cash_cny':8000,
               'input_account_risk':{'net_assets_cny':19960,'risk_budget_cny':1500},
               'position_path':{'target_position_pct':60,'boundary_price_cny':880}}
        result,=buy_sizing(value)
        self.assertEqual(result['kind'],'immediate')
        self.assertEqual(result['suggested_grams'],2)
        self.assertEqual(result['target_add_grams'],2.5217)
        self.assertEqual(result['position_after_pct'],57.5)
        self.assertEqual(result['spending_cny'],1840)
        self.assertEqual(result['cash_cap_grams'],8.69)
        self.assertEqual(result['risk_cap_grams'],7.0)
        self.assertEqual(result['account_at_exit_change_cny'],-1282.24)
        self.assertEqual(result['added_loss_cny_per_gram'],43.52)

    def test_buy_sizing_keeps_observation_quantities_hypothetical_and_unknown_limits_omitted(self):
        case={'decision':'needs_review','entry_price_cny':900,'risk_exit_cny':875,'grams':None}
        value={'action':'hold','input_data_status':{'account':'fresh'},'input_quote_status':'fresh',
               'input_state':{'cash_cny':90000,'total_grams':0},'input_quote_cny':950,
               'input_account_risk':{'net_assets_cny':90000},'position_path':{'target_position_pct':2},
               'limit_buy_reviews':[{'decision':'not_worthwhile','entry_price_cny':940},case]}
        result,=buy_sizing(value)
        self.assertIsNone(result['suggested_grams'])
        self.assertEqual((result['target_add_grams'],result['position_after_pct']),(2.,2.))
        self.assertNotIn('cash_cap_grams',result)
        self.assertNotIn('risk_cap_grams',result)
        self.assertNotIn('account_at_exit_change_cny',result)
        self.assertEqual(buy_sizing({**value,'input_quote_status':'historical'}),[])
        self.assertEqual(buy_sizing({**value,'limit_buy_reviews':[case,{**case,'entry_price_cny':890}]}),[])

    def test_combined_sizing_counts_each_new_batch_once_and_reserves_risk(self):
        from monitor.jd_live_presentation import presentation

        order={'kind':'limit_buy','price_cny':905,'grams':3,'expires_at':'2026-11-30T00:00:00+00:00'}
        review={'decision':'recommend','entry_price_cny':905,'risk_exit_cny':870,'grams':3,'expires_at':order['expires_at']}
        value={'action':'buy','price_condition':'at_or_below','price_cny':920,'grams':2,
               'input_data_status':{'account':'fresh'},'input_quote_status':'fresh','input_buy_quote_cny':1000,
               'input_quote_cny':1000,'input_sell_fee_rate':.004,'input_state':{'cash_cny':10000,'total_grams':10},
               'input_participation_cash_cny':9090,'input_pending_buy_exposure':{'grams':1,'cost_cny':910},
               'input_account_risk':{'net_assets_cny':19960,'risk_budget_cny':1500},
               'position_path':{'target_position_pct':60,'boundary_price_cny':880},
               'suggested_orders':[order],'limit_buy_reviews':[review]}
        immediate,limit=buy_sizing(value)
        together=immediate['combined_buys']
        self.assertEqual((together['other_buy_grams'],together['total_buy_grams'],together['total_spending_cny']),(4,6,5465))
        self.assertEqual(together['account_at_exit_change_cny'],-1401.32)
        self.assertEqual((together['cash_cap_grams'],together['risk_cap_grams']),(6.92,4.26))
        self.assertEqual((limit['combined_buys']['other_buy_grams'],limit['combined_buys']['total_buy_grams']),(3,6))
        self.assertNotIn('combined_buys',buy_sizing({**value,'input_pending_buy_exposure':None})[0])
        archived={**value,'buy_sizing_at_input':[immediate,limit],'suggested_orders':[{**order,'checks':['已到期']}]}
        self.assertEqual(presentation(archived)['buy_sizing'],[immediate,limit])

    def test_pending_exposure_counts_partial_order_remaining_quantity(self):
        from monitor.participation_review import pending_buy_exposure

        part={'status':'fresh','complete':True,'records':[
            {'action':'buy','status':'partially_filled','grams':5,'filled_grams':2,'price_cny':800},
            {'action':'buy','status':'cancelled','grams':9,'price_cny':800},
            {'action':'sell','status':'pending','grams':3,'price_cny':850}]}
        self.assertEqual(pending_buy_exposure({'pending_orders':part}),{'grams':3,'cost_cny':2400})
        part['records'][0]['filled_grams']=None
        self.assertIsNone(pending_buy_exposure({'pending_orders':part}))

    def test_annotation_does_not_rewrite_orders_or_create_notification_churn(self):
        order={'kind':'take_profit_stop_loss','grams':100.,'price_cny':900.,'loss_price_cny':840.}
        value={'suggested_orders':[order],'downside_scenarios':[scenario()]}
        signature=proposal_signature(value);s=source();before=deepcopy(s)
        annotate(value,s)
        self.assertEqual(proposal_signature(value),signature);self.assertEqual(s,before)
        self.assertEqual(order['account_effects']['stop_loss']['position_after_pct'],9.6812)
        text=format_proposals({'suggested_orders':[{**order,'reason':'测试情景','expires_at':(fixtures.NOW+timedelta(days=1)).isoformat()}]})
        self.assertIn('止损卖出后黄金仓位约9.7%',text)

    def test_new_private_valuation_fields_are_not_public_sources(self):
        _,risk=account_risk(source(),{'trade_risk_pct':4})
        ctx={'live_readonly_accounts':{'accounts':{'zheshang':{'account_risk':risk}}}}
        for key in ('gross_assets_cny','net_assets_cny','risk_budget_cny','gold_value_cny'):
            self.assertFalse(privacy_ok({'text':f'金额{risk[key]}'},ctx))


class DownsidePolicyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveProposalTests();self.f.setUp();self.addCleanup(self.f.tearDown)

    def test_model_input_and_saved_input_include_account_specific_risk(self):
        from monitor.jd_live_readonly import model_input
        inputs=model_input(self.f.s.conn,fixtures.NOW);bank=inputs['accounts']['zheshang']
        self.assertNotIn('risk_budget_cny',inputs['risk_profile'])
        self.assertIsNotNone(bank['position_pct']);self.assertGreater(bank['account_risk']['risk_budget_cny'],0)
        self.f.saved(self.f.value(downside_scenarios=[]))
        value=self.f.latest();self.assertEqual(value['schema_version'],4)
        self.assertEqual(value['input_account_risk'],bank['account_risk'])
        self.assertEqual(value['input_position_pct'],bank['position_pct'])
        self.assertIsNone(value['risk_profile']['historical_drawdown_pct'])

    def test_stress_test_or_hold_cannot_masquerade_as_stop_sell(self):
        buy=self.f.proposal(kind='limit_buy',grams=1,price_cny=830)
        stop=self.f.proposal(kind='take_profit_stop_loss',price_cny=880,loss_price_cny=820)
        value=self.f.value(suggested_orders=[buy,stop],downside_scenarios=[scenario(820)])
        self.f.saved(value);saved=self.f.latest()
        self.assertTrue(saved['suggested_orders'][1]['checks'])
        safe=notification_parts(saved)
        self.assertIsNotNone(safe);self.assertEqual([o['kind'] for o in safe['suggested_orders']],['limit_buy'])
        self.assertEqual(safe['downside_scenarios'][0]['preferred_action'],'hold')
        self.assertIsNotNone(saved['suggested_orders'][1]['account_effects']['stop_loss'])

    def test_new_stop_requires_matching_comparison_but_does_not_force_buy(self):
        stop=self.f.proposal(kind='take_profit_stop_loss',price_cny=880,loss_price_cny=820)
        for cases in ([],[scenario(819,'thesis_invalidation','sell')],[scenario(820,'risk_limit','hold')]):
            self.f.saved(self.f.value(suggested_orders=[stop],downside_scenarios=cases))
            self.assertTrue(self.f.latest()['suggested_orders'][0]['checks'])
        valid=scenario(820,'thesis_invalidation','sell');valid['basis']='假设在此失效，继续持有已无依据；按自选规模退出。'
        self.f.saved(self.f.value(suggested_orders=[stop],downside_scenarios=[valid]))
        self.assertEqual(self.f.latest()['checks'],[])
        self.f.saved(self.f.value(suggested_orders=[],downside_scenarios=[scenario(820,'entry_reference','buy')]))
        self.assertEqual(self.f.latest()['checks'],[])  # A hypothetical buy does not create a draft or execution.
        self.assertEqual(self.f.latest()['suggested_orders'],[])

    def test_downside_price_order_and_duplicates_are_local_warnings(self):
        value={'downside_scenarios':[scenario(900),scenario(900)],'suggested_orders':[]}
        errors=validate_downside(value,source(),require=True)
        self.assertTrue(errors);self.assertTrue(all(e.startswith('下跌情景：') for e in errors))
        original={'action':'hold','checks':errors,**value}
        self.assertIsNotNone(notification_parts(original))
