"""Advisory output schema; no executable orders or financial API operations."""
import json
from datetime import datetime,timedelta,timezone
from typing import Literal,Optional
from pydantic import BaseModel,Field
from .jd_live_proposals import SuggestedOrder,validate_proposals,proposal_signature
from .jd_live_semantics import immediate_signature,management_signature,notification_parts
from .jd_live_entry import LimitBuyReview
from .jd_live_path import PositionPath

class AdviceReassessment(BaseModel):
    change_summary:str=Field(min_length=1,max_length=120,description='相对上一轮的新增事实；无实质变化直说，不重复余额')
    thesis_status:Literal['new','valid','weakened','invalidated']
    thesis:str=Field(min_length=1,max_length=180,description='当前交易假设、支持证据与推翻条件')
    price_basis:str=Field(min_length=1,max_length=220,description='目标和复评价的来源及本轮沿用或调整依据；估计标为假设，无价位写不适用')

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

class PriceReviewCondition(BaseModel):
    condition:Literal['at_or_above','at_or_below']
    price_cny:float=Field(gt=0,allow_inf_nan=False)
    quote_side:Literal['sell','buy']='sell'
    order_key:Optional[str]=Field(default=None,min_length=24,max_length=24,pattern='^[0-9a-f]{24}$',description='只针对某挂单复评时填写；null为账户级条件')
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
    valid_for_minutes:int=Field(default=240,ge=15,le=1440)
    reason:str=Field(max_length=1600,description='简述本户持有、加仓、减仓的扣费比较与选择；不推荐条件单时说明原因，不重复reassessment')
    risk_note:str=Field(max_length=800)
    reassessment:Optional[AdviceReassessment]=None
    position_path:Optional[PositionPath]=Field(default=None,description='新分析填写建仓与退出的关联；无有效账户时null，复用下跌情景，不重复算式')
    downside_scenarios:list[DownsideScenario]=Field(default_factory=list,max_length=2,description='独立比较候选下跌价的加仓/持有/退出；无可支持价位填[]并在reason说明，禁止凑单')
    limit_buy_reviews:list[LimitBuyReview]=Field(default_factory=list,max_length=2,description='新版独立评估低位提前买入方案；无可靠候选也说明具体缺口，未知参数null，不强制买入')
    suggested_orders:list[SuggestedOrder]=Field(default_factory=list,max_length=2,description='本轮适合提前设置的条件单建议；互相独立、同时预留资金和克数。无合适机会填[]，不表示已挂单')
    order_management:list[OrderManagement]=Field(default_factory=list,max_length=5,description='对输入可见的已有挂单逐单给keep/cancel/replace建议，绝不直接执行')
    review_conditions:list[PriceReviewCondition]=Field(default_factory=list,max_length=4,description='使用本银行元/克报价触发AI复评；有效期沿用本条建议，不是下单条件')

class LiveAdviceSet(BaseModel):
    zheshang:Optional[LiveAdvice]=None
    minsheng:Optional[LiveAdvice]=None

PROMPT="""
真实账户规则（live_readonly_accounts）：
- 仅给启用真实账户人工建议，与模拟执行隔离；按risk_profile争取扣费净值增长，允许有依据地提前布局、逆势或分批参与，不要求确认底部、趋势反转或多项利好同时出现，不强制交易。峰值未知不判定回撤触线，成本浮亏不等于峰值回撤。
- reason按本户黄金/现金比例和共用比较规则选择规模与时机；独立比较再买一笔、维持现仓和等待，已有首笔或缺少新增利好不能单独否决追加；按当前价重算累计投入、均价及整仓风险，给追加/暂停/退出条件；补充减仓的扣费比较；情景标明假设，无法估计直说，不编造价位。reassessment结合previous_advice.strategy_review复核假设与候选变动；沿用削弱/失效假设时，price_basis给当前证据与目标可达依据；旧条件未触不能独立支持持有。
- position_path给目标仓位、0起的退出情景索引（无价格边界null）、退出影响整套建仓(plan)或当前这批(tranche)，以及继续/暂停追加的依据；自选损失边界不等于假设失效，须解释其范围。允许分批至满仓。保护仅覆盖确认现仓；买入或保护退出后刷新重评，旧追加不自动沿用，独立京东单不保证先后，边界内买单先复评及人工撤改确认。
- account_risk为本户黄金+现金的扣卖费估值及风险参考，预算约束净值损失，不直接等于金价折扣或退出幅度，也非必须用满。downside_scenarios比较候选下跌价的加仓、持有、退出：压力价不自动成为止损或买点；低仓位不自动买也不默认再卖，拒绝现价不替代较低价比较，无依据可留空。
- 废弃旧候选只在reassessment说明，不替代本轮新增评估；limit_buy_reviews仅写本轮低位方案，分别说明入场、目标、新增退出、规模及期限依据；百分比假设可用，但须说明适用性，不能仅按每轮现价固定折价滚动下移或把压力价换名作入场。候选保留、改动或废弃须结合新增事实，已触及旧候选却继续等待需解释；退出可为假设失效或自选损失限制，无需独立技术位确认。适合现价参与用buy，适合提前挂买单用recommend并给同参数买单；已知不值得用not_worthwhile，needs_review只用于影响决定的具体待核验前提，给截止及不同结果对应行动；未知参数null，不凑单、不重复评估表。
- suggested_orders评估无人盯盘的限价买≤、限价卖≥或组合止盈止损卖出，不凑单；组合止损须对应同价、倾向卖出的假设失效或自选风险限制情景，说明数量作用。已挂保护以本轮pending_orders为准，旧建议不等于已挂；无实际保护须说建议新挂。与已有单及本轮买卖合计占用，不借用待卖收入或别户资金，不重复挂单；休市不保证成交。
- 各户cash_cny/cash_source以接口优先，现金0不新增买入；即时买入预算未知可留空数量说明，条件买单须有确认预算；卖出不超可卖克数。使用本户有效快照与报价，过期给wait，不把历史样本冒充实时或完整历史。
- order_management逐笔处理已有单；replace只填修改项，null保留，部分成交默认用确认的剩余量，未知则明确核对；无单填[]，不编造标识。
- review_conditions是本银行报价触发复评，沿用建议有效期；suggested_orders是需本人挂到京东的交易条件，有独立期限，二者不混用。已有单不等于成交，参数关联仅为推断。价格均为元/克，不保证成交；无账户输入填null。"""


