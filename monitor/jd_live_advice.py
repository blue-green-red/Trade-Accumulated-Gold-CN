"""Advisory output schema; no executable orders or financial API operations."""
import json
from datetime import datetime,timedelta,timezone
from typing import Literal,Optional
from pydantic import BaseModel,Field
from .jd_live_proposals import SuggestedOrder,validate_proposals,proposal_signature
from .jd_live_semantics import immediate_signature,management_signature,notification_parts,minimum_buy_checks
from .jd_live_entry import LimitBuyReview
from .jd_live_path import PositionPath

class AdviceReassessment(BaseModel):
    change_summary:str=Field(min_length=1,max_length=120,description='相对上一轮的新增事实；无实质变化直说，不重复余额')
    thesis_status:Literal['new','valid','weakened','invalidated']
    thesis:str=Field(min_length=1,max_length=180,description='当前交易假设、支持证据与推翻条件')
    price_basis:str=Field(min_length=1,max_length=1200,description='约150字，写目标、退出、期限及敏感性，完整句收尾。单次触价只记触及。远端价升级须新增延续证据，否则仅作强反弹情景。继续检验较弱反弹。')

class ReplacementOrder(BaseModel):
    grams:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    price_condition:Optional[Literal['at_or_above','at_or_below']]=None
    price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    expires_at:Optional[str]=Field(default=None,max_length=40,description='含时区的ISO8601截止时间；null保留原单期限')

class OrderManagement(BaseModel):
    order_key:str=Field(min_length=24,max_length=24,pattern='^[0-9a-f]{24}$',description='pending_orders中的订单摘要标识')
    action:Literal['keep','cancel','replace']
    replacement:Optional[ReplacementOrder]=Field(default=None,description='replace时填写需修改的参数；null字段保留原值，方向沿用原单')
    reason:str=Field(max_length=500)

class BuyReviewZone(BaseModel):
    floor_cny:float=Field(gt=0,allow_inf_nan=False,description='回调或突破后回踩区间下缘，跌穿则放弃本路径')
    weak_target_cny:float=Field(gt=0,allow_inf_nan=False,description='本路径较弱反弹目标价')
    risk_exit_cny:float=Field(gt=0,allow_inf_nan=False,description='本路径新增仓的退出测算价')
    basis:str=Field(min_length=1,max_length=180,description='引用本行哪段近期报价作为区间与目标依据')

class BuyReview(BaseModel):
    grams:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False,description='具体待复评候选量；纯观察填null，到价再定量')
    recovery_price_cny:float=Field(gt=0,allow_inf_nan=False,description='本银行买价回收后须守住的价位')
    hold_minutes:int=Field(ge=1,le=60,description='用至少两笔有时间的有效本行买价核对，本轮有效期内可观察的分钟数')
    max_buy_price_cny:float=Field(gt=0,allow_inf_nan=False,description='复评时可接受的最高本行买价，按较弱反弹的扣费空间和失守风险选择，不固定折扣')
    zone:Optional[BuyReviewZone]=None

class PriceReviewCondition(BaseModel):
    condition:Literal['at_or_above','at_or_below']
    price_cny:float=Field(gt=0,allow_inf_nan=False)
    quote_side:Literal['sell','buy']='sell'
    order_key:Optional[str]=Field(default=None,min_length=24,max_length=24,pattern='^[0-9a-f]{24}$',description='只针对某挂单复评时填写；null为账户级条件')
    buy_review:Optional[BuyReview]=Field(default=None,description='达到价位后可考虑买入时填写，仅限账户级买价复评；卖出、风控或只观察填null')
    reason:str=Field(max_length=500)

class DownsideScenario(BaseModel):
    price_cny:float=Field(gt=0,allow_inf_nan=False,description='自主选择有依据的下跌候选价，元/克；仅为情景，不等于下单')
    price_role:Literal['stress_test','thesis_invalidation','risk_limit','entry_reference']
    basis:str=Field(min_length=1,max_length=160,description='来源及该价格为何属于此用途；失效说明哪个假设被推翻，风险限制说明自选损失及理由')
    buy_case:str=Field(min_length=1,max_length=120,description='下跌到此价加仓的价值或不合适原因，不套用现价拒买理由')
    hold_case:str=Field(min_length=1,max_length=120,description='维持现仓的收益与风险取舍')
    sell_case:str=Field(min_length=1,max_length=120,description='继续减仓/退出的必要性或不合适原因，考虑当前已低/高仓位')
    preferred_action:Literal['buy','hold','sell','reassess']

