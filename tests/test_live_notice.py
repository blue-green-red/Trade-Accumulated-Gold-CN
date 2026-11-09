import copy
import unittest

from monitor.jd_live_notice import body,brief,time_text
from monitor.jd_live_semantics import notification_parts


class LiveNoticeFormatTests(unittest.TestCase):
    def setUp(self):
        self.account={'bank':'zheshang','fetched_at':'2026-11-09T05:10:00Z',
            'quote':{'quoted_at':'2026-11-09T05:12:00+00:00'},
            'pending_orders':{'status':'fresh','records':[]}}
        self.current={'id':1,'expires_at':'2026-11-09T06:13:00Z'}
        self.value={'action':'hold','grams':None,'price_condition':'reference','price_cny':850,
            'input_position_pct':20,'input_quote_cny':850,'checks':[],
            'reason':'E850/T900/R810，是假设而非保证。','risk_note':'压力测试价不自动成为止损。',
            'suggested_orders':[],'order_management':[],'review_conditions':[],
            'limit_buy_reviews':[],'downside_scenarios':[]}
    def render(self):return body(self.account,self.current,notification_parts(self.value))
    def draft(self,**changes):
        value={'kind':'limit_buy','grams':2,'price_cny':840,
               'expires_at':'2026-11-10T13:00:00Z','reason':'分批参与','checks':[]}
        value.update(changes);return value

    def test_candidate_is_not_presented_as_a_buy_instruction(self):
        self.value['limit_buy_reviews']=[{'decision':'needs_review','entry_price_cny':840,
            'target_price_cny':900,'risk_exit_cny':810,'grams':2,
            'expires_at':'2026-11-10T13:00:00Z','reason':'目标可能因下跌原因改变。','checks':[]}]
        self.value['review_conditions']=[{'condition':'at_or_below','price_cny':840,'quote_side':'buy'}]
        text=self.render()
        self.assertTrue(text.startswith('**本轮结论：继续持有'))
        self.assertIn('仅待复评，暂不建议提前挂单',text)
        self.assertIn('到价只触发重新分析，不执行交易',text)
        self.assertIn('银行报价 ≤ 840元/克',text)
        for misleading in ('入场≤','建议预挂','2克','新增退出参考810'):
            self.assertNotIn(misleading,text)
        self.assertIn('暂无新增买卖、挂单或撤改建议',text)

    def test_recommended_order_retains_complete_terms_and_separate_risk_reference(self):
        draft=self.draft();self.value['suggested_orders']=[draft]
        self.value['limit_buy_reviews']=[{'decision':'recommend','entry_price_cny':840,
            'target_price_cny':900,'risk_exit_cny':810,'grams':2,'expires_at':draft['expires_at'],'checks':[]}]
        text=self.render()
        for required in ('尚未提交京东','买入2克','≤ 840元/克','截止11-10 21:00',
                         '评估目标900元/克','新增退出参考810元/克','不是已挂止损'):
            self.assertIn(required,text)
        self.assertNotIn('暂无新增买卖',text)

    def test_limit_sell_direction_is_derived_from_draft_kind_without_extra_fields(self):
        self.value['suggested_orders']=[self.draft(kind='limit_sell',price_cny=880)]
        text=self.render()
        self.assertIn('卖出2克 · ≥ 880元/克',text)
        self.assertNotIn('参考价 880',text)

    def test_combination_replacement_keeps_both_prices_quantity_and_expiry(self):
        original={'kind':'take_profit_stop_loss','action':'sell','grams':5,'price_cny':880,
                  'loss_price_cny':820,'expires_at':'2026-11-10T13:00:00Z',
                  'status':'partially_filled','filled_grams':None}
        self.value['order_management']=[{'action':'replace','input_order':original,
            'effective_replacement':{**original,'expires_at':'2026-11-11T13:00:00Z'},'reason':'调整期限。'}]
        text=self.render()
        for required in ('原单卖出5克','剩余量待核对','止盈 ≥ 880','止损 ≤ 820',
                         '截止11-10 21:00','修改后卖出5克','截止11-11 21:00','不视为释放资金或克数'):
            self.assertIn(required,text)

    def test_cancel_and_keep_are_distinct_from_new_trade(self):
        self.value['order_management']=[{'action':'cancel','input_order':{**self.draft(),'action':'buy'},'reason':'目标失效。'},
                                        {'action':'keep','reason':'保留'}]
        text=self.render()
        self.assertIn('建议撤单',text);self.assertIn('建议保留1笔',text)
        self.assertIn('本轮不立即买卖',text);self.assertNotIn('暂无新增买卖',text)

    def test_partial_warning_never_leaks_mixed_reason_thesis_risk_or_invalid_draft(self):
        self.value.update(action='sell',grams=1,reason='UNSAFE_REASON',risk_note='UNSAFE_RISK',
            reassessment={'thesis':'UNSAFE_THESIS'},checks=['条件单建议：无效'],
            suggested_orders=[self.draft(price_cny=999,checks=['无效'])])
        text=self.render()
        self.assertIn('已通过独立校验',text);self.assertIn('卖出 1克',text)
        for unsafe in ('UNSAFE','999','建议预挂'):self.assertNotIn(unsafe,text)

    def test_timestamps_are_beijing_and_link_opens_private_advice(self):
        text=self.render()
        for required in ('有效至11-09 14:13','账户更新11-09 13:10','报价更新11-09 13:12',
                         '均为北京时间','https://gold.goalgoal.fun/#strategies/live/zheshang/advice'):
            self.assertIn(required,text)
        self.assertNotIn('+00:00',text);self.assertNotIn('2026-11-09T',text)
        self.assertEqual(time_text(None),'待确认')
        self.assertEqual(time_text('2026-11-09T13:00:00'),'待确认')

    def test_brief_expands_jargon_prefers_thesis_and_bounds_analysis_without_mutation(self):
        self.assertEqual(brief('E850/T900/R810；ETF不变。'),'入场价850/目标价900/风险参考价810；ETF不变。')
        self.value['reason']='不应重复的总体长理由。'*100
        self.value['reassessment']={'thesis':'本轮交易假设仍需观察。'*30}
        before=copy.deepcopy(self.value);text=self.render()
        self.assertNotIn('不应重复',text);self.assertLess(len(text),800)
        self.assertIn('详见账户页',text);self.assertEqual(before,self.value)

    def test_overlapping_reasons_and_withdrawal_stay_explicit(self):
        text=body(self.account,self.current,self.value,['daily_first','position_changed'],True)
        self.assertEqual(text.count('提醒原因：'),1)
        self.assertIn('当日首次真实账户分析',text);self.assertIn('仓位或确认资金变化',text)
        self.assertIn('已挂到京东的单以本轮挂单处理建议为准',text)
