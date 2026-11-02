"""Advisory output schema; no executable orders or financial API operations."""
import json
from datetime import datetime,timedelta,timezone
from typing import Literal,Optional
from pydantic import BaseModel,Field

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
    reason:str=Field(max_length=1600)
    risk_note:str=Field(max_length=800)
    order_management:list[OrderManagement]=Field(default_factory=list,max_length=5,description='对输入可见的已有挂单逐单给keep/cancel/replace建议，绝不直接执行')
    review_conditions:list[PriceReviewCondition]=Field(default_factory=list,max_length=4,description='使用本银行元/克报价触发AI复评；有效期沿用本条建议，不是下单条件')

class LiveAdviceSet(BaseModel):
    zheshang:Optional[LiveAdvice]=None
    minsheng:Optional[LiveAdvice]=None

PROMPT="""
真实账户规则（live_readonly_accounts）：
- 在live_account_advice分别给浙商、民生建议，由用户确认执行；与六个模拟户隔离，不提交真实交易。
- 依risk_profile的风险偏好积极评估持有、加减仓与分批试错，不强制交易；预算参考不代表保证收益。历史峰值未知时不判定回撤触线，成本浮亏不等于峰值回撤。
- 各户仅用自己的cash_cny/cash_source：接口优先，用户满仓现金0仅为备用声明。不得借用别户或模拟资金；现金0不加仓，买入预算未知可将grams留空并说明；卖出不超过可卖克数。
- 使用各户快照、成本、银行报价及来源时间；账户数据缺失或过期给wait和原因。历史报价、持仓/清仓首页样本不冒充实时价或完整历史，未知项不补造。
- 账户action与已有挂单处理分别表达：order_management对每个可见的待成交单按order_key给keep/cancel/replace及理由。replace仅填需修改参数，其余null保留，部分成交单克数默认保留已确认未成交量，未知则需明确；先核对冻结数量/资金，避免重复占用。没有挂单填[]，明细未知不编造标识。
- review_conditions把理由中“到某价重新评估”落实为本银行元/克阈值、at_or_above/at_or_below、buy/sell报价侧及理由，可关联order_key；最多4条，有效期沿用本条建议。无明确条件填[]，不把复评阈值当自动买卖/撤改指令。已有挂单不等于成交，相近参数关联仅为推断。
- 价格单位元/克，价格条件不保证成交；每户给reason、risk_note及valid_for_minutes，无账户输入填null。"""


def prompt_for(context):
    return PROMPT if context.get('live_readonly_accounts') else ''



def save(conn,analyst,view_id,advice,inputs,now=None,invalidation_note=None):
    if not inputs:return
    now=now or datetime.now(timezone.utc)
    for bank,raw in advice.items():
        source=(inputs.get('accounts') or {}).get(bank)
        if raw is None or not source or not source.get('revision'):continue
        value=LiveAdvice.model_validate(raw).model_dump();warnings=[]
        if invalidation_note:warnings.append(invalidation_note)
        value['risk_profile']=inputs.get('risk_profile')
        value['input_quote_status']=(source.get('quote') or {}).get('status')
        value['input_quote_at']=(source.get('quote') or {}).get('quoted_at')
        value['schema_version']=2
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
        value['checks']=warnings
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
        if not current or current['status']!='current':continue
        value=json.loads(row[1])
        previous=conn.execute('SELECT advice_json,snapshot_revision FROM jd_live_advice WHERE bank=? AND analyst=? AND id<? ORDER BY id DESC LIMIT 1',
                              (account['bank'],analyst,row[0])).fetchone()
        old=json.loads(previous[0]) if previous else None
        fields=('action','grams','price_condition','price_cny')
        def management_signature(v):
            return sorted((o['order_key'],o['action'],json.dumps(o.get('replacement'),sort_keys=True)) for o in (v or {}).get('order_management',[]))
        orders_changed=management_signature(old)!=management_signature(value)
        manual_order_action=any(o['action'] in ('cancel','replace') for v in (old,value) if v for o in v.get('order_management',[]))
        order_notice=manual_order_action and (orders_changed or (previous and previous[1]!=row[2]))
        if old and all(old.get(k)==value.get(k) for k in fields) and not orders_changed and previous[1]==row[2]:continue
        if not order_notice and value['action'] in ('hold','wait') and (not old or old.get('action') not in ('buy','sell')):continue
        key=f'live-advice:{row[0]}'
        if conn.execute('SELECT 1 FROM alerts WHERE key=? LIMIT 1',(key,)).fetchone():continue
        action={'buy':'买入建议','sell':'卖出建议','hold':'调整为持有','wait':'调整为观望'}[value['action']]
        if order_notice:
            actionable=[o for o in value.get('order_management',[]) if o['action'] in ('cancel','replace')]
            action=({'cancel':'撤单建议','replace':'改单建议'}[actionable[0]['action']] if len(actionable)==1 else '挂单处理建议' if actionable else '挂单处理建议更新')
        condition={'at_or_above':'价格≥','at_or_below':'价格≤','reference':'参考价','none':'未设价格条件'}[value['price_condition']]
        quantity=('不新增买卖数量' if value['action'] in ('hold','wait') else '数量待确认') if value['grams'] is None else f"{value['grams']:g}克"
        price='' if value['price_cny'] is None else f"{value['price_cny']:g}元/克"
        expiry=datetime.fromisoformat(row[3]).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        orders=account.get('pending_orders') or {}
        order_note=f"已读取{len(orders.get('records',[]))}笔待成交明细" if orders.get('status')=='fresh' else '条件单明细待更新'
        tracking=(current.get('execution_progress') or {}).get('status')
        progress_note={'pending':'发现对应或相近挂单，仍待成交','partially_filled':'对应或相近条件单已有部分成交','filled':'对应或相近条件单已成交'}.get(tracking)
        structure=format_structure(value)
        body=(f"**真实账户建议，需本人手动确认；程序未自动下单。**\n\n{account['name']} · {action}\n\n"
              f"{quantity} · {condition}{price}\n\n有效至北京时间{expiry} · 建议 #{row[0]}\n\n"
              f"{value['reason']}\n\n{structure}风险：{value['risk_note']}\n\n委托查询：{order_note}{'；'+progress_note if progress_note else ''}\n\n账户快照：{account['fetched_at']}\n\n银行报价时间：{(account.get('quote') or {}).get('quoted_at') or '未知'}\n\n详情见看板账户页。")
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
        return f"{side}{grams:g}克"+(f"（已成交{filled:g}克）" if filled else '')+f" · {symbol.get(order.get('price_condition'),'参考')} {price:g}元/克"
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