class LiveAdvice(BaseModel):
    action:Literal['buy','sell','hold','wait']
    grams:Optional[float]=Field(default=None,ge=0,allow_inf_nan=False)
    price_condition:Literal['at_or_above','at_or_below','reference','none']='none'
    price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    entry_trigger_key:Optional[str]=Field(default=None,min_length=1,max_length=64,description='加仓所依赖的复评 condition_key，独立判断填 null')
    valid_for_minutes:int=Field(default=240,ge=15,le=1440)
    reason:str=Field(max_length=1600,description='约300–400字。先比较同情景的每克扣费空间；定量时说明整仓影响。给出取舍依据，省去已列余额、委托参数和价位推导。')
    risk_note:str=Field(max_length=800,description='一句说明会改变本轮建议的风险')
    reassessment:Optional[AdviceReassessment]=None
    position_path:Optional[PositionPath]=Field(default=None,description='新分析填写建仓与退出的关联；无有效账户时null，复用下跌情景，不重复算式')
    downside_scenarios:list[DownsideScenario]=Field(default_factory=list,max_length=2,description='独立比较候选下跌价的加仓/持有/退出；无可支持价位填[]并在reason说明，禁止凑单')
    limit_buy_reviews:list[LimitBuyReview]=Field(default_factory=list,max_length=2,description='新版独立评估低位提前买入方案；无可靠候选也说明具体缺口，未知参数null，不强制买入')
    suggested_orders:list[SuggestedOrder]=Field(default_factory=list,max_length=2,description='本轮适合提前设置的条件单建议；互相独立、同时预留资金和克数。无合适机会填[]，不表示已挂单')
    order_management:list[OrderManagement]=Field(default_factory=list,max_length=5,description='对输入可见的已有挂单逐单给keep/cancel/replace建议，绝不直接执行')
    review_conditions:list[PriceReviewCondition]=Field(default_factory=list,max_length=4,description='用本银行报价触发AI复评；区间买入观察由程序另计时，其他条件沿用建议期限')

class LiveAdviceSet(BaseModel):
    zheshang:Optional[LiveAdvice]=None
    minsheng:Optional[LiveAdvice]=None

PROMPT="""
真实账户规则（live_readonly_accounts）：
- 只为启用且有输入的账户给人工建议，不执行交易，无输入填null。按risk_profile争取扣费净值增长，允许提前布局、逆势和分批，不要求确认底部、趋势反转或多项利好同时出现，不强制交易。峰值未知不判定回撤触线，成本浮亏单列。
- minimum_buy_grams只约束建议下限。按本轮机会和整仓风险定量。追加用新报价、仓位和独立依据。参考previous_advice.strategy_review，用完整中文说明。
- account_risk约束整户扣费净值损失，不直接换算金价折扣或退出幅度。低仓位不自动买卖。拒绝现价仍独立比较低位加仓、持有、退出。压力价不直接作买点或止损，无依据可留空。
- limit_buy_reviews只评本轮低位候选。旧候选沿用、改动及触价后仍等待须说明新增依据，废弃只在reassessment说明。百分比情景说明适用性，勿随现价固定折价下移。退出可按假设失效或自选损失，不强求独立技术位。现价适合用buy，提前挂买用recommend并给同参买单，不值得用not_worthwhile。有具体待核验前提才用needs_review，未知填null。
- 现金以本户cash_cny/cash_source为准，现金为0不新增买入。即时买入资金未知可留空数量并说明。买单须有确认资金，卖出不超可卖克数。与现有委托及本轮买卖合计占用，不借待卖收入或别户资金，不重复挂单。过期数据给wait，历史样本只作参考。
- 组合止损对应同价且倾向卖出的退出情景，说明数量。保护只覆盖确认现仓，以pending_orders为准，未挂则写建议新挂。成交后刷新重评，边界内买单先人工核对撤改，独立京东单不保证先后，休市不保证成交。按原单处理，部分成交用确认的剩余量，未知先核对。
- 用recent_bank_quotes的本行高低与波动定回调区及突破价。分别评估浅回调、突破后回踩、持续上涨：守区回升、守突破位、现价扣费空间。失守重估。可行填buy_review和zone，未采用的路径写原因。回调price_cny为入区上缘，突破价可高于回踩上限。"""


