"""Advisory output schema; no executable orders or financial API operations."""
import json
from datetime import datetime,timedelta,timezone
from typing import Literal,Optional
from pydantic import BaseModel,Field
from .jd_live_proposals import SuggestedOrder,validate_proposals,proposal_signature,format_proposals
from .jd_live_semantics import immediate_signature,management_signature,notification_parts

class AdviceReassessment(BaseModel):
    change_summary:str=Field(min_length=1,max_length=120,description='相对上一轮的新增事实；无实质变化直说，不重复余额')
    thesis_status:Literal['new','valid','weakened','invalidated']
    thesis:str=Field(min_length=1,max_length=180,description='当前持有或等待假设、支持证据与推翻条件')
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

class LiveAdvice(BaseModel):
    action:Literal['buy','sell','hold','wait']
    grams:Optional[float]=Field(default=None,ge=0,allow_inf_nan=False)
    price_condition:Literal['at_or_above','at_or_below','reference','none']='none'
    price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    valid_for_minutes:int=Field(default=240,ge=15,le=1440)
    reason:str=Field(max_length=1600,description='简述本户持有、加仓、减仓的扣费比较与选择；不推荐条件单时说明原因，不重复reassessment')
    risk_note:str=Field(max_length=800)
    reassessment:Optional[AdviceReassessment]=None
    suggested_orders:list[SuggestedOrder]=Field(default_factory=list,max_length=2,description='本轮适合提前设置的条件单建议；互相独立、同时预留资金和克数。无合适机会填[]，不表示已挂单')
    order_management:list[OrderManagement]=Field(default_factory=list,max_length=5,description='对输入可见的已有挂单逐单给keep/cancel/replace建议，绝不直接执行')
    review_conditions:list[PriceReviewCondition]=Field(default_factory=list,max_length=4,description='使用本银行元/克报价触发AI复评；有效期沿用本条建议，不是下单条件')

class LiveAdviceSet(BaseModel):
    zheshang:Optional[LiveAdvice]=None
    minsheng:Optional[LiveAdvice]=None

