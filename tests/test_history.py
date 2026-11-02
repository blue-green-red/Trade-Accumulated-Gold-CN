"""History filtering must precede pagination and include all current account types."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from monitor.__main__ import Monitor
from monitor.ai import MarketView
from monitor.config import Settings
from monitor.store import utc_now
from monitor.web import history_page


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=replace(Settings(),db_path=Path(self.temp.name)/'history.db',ai_trading_mode='six_account',
                              anthropic_api_key='',claude_cli='missing',codex_cli='missing',clawx_api_key='')
        self.monitor=Monitor(self.settings,True)
        self.c=self.monitor.store.conn
        self.now=utc_now()

    def tearDown(self):
        self.c.close();self.temp.cleanup()

    def page(self,**values):
        return history_page(self.settings,{k:[str(v)] for k,v in values.items()})

    def test_pagination_and_server_filters_include_older_matching_rows(self):
        for i in range(120):
            account='codex:short_empty' if i%2 else 'claude:long_full'
            stamp=(self.now-timedelta(minutes=i)).isoformat()
            self.c.execute("INSERT INTO ai_plans(account,view_id,generation,created_at,expires_at,decision_json,status,reason) VALUES(?,?,0,?,?,?,'executed','')",
                           (account,i,stamp,stamp,json.dumps({'action':'hold'})))
        old=(self.now-timedelta(days=40)).isoformat()
        self.c.execute("INSERT INTO ai_plans(account,view_id,generation,created_at,expires_at,decision_json,status,reason) VALUES('codex:short_empty',999,0,?,?,?,'executed','')",(old,old,'{"action":"hold"}'))
        self.c.commit()
        first=self.page(kind='plans',model='codex',days=7)
        self.assertEqual(len(first['items']),50);self.assertTrue(first['has_more'])
        second=self.page(kind='plans',model='codex',days=7,offset=50)
        self.assertEqual(len(second['items']),10);self.assertFalse(second['has_more'])
        self.assertTrue(all(r['account']=='codex:short_empty' for r in first['items']+second['items']))
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        self.assertEqual(len(self.page(kind='plans',account='codex:short_empty',limit=100)['items']),61)
        self.assertEqual(self.page(kind='plans',account="x' OR 1=1 --")['items'],[])

    def test_fills_exclude_removed_rule_accounts(self):
        stamp=self.now.isoformat()
        self.c.execute("INSERT INTO account_fills(strategy,ts,side,grams,price,fee,reason) VALUES('hold',?,'buy',1,1000,0,'rule')",(stamp,))
        self.c.execute("INSERT INTO ai_portfolio_fills(account,ts,side,grams,price,fee,pnl,reason,quote_json) VALUES('codex:short_empty',?,'sell',1,1000,4,20,'AI','{}')",(stamp,))
        t=self.c.execute("INSERT INTO paper_trades(bank,strategy,status,opened_at,grams) VALUES('zheshang','short','open',?,1)",(stamp,))
        self.c.execute("INSERT INTO paper_fills(trade_id,ts,side,grams,price,fee,reason) VALUES(?,?,'buy',1,1000,0,'short rule')",(t.lastrowid,stamp))
        self.c.commit()
        rows=self.page(kind='fills')['items']
        self.assertEqual({r['account'] for r in rows},{'codex:short_empty'})
        self.assertEqual(self.page(kind='fills',model='rules')['items'],[])
        self.assertEqual(self.page(kind='fills',model='codex')['items'][0]['pnl'],20)

    def test_analysis_history_contains_forecast_outcomes_without_duplicate_history(self):
        view=MarketView(bias_short='neutral',bias_long='neutral',confidence='medium',p_up_3d=.3,p_down_3d=.3,
                        p_up_3m=.3,p_down_3m=.3,hard_risk_event=False,hard_risk_note='',drivers=[],upcoming_events=[],summary='history')
        self.c.execute("INSERT INTO ai_views(ts,xauusd,analyst,view_json,run_id) VALUES(?,4000,'codex',?,'history-run')",(self.now.isoformat(),view.model_dump_json()))
        self.c.commit()
        row=self.page(kind='analysis',model='codex')['items'][0]
        self.assertEqual(row['view']['summary'],'history')
        self.assertEqual(set(row['outcomes']),{'1d','3d','20d','60d'})

    def test_invalid_kind_rejected_and_size_bounded(self):
        with self.assertRaises(ValueError): self.page(kind='invalid')
        self.assertEqual(self.page(kind='plans',limit=9999)['limit'],100)


if __name__=='__main__':unittest.main()