def prompt_for(context):
    return PROMPT if context.get('live_readonly_accounts') else ''



def save(conn,analyst,view_id,advice,inputs,now=None,invalidation_note=None):
    if not inputs:return
    now=now or datetime.now(timezone.utc)
    from .jd_live_readonly import enabled_banks
    enabled=set(enabled_banks(conn))
    for bank,raw in advice.items():
        if bank not in enabled:continue
        source=(inputs.get('accounts') or {}).get(bank)
        if raw is None or not source or not source.get('revision'):continue
        strict_downside='downside_scenarios' in raw
        strict_entry='limit_buy_reviews' in raw
        strict_path='position_path' in raw
        value=LiveAdvice.model_validate(raw).model_dump();warnings=[]
        if invalidation_note:warnings.append(invalidation_note)
        value['risk_profile']={**(inputs.get('risk_profile') or {}),'risk_budget_cny':(source.get('account_risk') or {}).get('risk_budget_cny')}
        value['input_account_risk']=source.get('account_risk')
        value['input_position_pct']=source.get('position_pct')
        value['input_quote_status']=(source.get('quote') or {}).get('status')
        value['input_quote_at']=(source.get('quote') or {}).get('quoted_at')
        from .jd_live_feedback import price
        value['input_quote_cny']=price((source.get('quote') or {}).get('sell_price_cny')) if value['input_quote_status']=='fresh' else None
        previous=next((v for v in source.get('previous_advice',[]) if v.get('analyst')==analyst),{})
        if previous.get('strategy_review') is not None:value['input_strategy_review']=previous['strategy_review']
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
        if value['action'] in ('buy','sell') and value['price_condition']!='none' and value['price_cny'] is None:warnings.append('价格条件缺少价格')
        from .jd_live_conditions import validate_structure
        warnings.extend(validate_structure(value,source,now))
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
        value['validation_version']=1
        conn.execute('INSERT OR IGNORE INTO jd_live_advice(bank,analyst,view_id,created_at,expires_at,snapshot_revision,advice_json) VALUES(?,?,?,?,?,?,?)',
            (bank,analyst,view_id,now.isoformat(),(now+timedelta(minutes=value['valid_for_minutes'])).isoformat(),source['revision'],json.dumps(value,ensure_ascii=False)))


def notification_events(conn,analyst,view_id):
    """Route advice through the existing dispatcher; wording never implies an executed bank trade."""
    if not view_id:return []
    from .jd_live_readonly import accounts
    events=[]
    for account in accounts(conn):
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
        validation_recovered=bool(raw_old and raw_old.get('checks') and not raw_old.get('validation_version') and raw_value.get('validation_version'))
        orders_changed=management_signature(old)!=management_signature(value)
        from .jd_live_entry import signature as entry_signature
        proposed_changed=proposal_signature(old)!=proposal_signature(value) or entry_signature(old)!=entry_signature(value)
        from .jd_live_conditions import stamp
        from .jd_live_orders import proposal_progress
        old_drafts=[p for p in (old or {}).get('suggested_orders',[]) if proposal_progress(p,previous[2],account['order_history'],old.get('valid_for_minutes',240))['status']=='not_observed']
        renewed=bool(value.get('suggested_orders')) and any(stamp(p.get('expires_at')) and stamp(p['expires_at'])<=datetime.now(timezone.utc) for p in old_drafts)
        proposal_notice=(proposed_changed or renewed) and bool(old_drafts or value.get('suggested_orders'))
        manual_order_action=any(o['action'] in ('cancel','replace') for v in (old,value) if v for o in v.get('order_management',[]))
        order_notice=manual_order_action and (orders_changed or (previous and previous[1]!=row[2]))
        if not notice_reasons and old and immediate_signature(old)==immediate_signature(value) and not orders_changed and not proposal_notice and not validation_recovered and previous[1]==row[2]:continue
        if not notice_reasons and not order_notice and not proposal_notice and value['action'] in ('hold','wait') and (not old or old.get('action') not in ('buy','sell')):continue
        key=f'live-advice:{row[0]}'
        if conn.execute('SELECT 1 FROM alerts WHERE key=? LIMIT 1',(key,)).fetchone():continue
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