PROMPT="""
真实账户规则（live_readonly_accounts）：
- 仅给输入中启用的真实账户人工建议，与模拟执行隔离；按risk_profile判断，不强制交易。峰值未知不判定回撤触线，成本浮亏不等于峰值回撤。
- reassessment结合previous_advice.strategy_review复核假设和旧价位；假设削弱/失效仍沿用时，price_basis给当前证据与目标可达依据，reason重算等待、现价部分减仓及退出的扣费收益/风险与仓位取舍；旧条件未触不能独立支持持有，情景目标不是涨幅上限。
- 每轮评估适合无人盯盘时预挂的suggested_orders：限价买≤、限价卖≥或组合止盈止损卖出，不凑单；具体克数、价格、截止时间和依据见schema。与现有挂单及本轮买卖合计占用，不借用待卖出收入或别户资金，不重复推荐已有单；休市不保证触发成交。
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
        value=LiveAdvice.model_validate(raw).model_dump();warnings=[]
        if invalidation_note:warnings.append(invalidation_note)
        value['risk_profile']=inputs.get('risk_profile')
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
        value['schema_version']=3 if value.get('reassessment') is not None or value.get('suggested_orders') else 2
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
        value['checks']=warnings
        value['validation_version']=1
        conn.execute('INSERT OR IGNORE INTO jd_live_advice(bank,analyst,view_id,created_at,expires_at,snapshot_revision,advice_json) VALUES(?,?,?,?,?,?,?)',
            (bank,analyst,view_id,now.isoformat(),(now+timedelta(minutes=value['valid_for_minutes'])).isoformat(),source['revision'],json.dumps(value,ensure_ascii=False)))


def notification_events(conn,analyst,view_id):
    """Route advice through the existing dispatcher; wording never implies an executed bank trade."""
    if not view_id:return []
    from .jd_live_readonly import accounts
    from zoneinfo import ZoneInfo
    events=[]
    for account in accounts(conn):
        row=conn.execute('SELECT id,advice_json,snapshot_revision,expires_at FROM jd_live_advice WHERE bank=? AND analyst=? AND view_id=?',
                         (account['bank'],analyst,view_id)).fetchone()
        if not row:continue
        current=next((a for a in account['advice'] if a['id']==row[0]),None)
        if not current or current.get('action_status',current['status'])!='current':continue
        raw_value=json.loads(row[1]);value=notification_parts(raw_value)
        if value is None:continue
        if value.get('notification_partial') and value['action'] in ('hold','wait') and not value['suggested_orders'] and not any(o['action'] in ('cancel','replace') for o in value['order_management']):continue
        previous=conn.execute('SELECT advice_json,snapshot_revision,created_at FROM jd_live_advice WHERE bank=? AND analyst=? AND id<? ORDER BY id DESC LIMIT 1',
                              (account['bank'],analyst,row[0])).fetchone()
        raw_old=json.loads(previous[0]) if previous else None
        old=notification_parts(raw_old) if raw_old else None
        validation_recovered=bool(raw_old and raw_old.get('checks') and not raw_old.get('validation_version') and raw_value.get('validation_version'))
        orders_changed=management_signature(old)!=management_signature(value)
        proposed_changed=proposal_signature(old)!=proposal_signature(value)
        from .jd_live_conditions import stamp
        from .jd_live_orders import proposal_progress
        old_drafts=[p for p in (old or {}).get('suggested_orders',[]) if proposal_progress(p,previous[2],account['order_history'],old.get('valid_for_minutes',240))['status']=='not_observed']
        renewed=bool(value.get('suggested_orders')) and any(stamp(p.get('expires_at')) and stamp(p['expires_at'])<=datetime.now(timezone.utc) for p in old_drafts)
        proposal_notice=(proposed_changed or renewed) and bool(old_drafts or value.get('suggested_orders'))
        manual_order_action=any(o['action'] in ('cancel','replace') for v in (old,value) if v for o in v.get('order_management',[]))
        order_notice=manual_order_action and (orders_changed or (previous and previous[1]!=row[2]))
        if old and immediate_signature(old)==immediate_signature(value) and not orders_changed and not proposal_notice and not validation_recovered and previous[1]==row[2]:continue
        if not order_notice and not proposal_notice and value['action'] in ('hold','wait') and (not old or old.get('action') not in ('buy','sell')):continue
        key=f'live-advice:{row[0]}'
        if conn.execute('SELECT 1 FROM alerts WHERE key=? LIMIT 1',(key,)).fetchone():continue
        action={'buy':'买入建议','sell':'卖出建议','hold':'调整为持有','wait':'调整为观望'}[value['action']]
        if order_notice:
            actionable=[o for o in value.get('order_management',[]) if o['action'] in ('cancel','replace')]
            action=({'cancel':'撤单建议','replace':'改单建议'}[actionable[0]['action']] if len(actionable)==1 else '挂单处理建议' if actionable else '挂单处理建议更新')
        elif proposal_notice:
            action='条件单建议' if value.get('suggested_orders') else '条件单方案更新'
        if value['action'] in ('buy','sell') and (order_notice or proposal_notice):
            action={'buy':'买入建议','sell':'卖出建议'}[value['action']]+'＋'+action
        condition={'at_or_above':'价格≥','at_or_below':'价格≤','reference':'参考价','none':'未设价格条件'}[value['price_condition']]
        quantity=('本轮不立即买卖' if value['action'] in ('hold','wait') else '数量待确认') if value['grams'] is None else f"{value['grams']:g}克"
        if value['action'] in ('buy','sell'):quantity='新增买卖建议：'+{'buy':'买入','sell':'卖出'}[value['action']]+' '+quantity
        price='' if value['price_cny'] is None else f"{value['price_cny']:g}元/克"
        expiry=datetime.fromisoformat(row[3]).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        orders=account.get('pending_orders') or {}
        order_note=f"已读取{len(orders.get('records',[]))}笔待成交明细" if orders.get('status')=='fresh' else '条件单明细待更新'
        tracking=(current.get('execution_progress') or {}).get('status')
        progress_note={'pending':'发现对应或相近挂单，仍待成交','partially_filled':'对应或相近条件单已有部分成交','filled':'对应或相近条件单已成交'}.get(tracking)
        structure=format_proposals(value)+format_structure(value)
        if proposal_notice and not value.get('suggested_orders'):
            structure='本轮不再建议新挂上一轮条件单；已挂到京东的单以本轮挂单处理建议为准。\n\n'+structure
        reason='仅推送已通过独立校验的部分；其余挂单或复评建议需核对，请查看前端明细。' if value.get('notification_partial') else value['reason']
        risk='局部参数尚未通过校验，未包含在本次提醒；交易前请核对。' if value.get('notification_partial') else value['risk_note']
        body=(f"**真实账户建议，需本人手动确认；程序未自动下单。**\n\n{account['name']} · {action}\n\n"
              f"{quantity} · {condition}{price}\n\n有效至北京时间{expiry} · 建议 #{row[0]}\n\n"
              f"{reason}\n\n{structure}风险：{risk}\n\n委托查询：{order_note}{'；'+progress_note if progress_note else ''}\n\n账户快照：{account['fetched_at']}\n\n银行报价时间：{(account.get('quote') or {}).get('quoted_at') or '未知'}\n\n详情见看板账户页。")
        events.append({'key':key,'live_advice_id':row[0],'notification_topic':f"{analyst}:live_{account['bank']}",
                       'title':f"💡真实账户 · {analyst.title()} · {account['name']} · {action}",'body':body})
    return events


def format_structure(value):
    lines=[]
    symbol={'at_or_above':'≥','at_or_below':'≤'}
    def terms(order):
        grams=order.get('grams');price=order.get('price_cny')
        if grams is None or price is None:return '参数待核对'
        side={'sell':'卖出','buy':'买入'}.get(order.get('action'),'')
        filled=order.get('filled_grams')
        prices=f" · {symbol.get(order.get('price_condition'),'参考')} {price:g}元/克"
        if order.get('kind')=='take_profit_stop_loss':
            loss=order.get('loss_price_cny')
            prices=f" · 止盈 ≥ {price:g}元/克 / 止损 ≤ "+(f'{loss:g}元/克' if loss is not None else '待核对')
        return f"{side}{grams:g}克"+(f"（已成交{filled:g}克）" if filled else '')+prices
    for item in value.get('order_management',[]):
        original=item.get('input_order') or {}
        action={'keep':'保留原单','cancel':'建议撤单（需本人操作）','replace':'建议改单（需本人操作）'}[item['action']]
        line=f"挂单处理：{action} · 原单{terms(original)}"
        if item['action']=='replace':
            replacement=item.get('effective_replacement') or {}
            from .jd_live_conditions import stamp
            from zoneinfo import ZoneInfo
            expiry=stamp(replacement.get('expires_at'))
            deadline=expiry.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M 北京时间') if expiry else '待核对'
            line+=f" → 修改后{terms(replacement)} · 截止{deadline}"
        lines.append(line+'\n'+item['reason'])
    for condition in value.get('review_conditions',[]):
        lines.append(f"价格复评：本银行{'卖出' if condition['quote_side']=='sell' else '买入'}报价 {symbol[condition['condition']]} {condition['price_cny']:g}元/克时重新分析（不执行交易）\n{condition['reason']}")
    return '\n\n'.join(lines)+'\n\n' if lines else ''
