"""Shared meaning of advisory terms and independently validated notification parts."""
from copy import deepcopy
import json


def minimum_buy_checks(value, minimum):
    """Validate proposed new exposure, including replacements, without changing quantities."""
    if minimum is None:
        return []
    quantities = []
    if value.get('action') == 'buy':
        quantities.append(value.get('grams'))
    quantities.extend(order.get('grams') for order in value.get('suggested_orders', [])
                      if order.get('kind') == 'limit_buy')
    quantities.extend(review.get('grams') for review in value.get('limit_buy_reviews', [])
                      if review.get('decision') == 'recommend')
    for item in value.get('order_management', []):
        replacement = item.get('effective_replacement') or {}
        if item.get('action') == 'replace' and replacement.get('action') == 'buy':
            quantities.append(replacement.get('grams'))
    if any(grams is None or grams < minimum for grams in quantities):
        return [f'买入建议须明确数量且单笔至少{minimum:g}克，本轮建议不满足该要求']
    return []


def immediate_signature(value):
    value=value or {}
    result={'action':value.get('action')}
    if result['action'] in ('buy','sell'):
        result['grams']=value.get('grams')
        if value.get('price_condition') in ('at_or_above','at_or_below'):
            result.update(price_condition=value['price_condition'],price_cny=value.get('price_cny'))
    return result


def management_signature(value):
    return sorted(json.dumps({'order_key':o.get('order_key'),'action':o.get('action'),
        'changes':{k:v for k,v in (o.get('replacement') or {}).items() if v is not None}
        if o.get('action')=='replace' else {}},sort_keys=True)
        for o in (value or {}).get('order_management',[]))


def plan_terms(value):
    """Economic comparison only; never rounds or changes executable price conditions."""
    from .jd_live_proposals import proposal_signature
    from .jd_live_entry import signature
    value=value or {}
    return {'trade':immediate_signature(value),'proposals':proposal_signature(value),
            'management':management_signature(value),'entry':[list(item) for item in signature(value)],
            'target':(value.get('position_path') or {}).get('target_position_pct'),
            'boundary':(value.get('position_path') or {}).get('boundary_price_cny'),
            'scope':(value.get('position_path') or {}).get('exit_scope'),
            'invalidated':(value.get('reassessment') or {}).get('thesis_status')=='invalidated'}


def same_plan(left,right,move_pct=.4):
    from copy import deepcopy
    if not left or not right:return False
    left,right=deepcopy(left),deepcopy(right)
    a=left.get('trade') or {};b=right.get('trade') or {}
    p,q=a.pop('price_cny',None),b.pop('price_cny',None)
    if left!=right:return False
    if p is None or q is None:return p==q
    try:return p>0 and q>0 and abs(q/p-1)*100+1e-9<move_pct
    except (TypeError,ZeroDivisionError):return False


def blocking_checks(value):
    local={prefix+c for field,prefix in (('order_management','挂单建议：'),
           ('review_conditions','复评条件：'),('suggested_orders','条件单建议：'),('downside_scenarios','下跌情景：'),('limit_buy_reviews','限价评估：'))
           for item in value.get(field,[]) for c in item.get('checks',[])}
    local.update('建仓路径：'+c for c in (value.get('position_path') or {}).get('checks',[]))
    return [c for c in value.get('checks',[]) if c not in local]


def notification_parts(value):
    """Keep unknown/global warnings blocking; omit only explicitly checked local items."""
    if blocking_checks(value):return None
    result=deepcopy(value)
    for field in ('order_management','review_conditions','suggested_orders'):
        result[field]=[item for item in result.get(field,[]) if not item.get('checks')]
    result['notification_partial']=bool(value.get('checks'))
    result['checks']=[]
    return result
