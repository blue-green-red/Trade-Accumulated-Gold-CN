import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from monitor.config import Settings
from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,model_input,accounts
from monitor.jd_live_advice import save,PROMPT,notification_events
from monitor.jd_live_feedback import input_quote,strategy_review
from monitor.jd_live_review import request_reviews
from monitor.ai_events import read,acknowledge
from test_jd_live import payload

NOW=datetime(2026,11,3,6,tzinfo=timezone.utc)


class BankMoveReviewTests(unittest.TestCase):
    def setUp(self):
        session=patch('monitor.trading_hours.trading_session',return_value={'is_open':True});session.start();self.addCleanup(session.stop)
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'review.db'
        self.s=Store(self.path);ensure_schema(self.s.conn)
        self.settings=replace(Settings(),db_path=self.path,ai_periodic_min_move_pct=.4)
        self.s.set_state('ai_status:codex',{'available':True});self.s.set_state('ai_status:claude',{'available':True})
        self.s.set_state('model_control:claude',{'enabled':False,'generation':0,'changed_at':None})
        self.data={}
        for bank in ('zheshang','minsheng'):
            d=parse_account(bank,payload(bank));d.update(cash_cny=100.,
                quote={'sell_price_cny':850.,'buy_price_cny':850.,'quoted_at':NOW.isoformat()},
                pending_order_count=0,pending_orders={'records':[],'total_count':0,'complete':True},
                coverage={'pending_orders':'available'},component_times={'pending_orders':NOW.isoformat()})
            self.data[bank]=d;record_success(self.s.conn,bank,d,NOW.isoformat())
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def advice(self,thesis='weakened',**changes):
        value={'action':'hold','grams':None,'price_condition':'reference','price_cny':999.,'valid_for_minutes':240,
               'reason':'比较继续持有与减仓','risk_note':'假设会变化',
               'reassessment':{'change_summary':'重新核验','thesis_status':thesis,'thesis':'情景假设','price_basis':'依据本轮数据判断'},
               'suggested_orders':[{'kind':'limit_sell','grams':2.,'price_cny':875.,'expires_at':(NOW+timedelta(days=1)).isoformat(),'reason':'反弹减仓情景'}]}
        value.update(changes);return value
    def saved(self,bank='zheshang',view=1,model='codex',now=NOW,**changes):
        save(self.s.conn,model,view,{bank:self.advice(**changes)},model_input(self.s.conn,now),now);self.s.conn.commit()
        return next(a for a in accounts(self.s.conn,now) if a['bank']==bank)['advice'][0]
    def price(self,bank='zheshang',value=846.6,seconds=1,**changes):
        d=copy.deepcopy(self.data[bank]);d.update(changes)
        d['quote']={'sell_price_cny':value,'buy_price_cny':value,'quoted_at':(NOW+timedelta(seconds=seconds)).isoformat()}
        record_success(self.s.conn,bank,d,NOW.isoformat())
    def review(self,seconds=1):request_reviews(self.s,NOW+timedelta(seconds=seconds),settings=self.settings)
    def moves(self,model='codex'):return [e for e in read(self.s.conn,model)['pending'] if e['id'].startswith('live-move:')]

    def test_bank_move_uses_exact_input_not_hypothetical_output_and_deduplicates(self):
        value=self.saved();self.assertEqual(value['input_quote_cny'],850)
        self.assertEqual(value['price_cny'],999)
        self.price(value=846.6);self.review();self.review()
        events=self.moves();self.assertEqual(len(events),1)
        self.assertEqual(events[0]['data']['previous_quote_cny'],850)
        self.assertEqual(events[0]['data']['change_pct'],-.4)
        with self.s.conn:acknowledge(self.s.conn,'codex',[events[0]['id']])
        self.price(value=840,seconds=2);self.review(2)
        self.assertEqual(self.moves(),[])

    def test_threshold_up_down_boundary_and_small_move(self):
        self.saved()
        self.price(value=846.601);self.review();self.assertEqual(self.moves(),[])
        self.price(value=853.4,seconds=2);self.review(2);self.assertEqual(len(self.moves()),1)

    def test_configured_interval_and_disable_are_respected(self):
        self.saved();self.price(value=846.6)
        request_reviews(self.s,NOW+timedelta(seconds=1),settings=replace(self.settings,ai_short_review_minutes=5))
        self.assertEqual(self.moves(),[])
        self.price(value=846.6,seconds=301)
        request_reviews(self.s,NOW+timedelta(seconds=301),settings=replace(self.settings,ai_short_review_minutes=-1))
        self.assertEqual(self.moves(),[])
        request_reviews(self.s,NOW+timedelta(seconds=301),settings=replace(self.settings,ai_short_review_minutes=5))
        self.assertEqual(len(self.moves()),1)

    def test_new_analysis_reanchors_following_move(self):
        self.saved();self.price(value=846.6);self.review()
        with self.s.conn:acknowledge(self.s.conn,'codex',[e['id'] for e in self.moves()])
        self.saved(view=2,now=NOW+timedelta(seconds=1))
        self.price(value=845,seconds=2);self.review(2);self.assertEqual(self.moves(),[])
        self.price(value=842,seconds=3);self.review(3);self.assertEqual(len(self.moves()),1)
        self.assertEqual(self.moves()[0]['data']['previous_quote_cny'],846.6)

    def test_invalid_stale_and_regressive_quotes_cannot_trigger(self):
        advice=self.saved()
        from monitor.jd_live_review import request_bank_move
        for value in (None,False,float('nan'),float('inf'),-1):
            with self.subTest(value=value):
                account={'bank':'zheshang','connected':True,'quote':{'status':'fresh','sell_price_cny':value,'quoted_at':NOW.isoformat()}}
                request_bank_move(self.s,account,advice,.4,NOW)
                self.assertEqual(self.moves(),[])
        self.price(value=840,seconds=-1);self.review();self.assertEqual(self.moves(),[])
        self.price(value=840,seconds=-181);self.review();self.assertEqual(self.moves(),[])

    def test_two_banks_share_one_pending_batch_and_models_are_independent(self):
        self.s.set_state('model_control:claude',{'enabled':True,'generation':0,'changed_at':None})
        for bank in self.data:
            self.saved(bank=bank);self.saved(bank=bank,model='claude')
            self.price(bank=bank,value=846.6)
        self.review();self.review()
        self.assertEqual(len(self.moves()),2);self.assertEqual(len(self.moves('claude')),2)
        self.assertEqual({e['data']['bank'] for e in self.moves()},{'zheshang','minsheng'})
        self.assertEqual(self.s.conn.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0],0)
        # Enqueueing review facts must not make a model call or consume a daily run.
        self.assertNotIn('ai_runs',{r[0] for r in self.s.conn.execute("SELECT name FROM sqlite_master")})

    def test_each_model_uses_its_own_last_bank_input(self):
        self.saved()
        self.s.set_state('model_control:claude',{'enabled':True,'generation':0,'changed_at':None})
        self.price(value=855,seconds=1);self.saved(model='claude',now=NOW+timedelta(seconds=1))
        self.price(value=851,seconds=2);self.review(2)
        self.assertEqual(self.moves('codex'),[]);self.assertEqual(len(self.moves('claude')),1)
        self.assertEqual(self.moves('claude')[0]['data']['previous_quote_cny'],855)

    def test_each_bank_uses_its_own_quote(self):
        self.saved(bank='zheshang')
        self.price(bank='minsheng',value=900,seconds=1);self.saved(bank='minsheng',now=NOW+timedelta(seconds=1))
        self.price(bank='zheshang',value=846.6,seconds=2);self.review(2)
        self.assertEqual(len(self.moves()),1);self.assertEqual(self.moves()[0]['data']['bank'],'zheshang')

    def test_closed_or_disabled_model_does_not_enqueue(self):
        self.saved();self.price(value=840)
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':False}):self.review()
        self.assertEqual(self.moves(),[])
        self.s.set_state('model_control:codex',{'enabled':False});self.review();self.assertEqual(self.moves(),[])

    def test_recovery_from_exact_archive_never_uses_output_reference(self):
        value=self.saved();raw=json.loads(self.s.conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=?',(value['id'],)).fetchone()[0]);raw.pop('input_quote_cny')
        self.s.conn.execute('UPDATE jd_live_advice SET advice_json=? WHERE id=?',(json.dumps(raw),value['id']))
        self.s.conn.execute('CREATE TABLE ai_views(run_id TEXT)');self.s.conn.execute('INSERT INTO ai_views VALUES(?)',('same-round',));self.s.conn.commit()
        self.s.set_state('ai_input:same-round',{'context':{'live_readonly_accounts':{'accounts':{'zheshang':{
            'status':'fresh','quote':{'status':'fresh','sell_price_cny':850,'quoted_at':NOW.isoformat()}}}}}})
        value=accounts(self.s.conn,NOW)[0]['advice'][0]
        self.assertEqual(input_quote(self.s.conn,value)['price_cny'],850)
        self.price(value=846.6);self.review();self.assertEqual(len(self.moves()),1)
        with self.s.conn:self.s.conn.execute("DELETE FROM state WHERE key='ai_input:same-round'")
        self.assertIsNone(input_quote(self.s.conn,value))

    def test_feedback_summarizes_unchanged_plan_and_repeated_weakening(self):
        self.saved(view=1);self.saved(view=2,now=NOW+timedelta(seconds=1),thesis='valid')
        self.saved(view=3,now=NOW+timedelta(seconds=2))
        self.price(value=846.6,seconds=3)
        source=model_input(self.s.conn,NOW+timedelta(seconds=3))['accounts']['zheshang']
        review=source['previous_advice'][0]['strategy_review']
        self.assertEqual(review['same_plan_rounds'],3);self.assertEqual(review['weakened_rounds'],2)
        self.assertTrue(review['revalidate_unchanged_plan']);self.assertEqual(review['move_since_previous_pct'],-.4)
        self.saved(view=4,now=NOW+timedelta(seconds=3),suggested_orders=[])
        review=model_input(self.s.conn,NOW+timedelta(seconds=3))['accounts']['zheshang']['previous_advice'][0]['strategy_review']
        self.assertEqual(review['same_plan_rounds'],1);self.assertFalse(review['revalidate_unchanged_plan'])

    def test_rolling_deadline_or_rewording_is_not_a_new_trade_plan(self):
        self.saved(view=1)
        orders=self.advice()['suggested_orders'];orders[0]['expires_at']=(NOW+timedelta(days=2)).isoformat();orders[0]['reason']='补充新的解释'
        self.saved(view=2,now=NOW+timedelta(seconds=1),suggested_orders=orders,reason='重新表述持有理由')
        feedback=model_input(self.s.conn,NOW+timedelta(seconds=1))['accounts']['zheshang']['previous_advice'][0]['strategy_review']
        self.assertEqual(feedback['same_plan_rounds'],2);self.assertTrue(feedback['revalidate_unchanged_plan'])

    def test_reference_prices_do_not_reset_same_plan_but_hard_thresholds_and_size_do(self):
        from monitor.jd_live_feedback import plan_signature
        value=self.advice(action='sell',grams=2.,price_cny=850.,suggested_orders=[])
        quote_update={**value,'price_cny':849.,'reason':'参考报价变化'}
        self.assertEqual(plan_signature(value),plan_signature(quote_update))
        self.assertNotEqual(plan_signature(value),plan_signature({**value,'grams':3.}))
        limit={**value,'price_condition':'at_or_above'}
        self.assertNotEqual(plan_signature(limit),plan_signature({**limit,'price_cny':849.}))
        for i,price in enumerate((850.,849.5,849.),1):
            self.saved(view=i,now=NOW+timedelta(seconds=i),action='sell',grams=2.,price_cny=price,suggested_orders=[])
        account=accounts(self.s.conn,NOW+timedelta(seconds=3))[0]
        feedback=strategy_review(self.s.conn,account,'codex',NOW+timedelta(seconds=3))
        self.assertEqual(feedback['same_plan_rounds'],3);self.assertEqual(feedback['weakened_rounds'],3)

    def test_requested_real_deadline_is_meaningful_unlike_advisory_expiry(self):
        from monitor.jd_live_feedback import plan_signature
        value=self.advice(order_management=[{'order_key':'a'*24,'action':'replace',
            'replacement':{'expires_at':(NOW+timedelta(hours=8)).isoformat(),'grams':None},'reason':'缩短原单时间'}])
        advisory={**value,'valid_for_minutes':15}
        self.assertEqual(plan_signature(value),plan_signature(advisory))
        changed=copy.deepcopy(value);changed['order_management'][0]['replacement']['expires_at']=(NOW+timedelta(hours=9)).isoformat()
        self.assertNotEqual(plan_signature(value),plan_signature(changed))

    def test_account_revision_change_breaks_consecutive_plan_feedback(self):
        self.saved(view=1)
        self.price(seconds=1,value=850.,cash_cny=101.)
        self.saved(view=2,now=NOW+timedelta(seconds=1))
        account=accounts(self.s.conn,NOW+timedelta(seconds=1))[0]
        self.assertEqual(strategy_review(self.s.conn,account,'codex',NOW+timedelta(seconds=1))['same_plan_rounds'],1)

    def test_feedback_has_no_future_advice_or_other_models_and_retains_input_provenance(self):
        self.saved(view=1)
        self.s.set_state('model_control:claude',{'enabled':True,'generation':0,'changed_at':None})
        self.saved(view=2,model='claude',thesis='valid')
        self.saved(view=3,now=NOW+timedelta(minutes=1))
        account=accounts(self.s.conn,NOW+timedelta(minutes=1))[0]
        review=strategy_review(self.s.conn,account,'codex',NOW)
        self.assertEqual(review['same_plan_rounds'],1)
        source=model_input(self.s.conn,NOW+timedelta(minutes=1))
        saved=self.saved(view=4,now=NOW+timedelta(minutes=1))
        self.assertIn('input_strategy_review',saved)
        # The latest input's history and actual bank anchor are saved, without rewriting an old round.
        first=json.loads(self.s.conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=1').fetchone()[0])
        self.assertNotIn('input_strategy_review',first)
        self.assertEqual(first['input_quote_cny'],850)

    def test_repeated_weakening_requests_one_revalidation_then_does_not_loop(self):
        self.saved(view=1);self.saved(view=2,now=NOW+timedelta(seconds=1))
        self.review(1);self.review(1)
        events=[e for e in read(self.s.conn,'codex')['pending'] if e['id'].startswith('live-plan-review:')]
        self.assertEqual(len(events),1)
        with self.s.conn:acknowledge(self.s.conn,'codex',[e['id'] for e in events])
        latest=self.saved(view=3,now=NOW+timedelta(seconds=2))
        self.assertTrue(latest['input_strategy_review']['revalidate_unchanged_plan'])
        self.review(2);self.review(2)
        self.assertFalse(any(e['id'].startswith('live-plan-review:') for e in read(self.s.conn,'codex')['pending']))

    def test_prompt_reuses_existing_fields_instead_of_forcing_trade_or_new_schema(self):
        self.assertEqual(PROMPT.count('previous_advice.strategy_review'),1)
        self.assertIn('price_basis给当前证据与目标可达依据',PROMPT)
        self.assertIn('比较自主选择规模的当下参与与等待',PROMPT)
        self.assertIn('扣费收益、计划损失和踏空代价',PROMPT)
        self.assertIn('选等待须说明其具体优势',PROMPT)
        self.assertIn('不要求确认底部、趋势反转或多项利好同时出现',PROMPT)
        self.assertIn('情景假设须标明，无法估计直说，不编造价位',PROMPT)
        self.assertIn('不强制交易',PROMPT)

    def test_international_and_two_bank_triggers_are_covered_by_one_model_call(self):
        from monitor.ai import AIAnalyst,MarketView
        from monitor.cadence import request_periodic,usage
        from unittest.mock import Mock
        from test_ai_event_timing import result
        settings=replace(self.settings,ai_short_review_minutes=0,ai_schedule_bj=())
        analyst=AIAnalyst(settings,self.s,'codex','codex')
        self.s.conn.execute('INSERT INTO ai_views(ts,xauusd,analyst,error) VALUES(?,?,?,NULL)',((NOW-timedelta(minutes=1)).isoformat(),4000,'codex'))
        self.s.conn.commit()
        for bank in self.data:
            self.saved(bank=bank);self.price(bank=bank,value=846.6)
        before=usage(self.s.conn,'codex',NOW)
        request_periodic(settings,self.s,{'codex':analyst},{'xauusd':4020},NOW)
        self.review()
        events=read(self.s.conn,'codex')['pending'];self.assertEqual(len(events),3)
        self.assertEqual(usage(self.s.conn,'codex',NOW),before)
        answer=MarketView.model_validate({**result().model_dump(),'live_account_advice':{bank:self.advice() for bank in self.data}})
        analyst._run_codex=Mock(return_value=('notes',[],answer))
        context={'xauusd':4020,'snapshot_at':NOW.isoformat(),'live_readonly_accounts':model_input(self.s.conn,NOW+timedelta(seconds=1)),
                 'event_coverage':{'codex':[e['id'] for e in events]}}
        self.assertIsNotNone(analyst.analyze(context,'combined-review',run_id='combined-review'))
        analyst._run_codex.assert_called_once()
        self.assertEqual(read(self.s.conn,'codex')['pending'],[])
        self.assertEqual(usage(self.s.conn,'codex',NOW),before+1)

class BankReviewBrowserTests(unittest.TestCase):
    def test_real_advice_shows_review_background_and_cadence_mentions_bank_quotes(self):
        from playwright.sync_api import sync_playwright
        from test_dashboard_browser import local_dashboard
        now=datetime.now(timezone.utc)
        with local_dashboard() as (f,url),sync_playwright() as p:
            value={'action':'hold','grams':None,'price_condition':'reference','price_cny':999,'valid_for_minutes':240,
                   'reason':'等待、现价部分减仓与退出的扣费比较','risk_note':'情景有不确定性',
                   'reassessment':{'change_summary':'重新核验当前证据','thesis_status':'weakened','thesis':'反弹假设减弱','price_basis':'沿用目标需要新的证据'},
                   'suggested_orders':[{'kind':'limit_sell','grams':2,'price_cny':875,'expires_at':(now+timedelta(days=1)).isoformat(),'reason':'反弹兑现假设'}]}
            for i in range(3):
                at=now-timedelta(minutes=2-i)
                d=parse_account('zheshang',payload('zheshang'));d.update(cash_cny=100,
                    quote={'buy_price_cny':850 if i<2 else 845,'sell_price_cny':850 if i<2 else 845,'quoted_at':at.isoformat()},
                    pending_order_count=0,pending_orders={'records':[],'total_count':0,'complete':True},
                    coverage={'pending_orders':'available'},component_times={'pending_orders':at.isoformat()})
                record_success(f.m.store.conn,'zheshang',d,at.isoformat())
                save(f.m.store.conn,'codex',10000+i,{'zheshang':value},model_input(f.m.store.conn,at),at)
                f.m.store.conn.commit()
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page(viewport={'width':390,'height':844});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url+'/#strategies/live/zheshang/advice')
                page.get_by_text('本轮复核背景',exact=True).wait_for()
                self.assertIn('-0.59%',page.locator('#panel-strategies').inner_text())
                self.assertIn('同一交易方案连续 2 轮',page.locator('#panel-strategies').inner_text())
                self.assertIn('本轮重点复核旧价位',page.locator('#panel-strategies').inner_text())
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.get_by_role('tab',name='AI',exact=True).click()
                page.get_by_role('tab',name='模型控制',exact=True).click()
                page.get_by_role('heading',name='分析节奏与执行规则').wait_for()
                self.assertIn('任一真实账户银行价',page.locator('#panel-ai').inner_text())
                self.assertEqual(errors,[])
            finally:browser.close()
