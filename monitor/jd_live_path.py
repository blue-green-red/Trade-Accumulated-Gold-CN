"""Link staged entries to exit boundaries; advisory checks never change bank orders."""
from typing import Literal,Optional
from pydantic import BaseModel,Field
from .trade_costs import number
from .jd_live_conditions import stamp


class PositionPath(BaseModel):
    target_position_pct:Optional[float]=Field(ge=0,le=100,allow_inf_nan=False,description='路径条件成立后的目标总仓位，非本轮成交量；可至满仓，未知null')
    boundary_scenario_index:Optional[int]=Field(ge=0,le=1,strict=True,description='暂停追加/退出边界引用downside_scenarios的0起索引；无价格边界null')
    exit_scope:Literal['tranche','plan']=Field(description='边界终止当前这批仓位或整套建仓；均须成交后刷新重评，非自动续买')
    continuation:str=Field(min_length=1,max_length=220,description='上涨/回调各自的参与条件与规模或放弃理由，不能只写再评时间；含截止和退出范围，不重复算式或强制交易')


def remaining(order):
    grams=number(order.get('grams'));filled=number(order.get('filled_grams'))
    if grams is None or (order.get('status')=='partially_filled' and filled is None):return None
    return max(0,grams-(filled or 0))


def pending_orders(source,now):
    return [o for o in (source.get('pending_orders') or {}).get('records',[])
            if o.get('status') in ('pending','partially_filled')]


def coverage(value,source,now):
    orders=source.get('pending_orders') or {};pending=pending_orders(source,now)
    complete=source.get('status')=='fresh' and orders.get('status')=='fresh' and orders.get('complete') is True
    protect=[o for o in pending if o.get('kind')=='take_profit_stop_loss']
    if any(o.get('expires_at') and (stamp(o['expires_at']) is None or stamp(o['expires_at'])<=now) for o in pending):complete=False
    amounts=[remaining(o) for o in protect]
    protected=sum(amounts) if complete and all(g is not None for g in amounts) else None
    drafts=[o for o in value.get('suggested_orders',[]) if not o.get('checks')]
    proposed=sum(o['grams'] for o in drafts if o['kind']=='take_profit_stop_loss')
    buy_quantities=[remaining(o) for o in pending if o.get('action')=='buy']
    immediate=number(value.get('grams')) if value['action']=='buy' else 0
    added=(immediate+sum(buy_quantities)+sum(o['grams'] for o in drafts if o['kind']=='limit_buy')) if (
        complete and immediate is not None and all(g is not None for g in buy_quantities)) else None
    holding=number(source.get('total_grams'))
    after=holding+added if holding is not None and added is not None else None
    return {'input_grams':holding,'confirmed_protected_grams':protected,'suggested_protected_grams':proposed,
            'after_all_buys_grams':after,'additional_buy_grams':added,
            'uncovered_after_buys_grams':max(0,after-protected-proposed) if after is not None and protected is not None else None}


