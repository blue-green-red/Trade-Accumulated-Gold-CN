import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from monitor.public_market import news_data, calendar_data, indicators_data
from monitor.public_research import backfill, history, assessment_projection
from monitor.public_ai import save, latest
from monitor.controls import set_enabled
from test_public_site import site
from test_public_ai import content


class ExpandedPublicTests(unittest.TestCase):
    def test_public_wording_hides_provider_and_sampling_notes_without_changing_stored_content(self):
        from monitor.public_presentation import present,clean_text
        with site() as (store,settings,url):
            source={'analyst':'codex','available_analysts':['codex'],'market_assessment':{'confidence':'high','bias_short':'bullish'},
                    'analysis':{'outlook':'Codex认为震荡，京东采样不完整，但美元走弱。',
                                'risk':'京东报价缺失时需等待更新。'}}
            data=present(source,settings)
            self.assertEqual(source['market_assessment']['confidence'],'high')
            self.assertEqual(data['analysis']['outlook'],'金析AI认为震荡，但美元走弱。')
            self.assertEqual(data['analysis']['risk'],source['analysis']['risk'])
            self.assertEqual(data['model'],'primary')
            self.assertNotIn('confidence',data['market_assessment'])
            self.assertEqual(clean_text('京东采样并非完整日线。建议关注美元。'),'建议关注美元。')
            from dataclasses import replace
            policy=present({},replace(settings,ai_periodic_min_move_pct=.7))
            self.assertEqual(policy['update_policy']['move_threshold_pct'],.7)
            self.assertEqual(policy['reference_validity']['move_threshold_pct'],.4)

    def test_news_calendar_and_indicators_allowlist_mask_future_and_keep_zero(self):
        with site() as (store,settings,url):
            now=datetime.now(timezone.utc)
            at=now.astimezone(__import__('monitor.config',fromlist=['BJ']).BJ)
            store.set_state('jin10_flash',[
                {'time_bj':at.strftime('%Y-%m-%d %H:%M:%S'),'text':'黄金相关新消息','important':True,'account':'DO-NOT-PUBLISH'},
                {'time_bj':at.strftime('%Y-%m-%d %H:%M:%S'),'text':'黄金 相关新消息','important':True},
                {'time_bj':(at-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),'text':'旧消息','important':True}])
            def event(hours,actual):return {'time_utc':(now+timedelta(hours=hours)).isoformat().replace('+00:00','Z'),'country':'US','tier':1,
                'title':'JOLTs Job Openings','actual':actual,'forecast':0,'previous':1,'released':True,'private':'DO-NOT-PUBLISH'}
            store.set_state('web_macro',{'calendar':[event(1,99),event(-1,0)],'calendar_fetched_at':now.isoformat(),
                'quotes_fetched_at':now.isoformat(),'quotes':{'美国10年期国债收益率':{'value':0,'change_pct':-.1,'account':'DO-NOT-PUBLISH'},
                '美国2年期国债收益率':{'value':-.2},'private':'DO-NOT-PUBLISH'},'quota':'DO-NOT-PUBLISH'})
            snap=store.get_state('web_snapshot');snap['daily_indicators']={'rsi':0,'ma20_usd':4000,'ma50_usd':4100,'fetched_at':now.isoformat(),'private':'DO-NOT-PUBLISH'};store.set_state('web_snapshot',snap)
            news=news_data(store.conn,now);cal=calendar_data(store.conn,now);ind=indicators_data(store.conn,now)
            self.assertEqual(len(news['items']),1)
            self.assertIsNone(next(e for e in cal['items'] if not e['released'])['actual'])
            self.assertEqual(next(e for e in cal['items'] if e['released'])['actual'],0)
            self.assertEqual(ind['quotes'][1]['value'],0)
            self.assertEqual(ind['quotes'][2]['value'],-.2)
            self.assertEqual(ind['technical']['rsi'],0)
            self.assertNotIn('DO-NOT-PUBLISH',json.dumps([news,cal,ind]))

    def test_offline_backfill_requires_exact_view_identity_and_anonymous_reads_use_public_only(self):
        with site() as (store,settings,url):
            c=store.conn;r=c.execute('SELECT * FROM public_ai_reports').fetchone()
            c.execute('CREATE TABLE ai_views(analyst TEXT,ts TEXT,view_json TEXT,notes TEXT)')
            private={'bias_short':'bearish','bias_long':'neutral','confidence':'medium','p_up_3d':.2,'p_down_3d':.5,
                     'p_up_3m':.3,'p_down_3m':.4,'account_decisions':{'cash':'DO-NOT-PUBLISH'},'summary':'DO-NOT-PUBLISH'}
            c.execute('INSERT INTO ai_views(rowid,analyst,ts,view_json,notes) VALUES(?,?,?,?,?)',
                (r['view_id'],'codex','2000-01-01T00:00:00+00:00',json.dumps(private),'DO-NOT-PUBLISH'))
            c.execute('UPDATE public_ai_reports SET assessment_json=NULL,evidence_json=NULL')
            self.assertEqual(backfill(c),0)
            c.execute('UPDATE ai_views SET ts=?',(r['created_at'],))
            self.assertEqual(backfill(c),1)
            self.assertEqual(backfill(c),0)
            c.execute('DROP TABLE ai_views');c.commit()
            queries=[];c.set_trace_callback(queries.append)
            data=latest(c,settings)
            archive=history(c,settings,{'analyst':['codex']})
            self.assertEqual(data['market_assessment']['confidence'],'medium')
            self.assertEqual(archive['items'][0]['market_assessment']['p_down_3d'],.5)
            self.assertNotIn('DO-NOT-PUBLISH',json.dumps([data,archive]))
            self.assertFalse(any('FROM ai_views' in q for q in queries))

    def test_history_pages_public_records_only_and_scopes_model_and_expanded_detail(self):
        with site() as (store,settings,url):
            c=store.conn;now=datetime.now(timezone.utc)
            assessment={'bias_short':'bullish','bias_long':'neutral','confidence':'high','p_up_3d':.5,'p_down_3d':.2}
            for i in range(2,16):
                at=now-timedelta(minutes=16-i)
                save(c,'codex',0,i,at.isoformat(),at.isoformat(),content(),{},assessment=assessment)
            save(c,'codex',0,20,now.isoformat(),now.isoformat(),None,{},assessment=assessment)
            c.commit()
            first=history(c,settings,{'limit':['10']},now)
            self.assertEqual(len(first['items']),10)
            second=history(c,settings,{'limit':['10'],'before':[str(first['next_before'])]},now)
            self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
            self.assertEqual(len(first['items'])+len(second['items']),15)
            rid=first['items'][0]['id']
            detail=history(c,settings,{'report_id':[str(rid)]},now)['report']
            self.assertTrue(detail['historical']);self.assertIn('analysis',detail)
            self.assertNotIn('recommendations',detail);self.assertNotIn('view_id',detail)
            set_enabled(c,'codex',False)
            self.assertEqual(history(c,settings,{})['items'],[])
            self.assertIsNone(history(c,settings,{'report_id':[str(rid)]})['report'])
            for bad in ({'before':['-1']},{'limit':['500']},{'days':['31']},{'report_id':['1'],'limit':['2']},{'analyst':['codex','claude']},{'view_id':['1']}):
                with self.assertRaises(ValueError):history(c,settings,bad)

    def test_forecasts_and_key_level_provenance_are_projected_without_account_data(self):
        with site() as (store,settings,url):
            now=datetime.now(timezone.utc)
            value=content();value['decision_basis']['review_triggers']=[{'kind':'price','condition':'at_or_below','price_cny':840,
                                                                      'event':None,'at':None}]
            ctx={'jd_accumulation_quote':{'price':850,'source':'jd_latest_price_api','quote_at':now.isoformat(),'fetched_at':now.isoformat()},
                 'jd_daily_samples':{'days':[{'date_bj':'2026-11-02','sampled_low_cny':840,'private':'DO-NOT-PUBLISH'}]},
                 'trading_accounts':{'accounts':{'high_full':{'cash':123456.78}}}}
            save(store.conn,'codex',0,2,now.isoformat(),now.isoformat(),value,ctx,assessment={'bias_short':'bearish','confidence':'high','cash':'DO-NOT-PUBLISH'})
            store.conn.commit();d=latest(store.conn,settings)
            self.assertIn('2026-11-02',d['key_levels'][0]['basis'])
            self.assertNotIn('DO-NOT-PUBLISH',json.dumps(d))
            self.assertNotIn('123456.78',json.dumps(d))
            self.assertEqual(assessment_projection({'p_up_3d':.8,'p_down_3d':.8,'confidence':'DO-NOT-PUBLISH'}),None)


