import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock,patch
from monitor.store import Store,utc_now
from monitor.config import Settings
from monitor.__main__ import Monitor
from monitor.wave_evidence import daily_reference,daily_indicators,calendar_window,jd_daily_samples
from monitor.ai_inputs import compact_context


class WaveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
        self.now=datetime.fromisoformat('2026-10-26T10:03:00+08:00')
        self.store.conn.execute('CREATE TABLE daily_bars(day TEXT PRIMARY KEY,xau_open REAL,xau_close REAL,usdcny REAL,open REAL,close REAL)')

    def tearDown(self):self.store.conn.close();self.tmp.cleanup()

    def tick(self,at,price):
        self.store.add_tick({'ts':datetime.fromisoformat(at),'jd_price':price,'market_open':True})

    def test_completed_proxy_dates_and_bounded_price_sequence(self):
        for n in range(100):
            day=(self.now-timedelta(days=n)).date()
            if day.weekday()<5:self.store.conn.execute('INSERT INTO daily_bars VALUES(?,?,?,?,?,?)',(day.isoformat(),4000-n,4001-n,7,900,901))
        self.store.conn.commit();r=daily_reference(self.store.conn,self.now)
        self.assertEqual(r['last_day'],'2026-10-23')
        self.assertEqual(len(r['recent_sessions']),30)
        self.assertEqual(list(r['recent_sessions'][-1]),['date','open_usd','close_usd'])
        self.assertEqual(r['recent_sessions'][-1]['close_usd'],3998)
        self.assertIn('60d',r['changes_pct'])
        self.assertLess(r['recent_sessions'][0]['date'],r['recent_sessions'][-1]['date'])

    def test_indicator_freshness_valid_ranges_and_missing_values(self):
        tv={'status':'ok','fetched_at':self.now.isoformat(),'rsi_1d':45,'sma20_1d':4000,'sma50_1d':4100}
        self.assertEqual(daily_indicators({'tv':tv},self.now)['status'],'fresh')
        self.assertEqual(daily_indicators({'tv':tv},self.now+timedelta(seconds=181))['status'],'unavailable')
        self.assertEqual(daily_indicators({'tv':tv},self.now-timedelta(hours=1))['status'],'unavailable')
        value=daily_indicators({'tv':dict(tv,rsi_1d=101,sma20_1d=float('nan'))},self.now)
        self.assertEqual(value['status'],'partial');self.assertIsNone(value['rsi']);self.assertIsNone(value['ma20_usd'])
        self.assertEqual(daily_indicators({},self.now)['status'],'unavailable')

    def test_seven_day_calendar_caps_prioritizes_and_hides_future_actuals(self):
        def event(days,tier=2):return {'time_utc':(self.now+timedelta(days=days)).isoformat(),'tier':tier,'actual':123,'released':True}
        self.assertEqual(calendar_window([None,{'time_utc':'2026-10-26T10:00:00'}],self.now)['events'],[])
        original=event(3,1)
        window=calendar_window([event(-2),event(-1),event(1),original,event(7),event(8)],self.now)
        self.assertEqual(window['total_in_window'],4)
        future=[e for e in window['events'] if datetime.fromisoformat(e['time_utc'])>self.now]
        self.assertTrue(all(e['actual'] is None and not e['released'] for e in future))
        self.assertEqual(original['actual'],123)
        limited=calendar_window([event(1),original],self.now,limit=1)
        self.assertTrue(limited['truncated']);self.assertEqual(limited['events'][0]['tier'],1)

    def test_jd_sampling_is_bj_dated_partial_and_excludes_future_or_closed_quotes(self):
        for at,p in [('2026-10-26T08:59:00+08:00',100),('2026-10-26T09:00:00+08:00',900),
                     ('2026-10-26T09:01:00+08:00',901),('2026-10-26T10:00:00+08:00',910),
                     ('2026-10-26T10:04:00+08:00',999),('2026-10-25T12:00:00+08:00',300)]:self.tick(at,p)
        r=jd_daily_samples(self.store.conn,self.now);self.assertEqual(len(r['days']),1)
        d=r['days'][0]
        self.assertEqual((d['first_price_cny'],d['last_price_cny'],d['sampled_low_cny'],d['sampled_high_cny']),(900,910,900,910))
        self.assertEqual(d['samples'],3);self.assertEqual(d['expected_minutes'],64)
        self.assertEqual(d['missing_minute_samples'],61);self.assertEqual(d['max_gap_minutes'],59)
        self.assertFalse(d['session_ended']);self.assertTrue(d['partial'])

    def test_jd_reports_missing_trading_dates_without_fabricated_bars(self):
        self.tick('2026-10-23T20:00:00+08:00',900)
        self.tick('2026-10-27T09:00:00+08:00',910)
        r=jd_daily_samples(self.store.conn,self.now+timedelta(days=1))
        self.assertEqual([d['date_bj'] for d in r['days']],['2026-10-23','2026-10-27'])
        self.assertIn('2026-10-26',r['missing_dates']);self.assertNotIn('2026-10-25',r['missing_dates'])
        self.assertTrue(r['days'][0]['session_ended'])

    def test_context_reuses_cached_data_and_compaction_keeps_new_evidence(self):
        now=utc_now();self.store.set_state('web_macro',{'fetched_at':now.isoformat(),'quotes':{},
          'calendar':[{'time_utc':(now+timedelta(days=6)).isoformat(),'tier':1,'title':'future-event'}]})
        m=Monitor.__new__(Monitor);m.settings=replace(Settings(),db_path=self.store.path if hasattr(self.store,'path') else Path(self.tmp.name)/'db',clawx_api_key='')
        with patch('monitor.jd_research.fetch',side_effect=AssertionError('no network')):
            c=m.ai_context({'ts':now,'xauusd':4000,'tv':{'status':'ok','fetched_at':now.isoformat(),'rsi_1d':45,'sma20_1d':4000,'sma50_1d':4100}},None)
        self.assertEqual(len(c['macro']['calendar']),1);self.assertEqual(c['macro']['calendar_window']['days_ahead'],7)
        self.assertEqual(c['daily_indicators']['status'],'fresh')
        value=compact_context(c)
        for key in ('daily_indicators','daily_trend_reference','jd_daily_samples'):self.assertEqual(c[key],value[key])
        self.assertIsNotNone(self.store.get_state('ai_inputs')['wave_evidence'])

    def test_ai_page_uses_model_archive_instead_of_latest_preparation(self):
        import test_four_account as fixtures
        from monitor.web import ai_section
        from monitor.wave_evidence import input_summary
        self.assertIsNone(input_summary({"daily_trend_reference":{"last_close_usd":4000}}))
        self.assertEqual(input_summary({"daily_indicators":{},"macro":{"status":{"calendar":"stale_or_missing"}}})["calendar"]["status"],"stale_or_missing")
        f=fixtures.FourAccountTests();f.setUp()
        try:
            ctx=f.context();ctx['daily_trend_reference']={'status':'available','last_day':'2026-10-23','recent_sessions':[{'date':'2026-10-23','close_usd':4000}]}
            f.a._run_codex=Mock(return_value=('notes',[],f.value()));f.a.analyze(ctx,'test',run_id='actual-input')
            f.m.store.set_state('ai_inputs',{'wave_evidence':{'daily_reference':{'sessions':99}}})
            actual=ai_section(f.m.store.conn,f.settings)['analysts'][0]['wave_input']
            self.assertEqual(actual['daily_reference']['sessions'],1)
        finally:f.tearDown()