def validate(value,source,now,require=False):
    path=value.get('position_path')
    if path is None:
        return ['缺少本轮结构化建仓路径，需重新评估'] if require and source.get('status')=='fresh' else []
    checks=[];warnings=[];scenarios=value.get('downside_scenarios',[])
    index=path.get('boundary_scenario_index');scenario=scenarios[index] if index is not None and index<len(scenarios) else None
    if index is not None and not scenario:checks.append('建仓边界引用的下跌情景不存在')
    if scenario and (scenario.get('checks') or scenario.get('price_role') not in ('risk_limit','thesis_invalidation') or scenario.get('preferred_action')!='sell'):
        checks.append('建仓退出边界须引用有效且倾向退出的风险限制或假设失效情景')
    path['boundary_price_cny']=scenario.get('price_cny') if scenario else None
    path['boundary_role']=scenario.get('price_role') if scenario else None
    path['input_grams']=source.get('total_grams')
    orders=pending_orders(source,now)
    protections=[o for o in orders if o.get('kind')=='take_profit_stop_loss']+[
        o for o in value.get('suggested_orders',[]) if o['kind']=='take_profit_stop_loss' and not o.get('checks')]
    if protections and not scenario:checks.append('存在退出保护，须明确对应的建仓边界及退出范围')
    boundary=path.get('boundary_price_cny')
    levels=[o.get('loss_price_cny') for o in protections]+([boundary] if boundary is not None else [])
    levels=[n for p in levels if (n:=number(p)) is not None and n>0]
    barrier=max(levels) if levels else None
    if scenario and protections and not any(o.get('loss_price_cny')==boundary for o in protections):
        checks.append('建仓边界与可见退出保护不一致，需先说明并确认撤改后的方案')
    # Even an entry above a stop can overlap it on a gap; bank price-only orders have no lower bound.
    path['execution_note']='到边界或任一保护退出后，暂停旧追加方案，先核对成交、撤改旧买单并刷新账户再复评；独立京东委托不保证成交先后。'
    def local(item,text,prefix):
        item.setdefault('checks',[]).append(text);warnings.append(prefix+text)
    if value['action']=='buy' and barrier is not None:
        quote=number((source.get('quote') or {}).get('buy_price_cny'))
        entry=number(value.get('price_cny'))
        if (quote is not None and quote<=barrier) or (value.get('price_condition')=='at_or_below' and entry is not None and entry<=barrier):
            warnings.append('本轮买入已处在退出保护/建仓边界内，须先核对退出与撤改，刷新重评后再决定买入')
    management={o['order_key']:o for o in value.get('order_management',[])}
    unresolved=False
    for order in orders:
        if order.get('action')!='buy' or barrier is None:continue
        item=management.get(order.get('order_key'));entry=number(order.get('price_cny'))
        if entry is not None and entry<=barrier:
            unresolved=True
            if not item or item.get('action') not in ('cancel','replace'):
                checks.append('已有买单进入退出边界，须明确人工撤改；价格复评不会撤销京东委托')
            elif item.get('action')=='replace' and number((item.get('effective_replacement') or {}).get('price_cny')) is not None and item['effective_replacement']['price_cny']<=barrier:
                local(item,'修改后买单仍处在退出边界内','挂单建议：')
    path['manual_resolution_required']=unresolved
    if unresolved and value['action']=='buy':warnings.append('已有买单与退出边界冲突，建议撤改尚未确认，暂停新增买入并刷新重评')
    if unresolved:
        for order in value.get('suggested_orders',[]):
            if order['kind']=='limit_buy':local(order,'须先确认旧买单撤改结果，再刷新账户重评新增买单','条件单建议：')
    for order in value.get('suggested_orders',[]):
        if order['kind']!='limit_buy' or barrier is None or order['price_cny']>barrier:continue
        local(order,'条件买单进入退出保护/建仓边界，不能作为同一轮独立预挂方案','条件单建议：')
        from .jd_live_entry import same_order
        for review in value.get('limit_buy_reviews',[]):
            if review.get('decision')=='recommend' and same_order(review,order):
                local(review,'推荐买单进入退出边界，应先复评和确认撤改','限价评估：')
    if boundary is not None and scenario and not scenario.get('checks'):
        # Reuse the existing bounded, account-level review watcher rather than another scheduler.
        conditions=value.get('review_conditions',[])
        if not any(c['condition']=='at_or_below' and c['price_cny']==boundary and not c.get('order_key') for c in conditions):
            if len(conditions)<4:
                conditions.append({'condition':'at_or_below','price_cny':boundary,'quote_side':'sell','order_key':None,
                    'reason':'建仓边界：暂停旧追加，核对保护成交与旧买单，刷新账户后重评。','checks':[]})
            else:checks.append('建仓边界缺少账户级价格复评条件，需调整现有复评条件')
    path['checks']=checks
    warnings.extend('建仓路径：'+c for c in checks)
    if checks and value['action']=='buy':warnings.append('本轮新增买入的建仓路径未通过校验')
    if checks:
        for order in value.get('suggested_orders',[]):
            if order['kind']=='limit_buy':local(order,'建仓路径未通过校验，暂停独立预挂买入','条件单建议：')
    path['coverage']=coverage(value,source,now)
    if any(o['kind']=='limit_buy' and not o.get('checks') for o in value.get('suggested_orders',[])) and protections:
        path['execution_note']+='买价高于退出价也可能因跳价同时触发，预挂前须人工核对并接受该限制。'
    return warnings