class ExpandedPublicBrowserTests(unittest.TestCase):
    def test_subpages_load_on_demand_preserve_cache_and_history_and_overview_are_public_only(self):
        from playwright.sync_api import sync_playwright, expect
        from monitor.config import BJ
        with site() as (store,settings,url),sync_playwright() as p:
            now=datetime.now(timezone.utc);q={'price':850,'source':'jd_latest_price_api','quote_at':now.isoformat(),'fetched_at':now.isoformat()}
            at=now.astimezone(BJ).strftime('%Y-%m-%d %H:%M:%S')
            store.set_state('jin10_flash',[{'time_bj':at,'text':'黄金重要消息测试','important':True,'secret':'DO-NOT-PUBLISH'},
                                          {'time_bj':at,'text':'黄金普通消息测试','important':False}])
            store.set_state('web_macro',{'quotes':{'美元指数':{'value':100},'美国10年期国债收益率':{'value':0}},
                'quotes_fetched_at':now.isoformat(),'calendar_fetched_at':now.isoformat(),'calendar':[
                    {'time_utc':(now+timedelta(minutes=15)).isoformat(),'title':'JOLTs Job Openings','country':'US','tier':1,'actual':'FUTURE-NOT-PUBLISH','released':True,'forecast':0,'previous':1},
                    {'time_utc':(now-timedelta(minutes=2)).isoformat(),'title':'ISM Manufacturing PMI','country':'US','tier':1,'actual':0,'released':True,'forecast':1,'previous':2}]})
            for i in range(2,15):
                when=now-timedelta(seconds=15-i)
                d=content();d['decision_basis']['review_triggers']=[{'kind':'price','condition':'at_or_below','price_cny':840,'event':None,'at':None}]
                save(store.conn,'codex',0,i,when.isoformat(),when.isoformat(),d,{'jd_accumulation_quote':q,
                    'jd_daily_samples':{'days':[{'date_bj':'2026-11-02','sampled_low_cny':840}]}},
                    assessment={'bias_short':'bearish','bias_long':'neutral','confidence':'high','p_up_3d':.2,'p_down_3d':.6})
            store.conn.commit()
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page(viewport={'width':390,'height':844});requests=[];errors=[]
                page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url+'/public/');page.locator('[data-public-view="market"][data-ready="true"]').wait_for()
                self.assertFalse(any('view=news' in u or 'view=calendar' in u or '/ai/history' in u for u in requests))
                page.get_by_role('link',name='快讯',exact=True).click();page.locator('[data-public-view="news"][data-ready="true"]').wait_for()
                self.assertEqual(page.locator('.public-news-list li').count(),1)
                page.get_by_role('button',name='全部',exact=True).click();expect(page.locator('.public-news-list li')).to_have_count(2)
                page.get_by_role('link',name='金价',exact=True).click();page.get_by_role('link',name='快讯',exact=True).click()
                expect(page.locator('.public-news-list li')).to_have_count(2)
                self.assertEqual(sum('view=news' in u for u in requests),1)
                page.get_by_role('link',name='日历',exact=True).click();page.locator('[data-public-view="calendar"][data-ready="true"]').wait_for()
                self.assertEqual(page.get_by_role('button',name='7天范围',exact=True).get_attribute('aria-pressed'),'true')
                page.get_by_role('button',name='今天',exact=True).click()
                self.assertEqual(page.get_by_role('button',name='今天',exact=True).get_attribute('aria-pressed'),'true')
                self.assertNotIn('FUTURE-NOT-PUBLISH',page.locator('body').inner_text())
                page.get_by_role('button',name='7天范围',exact=True).click();page.get_by_role('button',name='已公布',exact=True).click()
                self.assertIn('0',page.locator('.event-values strong').inner_text())
                page.get_by_role('link',name='市场指标',exact=True).click();page.get_by_role('heading',name='宏观环境',exact=True).wait_for()
                page.get_by_role('button',name='AI分析',exact=True).click();page.get_by_role('heading',name='关注价位',exact=True).wait_for()
                page.get_by_role('heading',name='本轮风险',exact=True).wait_for()
                self.assertIn('0.4%',page.locator('.update-policy').inner_text())
                self.assertNotIn('风险与下一次观察',page.locator('body').inner_text())
                self.assertIn('偏空',page.locator('.assessment').inner_text())
                self.assertNotIn('判断置信度',page.locator('body').inner_text())
                self.assertNotIn('Codex',page.locator('body').inner_text())
                page.locator('.level-source summary').first.click()
                self.assertIn('2026-11-02',page.locator('.key-levels').inner_text())
                page.screenshot(path='/tmp/public-expanded-analysis.png',full_page=True)
                page.get_by_role('link',name='历史观点',exact=True).click();page.locator('.history-card').first.wait_for()
                self.assertEqual(page.locator('.history-card').count(),10)
                self.assertFalse(any('report_id=' in u for u in requests))
                page.get_by_role('button',name='展开该轮分析',exact=True).first.click();page.get_by_role('heading',name='当时的市场依据',exact=True).wait_for()
                self.assertIn('仅用于回顾',page.locator('.history-detail .key-levels').inner_text())
                self.assertEqual(sum('report_id=' in u for u in requests),1)
                page.get_by_role('button',name='加载更早的观点',exact=True).click();page.wait_for_function('document.querySelectorAll(".history-card").length===14')
                page.get_by_role('button',name='仓位推荐',exact=True).click();page.get_by_text('11档仓位推荐总览',exact=True).click()
                self.assertIn('0.4%',page.locator('.update-policy').inner_text())
                self.assertIn('相对上次分析输入价',page.locator('.update-policy').inner_text())
                page.locator('.reference-rules summary').click()
                self.assertIn('不代表已启动新分析',page.locator('.update-policy').inner_text())
                self.assertEqual(page.locator('.position-overview tbody tr').count(),11)
                before=sum('/api/public/ai?' in u for u in requests)
                page.get_by_role('button',name='查看100%仓位推荐',exact=True).click()
                self.assertIn('100%',page.locator('.position-current').inner_text())
                self.assertEqual(sum('/api/public/ai?' in u for u in requests),before)
                page.get_by_role('button',name='AI分析',exact=True).click();page.locator('.history-card').first.wait_for()
                self.assertEqual(page.locator('.history-card').count(),14)
                for width in (360,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    for route,view in (('market/news','news'),('market/calendar','calendar'),('market/indicators','indicators'),('analysis','analysis'),('analysis/history','history'),('recommendations','recommendations')):
                        page.evaluate('(route)=>location.hash=route',route)
                        page.locator(f'[data-public-view="{view}"]').wait_for()
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,route))
                        self.assertNotIn('DO-NOT-PUBLISH',page.locator('body').inner_text())
                        self.assertNotIn('Codex',page.locator('body').inner_text())
                        self.assertNotIn('判断置信度',page.locator('body').inner_text())
                self.assertFalse(any('analyst=codex' in u for u in requests))
                for theme,bg,brand in (('light','rgb(246, 245, 242)','rgb(154, 107, 18)'),('dark','rgb(16, 16, 16)','rgb(224, 169, 74)')):
                    page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                    self.assertEqual(page.evaluate('getComputedStyle(document.documentElement).backgroundColor'),bg)
                    expect(page.locator('.bottom-nav button[aria-current=page]')).to_have_css('color',brand)
                page.screenshot(path='/tmp/gold-public-dark.png',full_page=True)
                page.evaluate('location.hash="analysis/history"');page.locator('.history-card').first.wait_for()
                set_enabled(store.conn,'codex',False)
                page.get_by_role('button',name='刷新当前页面',exact=True).click()
                page.wait_for_function('document.querySelectorAll(".history-card").length===0')
                page.get_by_role('link',name='最新观点',exact=True).click()
                page.get_by_role('heading',name='暂无公开观点',exact=True).wait_for()
                self.assertEqual(page.locator('.analysis-lead').count(),0)
                self.assertFalse(any('/api/' in u and '/api/public/' not in u for u in requests))
                self.assertEqual(errors,[])
            finally:browser.close()
