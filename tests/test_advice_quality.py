"""Calendar coverage, continuation deduplication and complete model explanations."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monitor.config import Settings
from monitor.store import Store
from monitor.official_calendar import parse,merge,supplement
from monitor.jd_live_semantics import plan_terms,same_plan
from monitor.jd_live_text import incomplete_fields
import test_live_notice_rules as notices
import test_live_proposals as live

NOW=datetime(2026,11,17,7,tzinfo=timezone.utc)
HTML='''<div class="panel-body"><div class="row"><div class="col-xs-2"><p>2:00 p.m.</p></div><div class="col-xs-7"><p>FOMC Minutes</p><p>Meeting of October 27-28</p></div><div class="col-xs-3"><p>18</p></div></div></div><div class="panel-body"><div class="col-xs-2">9:15 a.m.</div><div class="col-xs-7">G.17 - Industrial Production and Capacity Utilization</div><div class="col-xs-3">17</div></div>'''


class CalendarQualityTests(unittest.TestCase):
    def test_official_times_dst_and_no_invented_actual(self):
        rows=parse(HTML,2026,11,'https://www.federalreserve.gov/example')
        self.assertEqual([e['time_bj'] for e in rows],['11-19 03:00','11-17 22:15'])
        summer=parse(HTML,2026,7,'https://www.federalreserve.gov/example')
        self.assertEqual(summer[0]['time_bj'],'07-19 02:00')
        self.assertTrue(all(e['actual'] is None and not e['released'] for e in rows))
        with self.assertRaises(ValueError):parse('<h1>Unavailable</h1>',2026,11,'example')

    def test_official_merge_keeps_released_zero_and_provider_forecast(self):
        official=parse(HTML,2026,11,'example')
        primary=[{**official[1],'actual':0,'forecast':.2,'released':True}]
        result=merge(primary,official)
        self.assertEqual(len(result),2)
        event=next(e for e in result if e['title']=='Industrial Production MoM')
        self.assertEqual(event['actual'],0);self.assertEqual(event['forecast'],.2);self.assertTrue(event['released'])
        self.assertEqual(event['schedule_source'],'Federal Reserve')

    def test_official_cache_retry_and_error_do_not_refresh_success_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'test.db')
            with patch('monitor.official_calendar._fetch',return_value=HTML) as fetch:
                a,_=supplement(s,NOW);supplement(s,NOW+timedelta(minutes=30));self.assertEqual(fetch.call_count,1)
            with patch('monitor.official_calendar._fetch',side_effect=TimeoutError) as fetch:
                b,status=supplement(s,NOW+timedelta(hours=7));supplement(s,NOW+timedelta(hours=7,minutes=1))
                self.assertEqual(fetch.call_count,1)
                self.assertEqual(status[0]['fetched_at'],NOW.isoformat());self.assertEqual(status[0]['status'],'cached')
                self.assertEqual(len(a),len(b))
            s.conn.close()

    def test_provider_importance_does_not_hide_named_major_releases(self):
        import macro_data
        for title in ('Industrial Production MoM','FOMC Minutes','Retail Sales Control Group'):
            self.assertEqual(macro_data.event_tier({'country':'US','title':title,'importance':0}),2)

    def test_public_calendar_distinguishes_documents_and_reveals_no_private_fields(self):
        from monitor.public_market import calendar_data
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'test.db')
            rows=parse(HTML,2026,11,'https://www.federalreserve.gov/example')
            rows[0]['private_account']='PRIVATE-MARKER'
            s.set_state('web_macro',{'calendar':rows,'calendar_fetched_at':NOW.isoformat()})
            value=calendar_data(s.conn,NOW)
            self.assertIn('美联储',value['source'])
            self.assertTrue(next(e for e in value['items'] if e['title']=='FOMC Minutes')['document_event'])
            self.assertFalse(next(e for e in value['items'] if e['title']=='Industrial Production MoM')['document_event'])
            self.assertNotIn('PRIVATE-MARKER',str(value));s.conn.close()


class AdviceQualityTests(unittest.TestCase):
    def test_live_schema_has_no_ref_siblings_and_keeps_field_instructions(self):
        from monitor.ai import ClaudeCodeResult,strict_schema
        schema=ClaudeCodeResult.model_json_schema();original=deepcopy(schema)
        strict=strict_schema(schema)
        def walk(value):
            if isinstance(value,dict):
                if '$ref' in value:self.assertEqual(list(value),['$ref'])
                if value.get('type')=='object':
                    self.assertEqual(value.get('required'),list(value.get('properties',{})))
                    self.assertIs(value.get('additionalProperties'),False)
                self.assertNotIn('default',value)
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(strict);self.assertEqual(schema,original)
        field=strict['properties']['live_account_advice']
        self.assertEqual(field['description'],schema['properties']['live_account_advice']['description'])
        self.assertEqual(field['anyOf'],[{'$ref':'#/$defs/LiveAdviceSet'}])

    def test_expiry_review_survives_small_hard_limit_moves_and_restart(self):
        import test_live_structured as structured
        from monitor.jd_live_review import request_reviews
        f=structured.StructuredAdviceTests();f.setUp();self.addCleanup(f.tearDown)
        f.data['cash_cny']=20000;f.fresh_at(structured.NOW)
        v=f.value(action='keep',grams=2,price_condition='at_or_below',price_cny=855,valid_for_minutes=15)
        v['action']='buy';f.saved(v)
        for n,price in ((16,855.5),(32,856)):
            now=structured.NOW+timedelta(minutes=n);f.fresh_at(now);request_reviews(f.s,now)
            self.assertEqual(len(f.expiry_events()),1 if n==16 else 0)
            f.cover();f.saved({**v,'price_cny':price},now)
            f.s.conn.close();f.s=Store(f.path)
        now=structured.NOW+timedelta(minutes=33);f.fresh_at(now)
        f.saved({**v,'price_cny':860},now);request_reviews(f.s,now)
        now+=timedelta(minutes=16);f.fresh_at(now);request_reviews(f.s,now)
        self.assertEqual(len(f.expiry_events()),1)

    def test_small_entry_moves_are_economic_renewals_but_guards_stay_exact(self):
        a={'action':'buy','grams':69,'price_condition':'at_or_below','price_cny':832,
            'position_path':{'target_position_pct':20,'boundary_price_cny':804,'exit_scope':'plan'}}
        before=deepcopy(a);b={**a,'price_cny':832.6}
        self.assertTrue(same_plan(plan_terms(a),plan_terms(b)));self.assertEqual(a,before)
        for change in ({'price_cny':836},{'grams':70},{'price_condition':'at_or_above'},
                       {'position_path':{**a['position_path'],'boundary_price_cny':803}},
                       {'position_path':{**a['position_path'],'target_position_pct':30}}):
            self.assertFalse(same_plan(plan_terms(a),plan_terms({**a,**change})))

    def test_recommended_entry_terms_survive_json_persistence(self):
        import json
        at='2026-11-19T10:00:00+08:00'
        v={'action':'hold','suggested_orders':[{'kind':'limit_buy','grams':2,'price_cny':840,'expires_at':at}],
           'limit_buy_reviews':[{'decision':'recommend','entry_price_cny':840,'target_price_cny':860,
                                'risk_exit_cny':820,'grams':2,'expires_at':at}]}
        terms=plan_terms(v);self.assertEqual(len(terms['entry']),1)
        self.assertTrue(same_plan(json.loads(json.dumps(terms)),plan_terms(v)))

    def test_notice_price_drift_is_anchored_to_last_notice_not_last_round(self):
        f=notices.MilestoneNotificationTests();f.setUp();self.addCleanup(f.tearDown)
        from monitor.notification_control import set_subscription
        from monitor.notification_outbox import dispatch
        set_subscription(f.s.conn,'codex:live_zheshang',True)
        v=notices.hold();v.update(action='buy',grams=2,price_condition='at_or_below',price_cny=850)
        first=f.events(f.saved(value=v));self.assertEqual(len(first),1);dispatch(f.s,f.n,first)
        for price in (851,852):self.assertEqual(f.events(f.saved(value={**v,'price_cny':price})),[])
        self.assertEqual(len(f.events(f.saved(value={**v,'price_cny':854}))),1)
        # The daily/actual-position milestones remain independent of price tolerance.
        f.data['cash_cny']+=100;f.snapshot()
        events=f.events(f.saved(value={**v,'price_cny':852}));self.assertEqual(len(events),1)
        self.assertIn('position_changed',events[0]['notification_reasons'])

    def test_incomplete_text_is_preserved_but_cannot_be_notified_as_valid_advice(self):
        self.assertEqual(incomplete_fields({'reassessment':{'price_basis':'退出损失约3.'},'position_path':{'continuation':'另作保留决'}}),['价位依据','后续计划'])
        self.assertEqual(incomplete_fields({'reassessment':{'price_basis':'退出损失约3.5%。'},'position_path':{'continuation':'重新分析。'}}),[])
        f=live.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        from monitor.jd_live_readonly import model_input
        from monitor.jd_live_advice import save
        from monitor.jd_live_semantics import notification_parts
        inputs=model_input(f.s.conn,live.NOW);inputs['text_contract']='complete_sentences_v1'
        value=f.value();value['reassessment']['price_basis']='退出损失约3.'
        save(f.s.conn,'codex',100,{'zheshang':value},inputs,live.NOW);f.s.conn.commit()
        saved=f.latest();self.assertTrue(any('分析文字未完整结束' in t for t in saved['checks']))
        self.assertIsNone(notification_parts(saved));self.assertEqual(saved['reassessment']['price_basis'],'退出损失约3.')