def prompt_for(context):
    return PROMPT if context.get('live_readonly_accounts') else ''



def save(conn,analyst,view_id,advice,inputs,now=None,invalidation_note=None,entry_warnings=None,entry_routes=None,
         submitted_price_events=()):
    if not inputs:return
    now=now or datetime.now(timezone.utc)
    from .jd_live_readonly import enabled_banks,buy_minimums
    enabled=set(enabled_banks(conn))
    minimums=buy_minimums(conn)
    for bank,raw in advice.items():
        if bank not in enabled:continue
        source=(inputs.get('accounts') or {}).get(bank)
        if raw is None or not source or not source.get('revision'):continue
        strict_downside='downside_scenarios' in raw
        strict_entry='limit_buy_reviews' in raw
        strict_path='position_path' in raw
        value=LiveAdvice.model_validate(raw).model_dump();warnings=[]
        for condition in value['review_conditions']:
            review=condition.get('buy_review')
            if review and review.get('zone') is None:
                review.pop('zone',None)
        if inputs.get('text_contract')=='complete_sentences_v1':
            from .jd_live_text import incomplete_fields
            fields=incomplete_fields(value)
            if fields:warnings.append('分析文字未完整结束，需复核：'+'、'.join(fields))
        if invalidation_note:warnings.append(invalidation_note)
        value['risk_profile']={**(inputs.get('risk_profile') or {}),'risk_budget_cny':(source.get('account_risk') or {}).get('risk_budget_cny')}
        value['input_account_risk']=source.get('account_risk')
        value['input_position_pct']=source.get('position_pct')
        value['input_quote_status']=(source.get('quote') or {}).get('status')
        value['input_sell_fee_rate']=source.get('sell_fee_rate')
        value['input_minimum_buy_grams']=minimums.get(bank)
        value['input_quote_at']=(source.get('quote') or {}).get('quoted_at')
        from .jd_live_feedback import price
        value['input_quote_cny']=price((source.get('quote') or {}).get('sell_price_cny')) if value['input_quote_status']=='fresh' else None
        value['input_buy_quote_cny']=price((source.get('quote') or {}).get('buy_price_cny')) if value['input_quote_status']=='fresh' else None
        previous=next((v for v in source.get('previous_advice',[]) if v.get('analyst')==analyst),{})
        if previous.get('strategy_review') is not None:value['input_strategy_review']=previous['strategy_review']
        from .participation_review import bank_cash
        value['input_participation_cash_cny']=bank_cash(source)
        input_orders=source.get('pending_orders') or {}
        value['input_data_status']={'account':source.get('status'),'orders':input_orders.get('status'),
            'orders_complete':input_orders.get('source_complete',input_orders.get('complete')),
            'orders_at':(source.get('component_times') or {}).get('pending_orders')}
        value['schema_version']=6 if strict_path else 5 if strict_entry else 4 if strict_downside else 3 if value.get('reassessment') is not None or value.get('suggested_orders') else 2
        value['input_state']={k:source.get(k) for k in ('total_grams','cash_cny','pending_order_count','recent_trade')}
        if source.get('status')!='fresh':warnings.append('输入账户数据不可用或过期')
        if value['action']=='sell' and (value['grams'] is None or value['grams']<=0 or value['grams']>(source.get('sellable_grams') or 0)+1e-8):warnings.append('卖出数量未填写或超过可卖克数')
        if value['action']=='buy':
            if source.get('cash_cny') is None:warnings.append('可用资金未知，执行前需确认预算')
            elif source['cash_cny']<=0:warnings.append('当前确认现金为0，买入需先明确资金来源或卖出后再评估')
            elif value['grams'] and value['price_cny'] and value['grams']*value['price_cny']>source['cash_cny']+1e-8:warnings.append('建议数量按参考价估算超过本账户可用资金')
            elif ((free_cash:=value.get('input_participation_cash_cny')) is not None and value['grams']):
                entry = value['price_cny'] or 0
                if value['price_condition']!='at_or_below' and value['input_quote_status']=='fresh':
                    entry=max(entry,price((source.get('quote') or {}).get('buy_price_cny')) or 0)
                if entry and value['grams']*entry>free_cash+1e-8:
                    warnings.append('新增买入与待成交买单的预算合计超过本户可用资金')
        if value['action'] in ('buy','sell') and value['price_condition']!='none' and value['price_cny'] is None:warnings.append('价格条件缺少价格')
        from .jd_live_conditions import validate_structure
        warnings.extend(validate_structure(value,source,now,minimum_buy_grams=minimums.get(bank)))
        warnings.extend(minimum_buy_checks(value,minimums.get(bank)))
        warnings.extend(validate_proposals(value,source,now))
        from .jd_live_risk import validate_downside,annotate,protection_state
        warnings.extend(validate_downside(value,source,require=strict_downside))
        from .jd_live_entry import validate as validate_entry
        warnings.extend(validate_entry(value,source,now,require=strict_entry))
        from .jd_live_path import validate as validate_path
        warnings.extend(validate_path(value,source,now,require=strict_path))
        annotate(value,source)
        value['input_protection']=protection_state(source)
        value['checks']=warnings
        warning = (entry_warnings or {}).get(bank)
        routes=(entry_routes or {}).get(bank,[])
        basis=value.get('entry_trigger_key')
        if basis and not any(r['condition_key']==basis for r in routes):
            from .jd_live_buy_guard import explicit_price_basis
            if touch:=explicit_price_basis(conn,analyst,bank,source,submitted_price_events,basis,now):
                routes=[*routes,touch]
                warning=None
        if routes or basis:
            from .jd_live_buy_guard import publication_entry_basis
            selected, basis_warning = publication_entry_basis(conn, bank, value, routes, now)
            warning = warning or basis_warning
            if not warning:
                value['entry_trigger_routes'] = selected
        if warning:
            from .jd_live_buy_guard import block_entries
            block_entries(value,warning,include_reviews=bank in (entry_warnings or {}))
        from .jd_live_buy_watch import set_watch
        set_watch(conn,bank,analyst,value,source,now,minimums.get(bank))
        value['validation_version']=1
        conn.execute('INSERT OR IGNORE INTO jd_live_advice(bank,analyst,view_id,created_at,expires_at,snapshot_revision,advice_json) VALUES(?,?,?,?,?,?,?)',
            (bank,analyst,view_id,now.isoformat(),(now+timedelta(minutes=value['valid_for_minutes'])).isoformat(),source['revision'],json.dumps(value,ensure_ascii=False)))