def format_path(value):
    path=value.get('position_path')
    if not path or path.get('checks'):return ''
    target=path.get('target_position_pct');boundary=path.get('boundary_price_cny')
    scope='整套建仓结束并重评' if path['exit_scope']=='plan' else '当前这批退出，建仓思路仍需重评'
    lines=['建仓路径：'+('目标仓位待确认' if target is None else f'条件成立时目标黄金仓位{target:g}%，非本轮追加指令')]
    lines.append((f'边界{boundary:g}元/克触达时 · ' if boundary is not None else '退出条件满足时 · ')+scope)
    lines.append(path['continuation']);lines.append(path['execution_note'])
    if path.get('manual_resolution_required'):lines.append('须先人工确认旧买单撤改结果，当前不新增独立买单。')
    c=path.get('coverage') or {}
    if c.get('confirmed_protected_grams') is not None:
        lines.append(f"分析时已挂组合保护{c['confirmed_protected_grams']:g}克；新挂建议{c.get('suggested_protected_grams',0):g}克，仅针对确认现仓。")
    if c.get('additional_buy_grams') and c.get('after_all_buys_grams') is not None:
        lines.append(f"若全部新增买入成交，持仓约{c['after_all_buys_grams']:g}克；保护不自动扩量，须按新快照重评。")
    return '\n'.join(lines)


def current_state(advice,account,conn=None):
    """Pause stale entries on GET/delivery without rewriting the archived decision."""
    path=advice.get('position_path')
    if not path:return
    lifecycle=advice.get('lifecycle_status',advice.get('status'))
    path['state']=lifecycle
    if lifecycle!='current':return
    boundary=number(path.get('boundary_price_cny'));quote=account.get('quote') or {}
    current=number(quote.get('sell_price_cny'))
    if account.get('status')!='fresh' or quote.get('status')!='fresh' or boundary is None or current is None:return
    input_price=number(advice.get('input_quote_cny'))
    if advice['action']=='sell' and input_price is not None and input_price<=boundary:
        path['state']='exit_reassessment'
        return
    triggered=False
    if conn is not None:
        from .jd_live_conditions import read_watch,watch_key
        for condition in advice.get('review_conditions',[]):
            if condition['condition']!='at_or_below' or condition['price_cny']!=boundary or condition.get('order_key'):continue
            watch=read_watch(conn,watch_key(conn,advice['analyst'],account['bank'],condition))
            if watch.get('advice_id')==advice['id'] and watch.get('triggered_at'):
                triggered=True;path['boundary_triggered_at']=watch['triggered_at']
    if current>boundary and not triggered:return
    from .jd_live_semantics import blocking_checks
    was_actionable=not blocking_checks(advice)
    path['state']='boundary_reached'
    message='退出边界已触达，旧追加方案暂停，须核对成交和撤改后刷新重评'
    path.setdefault('checks',[]).append(message);advice.setdefault('checks',[]).append('建仓路径：'+message)
    if advice['action']=='buy':
        advice['checks'].append('退出边界已触达，旧买入建议暂停')
        advice['action_status']='review_required'
        advice['_path_pause']=was_actionable
    for order in advice.get('suggested_orders',[]):
        if order['kind'] not in ('limit_buy','take_profit_stop_loss'):continue
        warning=message if order['kind']=='limit_buy' else '当前已触达退出保护，不沿用旧参数新挂，需刷新重评'
        order.setdefault('checks',[]).append(warning);advice['checks'].append('条件单建议：'+warning)
        if order['kind']=='limit_buy':
            from .jd_live_entry import same_order
            for review in advice.get('limit_buy_reviews',[]):
                if review.get('decision')=='recommend' and same_order(review,order):
                    review.setdefault('checks',[]).append(message);advice['checks'].append('限价评估：'+message)
    advice['status']='review_required'
