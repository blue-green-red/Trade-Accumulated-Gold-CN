"""Shared meaning of advisory terms and independently validated notification parts."""
from copy import deepcopy
import json


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


def blocking_checks(value):
    local={prefix+c for field,prefix in (('order_management','挂单建议：'),
           ('review_conditions','复评条件：'),('suggested_orders','条件单建议：'))
           for item in value.get(field,[]) for c in item.get('checks',[])}
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
