"""Simulation path references existing risk/stop fields; all protection is account-wide."""
from typing import Literal,Optional
from pydantic import BaseModel,Field,ConfigDict


class SimulationPath(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    target_position_pct:Optional[float]=Field(ge=0,le=100,description='路径条件成立后的目标总仓位，非本轮成交量；可至满仓，未知null')
    boundary_source:Literal['risk_exit','stop_loss','none']=Field(description='复用risk_exit_cny或生效止损；none不另设参考边界，仍尊重已生效保护')
    continuation:str=Field(min_length=1,max_length=180,description='上涨/回调各自的参与条件与规模或放弃理由，不能只写再评时间；含截止及暂停/退出，不强制交易')


def boundary(account,decision):
    path=decision.get('position_path')
    if not path:return None
    source=path['boundary_source']
    selected=decision.get('risk_exit_cny') if source=='risk_exit' else (
        None if decision.get('clear_stop_loss') else decision.get('stop_loss_cny') or account.get('stop_price')) if source=='stop_loss' else None
    # A pending buy cannot clear/replace existing protection before that buy fills.
    old=account.get('stop_price') if account.get('grams',0)>1e-9 else None
    levels=[x for x in (selected,old) if x is not None]
    return max(levels) if levels else None


def validate(account,decision,quote):
    path=decision.get('position_path')
    if not path:return
    SimulationPath.model_validate(path)
    source=path['boundary_source']
    effective=None if decision.get('clear_stop_loss') else decision.get('stop_loss_cny') or account.get('stop_price')
    if source=='risk_exit' and decision.get('risk_exit_cny') is None:raise ValueError('建仓路径缺少引用的风险退出参考')
    if source=='stop_loss' and effective is None:raise ValueError('建仓路径引用的止损未生效或已被取消')
    level=boundary(account,decision)
    if decision['action'] in ('hold','wait') and level is not None and quote['price']<=level:
        raise ValueError('建仓边界已触达，不能继续沿用旧边界作为有效持有路径')
    if decision['action']=='buy':
        level=boundary(account,decision)
        entry=quote['price'] if decision['execution']=='market' else decision['limit_price_cny']
        if level is not None and min(quote['price'],entry)<=level:
            raise ValueError('买入进入建仓/已有保护边界；先调整保护并基于新账户版本重新分析')


def display(account,plan,conn=None):
    if not plan:return None
    decision=plan['decision'];path=decision.get('position_path')
    if not path:return None
    return {**path,'boundary_price_cny':boundary(account,decision),'exit_scope':'plan',
        'state':'boundary_reached' if (plan['status']=='cancelled' and plan.get('reason','').startswith('建仓边界')) or (conn is not None and paused(conn,account['key'],plan['id'])) else plan['status'],
        'input_grams':account['grams'],'stop_coverage_grams':account['grams'] if account.get('stop_price') is not None else 0,
        'target_coverage_grams':account['grams'] if account.get('target_price') is not None else 0,
        'execution_note':'模拟盘先执行已生效的全仓保护；保护成交撤销旧委托，按新账户版本重评。风险参考边界只暂停买单，不自动平仓；加仓后保留或新设的保护作用于届时全仓。'}


def paused(conn,account,plan_id):
    import json
    row=conn.execute('SELECT value FROM state WHERE key=?',('simulation_path:'+account,)).fetchone()
    return bool(row and json.loads(row[0]).get('plan_id')==plan_id)


def observe(conn,account,plan,quote,now):
    """A bounded per-account latch; reference boundaries pause review, never sell holdings."""
    import json
    if not plan or plan['status'] not in ('pending','executed'):return False
    decision=json.loads(plan['decision_json'])
    if decision['action'] not in ('buy','hold','wait'):return False
    level=boundary(account,decision)
    if level is None or quote['price']>level or paused(conn,account['key'],plan['id']):return False
    payload={'plan_id':plan['id'],'boundary_price_cny':level,'triggered_at':now,'quote_cny':quote['price']}
    conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 ('simulation_path:'+account['key'],json.dumps(payload),now))
    return True