def notification_events(conn,analyst,view_id,*,now=None,selected_bank=None,include_recorded=False):
    """Route advice through the existing dispatcher; wording never implies an executed bank trade."""
    if not view_id:return []
    from .jd_live_readonly import accounts
    events=[]
    for account in accounts(conn,now,selected_bank=selected_bank):
        row=conn.execute('SELECT id,advice_json,snapshot_revision,expires_at FROM jd_live_advice WHERE bank=? AND analyst=? AND view_id=?',
                         (account['bank'],analyst,view_id)).fetchone()
        if not row:continue
        current=next((a for a in account['advice'] if a['id']==row[0]),None)
        if not current or current.get('action_status',current['status'])!='current':continue
        raw_value=json.loads(row[1]);value=notification_parts(current)
        if value is None:continue
        if value.get('notification_partial') and value['action'] in ('hold','wait') and not value['suggested_orders'] and not any(o['action'] in ('cancel','replace') for o in value['order_management']):continue
        from .jd_live_notice_rules import reasons
        notice_reasons=reasons(conn,analyst,account['bank'],row[0],current['created_at'],raw_value)
        previous=conn.execute('SELECT advice_json,snapshot_revision,created_at FROM jd_live_advice WHERE bank=? AND analyst=? AND id<? ORDER BY id DESC LIMIT 1',
                              (account['bank'],analyst,row[0])).fetchone()
        raw_old=json.loads(previous[0]) if previous else None
        old=notification_parts(raw_old) if raw_old else None
        from .jd_live_semantics import plan_terms,same_plan
        last_notice=conn.execute("SELECT a.advice_json,a.snapshot_revision FROM jd_live_advice a JOIN alerts n ON n.key='live-advice:'||a.id WHERE a.bank=? AND a.analyst=? AND a.id<? ORDER BY a.id DESC LIMIT 1",(account['bank'],analyst,row[0])).fetchone()
        anchor=notification_parts(json.loads(last_notice[0])) if last_notice else old
        anchor_revision=last_notice[1] if last_notice else previous[1] if previous else None
        validation_recovered=bool(raw_old and raw_old.get('checks') and not raw_old.get('validation_version') and raw_value.get('validation_version'))
        orders_changed=management_signature(old)!=management_signature(value)
        from .jd_live_entry import signature as entry_signature
        proposed_changed=proposal_signature(old)!=proposal_signature(value) or entry_signature(old)!=entry_signature(value)
        from .jd_live_conditions import stamp
        from .jd_live_orders import proposal_progress
        old_drafts=[p for p in (old or {}).get('suggested_orders',[]) if proposal_progress(p,previous[2],account['order_history'],old.get('valid_for_minutes',240))['status']=='not_observed']
        current_time=now or datetime.now(timezone.utc)
        renewed=bool(value.get('suggested_orders')) and any(stamp(p.get('expires_at')) and stamp(p['expires_at'])<=current_time for p in old_drafts)
        proposal_notice=(proposed_changed or renewed) and bool(old_drafts or value.get('suggested_orders'))
        manual_order_action=any(o['action'] in ('cancel','replace') for v in (old,value) if v for o in v.get('order_management',[]))
        order_notice=manual_order_action and (orders_changed or (previous and previous[1]!=row[2]))
        if not notice_reasons and old and same_plan(plan_terms(anchor),plan_terms(value)) and not orders_changed and not proposal_notice and not validation_recovered and anchor_revision==row[2]:continue
        if not notice_reasons and not order_notice and not proposal_notice and value['action'] in ('hold','wait') and (not old or old.get('action') not in ('buy','sell')):continue
        key=f'live-advice:{row[0]}'
        if not include_recorded and conn.execute('SELECT 1 FROM alerts WHERE key=? LIMIT 1',(key,)).fetchone():continue
        action={'buy':'买入建议','sell':'卖出建议','hold':'持有','wait':'观望'}[value['action']]
        if order_notice:
            actionable=[o for o in value.get('order_management',[]) if o['action'] in ('cancel','replace')]
            action=({'cancel':'撤单建议','replace':'改单建议'}[actionable[0]['action']] if len(actionable)==1 else '挂单处理建议' if actionable else '挂单处理建议更新')
        elif proposal_notice:
            action='条件单建议' if value.get('suggested_orders') else '条件单方案更新'
        elif notice_reasons and value['action'] in ('hold','wait'):
            action='仓位变化复评' if 'position_changed' in notice_reasons else '当日首次评估'
        if value['action'] in ('buy','sell') and (order_notice or proposal_notice):
            action={'buy':'买入建议','sell':'卖出建议'}[value['action']]+'＋'+action
        if value['action'] in ('hold','wait') and action not in ('持有','观望'):
            action={'hold':'持有','wait':'观望'}[value['action']]+'·'+action
        from .jd_live_notice import body as notification_body
        body=notification_body(account,current,value,notice_reasons,proposal_notice and not value.get('suggested_orders'))
        events.append({'key':key,'live_advice_id':row[0],'notification_reasons':notice_reasons,'notification_topic':f"{analyst}:live_{account['bank']}",
                       'title':f"💡{analyst.title()}账户建议｜{account['name'].removesuffix('积存金')}｜{action}",'body':body})
    return events
