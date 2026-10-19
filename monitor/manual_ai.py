"""Authenticated, persistent, one-shot model requests consumed by the serial monitor worker."""
import uuid
from .controls import control,active
from .cadence import allowed
from .store import utc_now
from .price_guard import read,save

TRIGGER='网页手动触发'


class RequestError(ValueError):
    def __init__(self,message,status=409):super().__init__(message);self.status=status


def status(conn,model):
    item=read(conn,'ai_manual:'+model)
    if item.get('status') in ('queued','running') and not active(conn,model,item.get('generation')):
        return {**item,'status':'cancelled','message':'模型开关已变化，本次手动请求已取消'}
    return item


def request(conn,settings,model):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if not active(conn,model):raise RequestError('请先开启该模型')
        old=status(conn,model)
        if old.get('status') in ('queued','running'):return old
        if not read(conn,'ai_status:'+model).get('available',False):raise RequestError('该模型分析后端暂不可用')
        if not allowed(settings,conn,model,utc_now()):raise RequestError('今日普通分析额度已用完，紧急事件预留额度不用于手动触发',429)
        item={'id':str(uuid.uuid4()),'status':'queued','requested_at':utc_now().isoformat(),
              'generation':control(conn,model)['generation'],'message':'已排队，等待监控使用最新行情分析并操盘'}
        save(conn,'ai_manual:'+model,item)
        return item


def ready(conn,settings,model):
    item=status(conn,model)
    if item.get('status')!='queued':return False
    if allowed(settings,conn,model,utc_now()):return True
    with conn:save(conn,'ai_manual:'+model,{**item,'status':'failed','message':'今日普通分析额度已用完，请在额度恢复后重新提交'})
    return False


def claim(conn,model,run_id):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        item=status(conn,model)
        if item.get('status')!='queued':return None
        item.update(status='running',started_at=utc_now().isoformat(),run_id=run_id,message='正在分析并提交六账户决策')
        save(conn,'ai_manual:'+model,item)
        return item


def finish(conn,model,item,result):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        current=read(conn,'ai_manual:'+model)
        if current.get('id')!=item['id']:return
        if not active(conn,model,item['generation']):state,message='cancelled','模型已关闭或重新开启，本次请求已取消'
        elif result is None:state,message='failed','本次分析未完成，请查看分析记录；不会自动重复本次手动请求'
        elif (result.get('_price_guard') or {}).get('invalidated_at'):state,message='completed','分析已完成但行情异动导致决策失效，已排队复查'
        else:state,message='completed','分析已完成，六账户决策按条件执行；不代表必然成交'
        current.update(status=state,message=message,finished_at=utc_now().isoformat(),view_id=(result or {}).get('_view_id'))
        save(conn,'ai_manual:'+model,current)


def recover(conn,model):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        item=read(conn,'ai_manual:'+model)
        if item.get('status')=='running':
            item.update(status='failed',message='监控服务已重启，本次请求中断；请查看记录后按需重新触发',finished_at=utc_now().isoformat())
            save(conn,'ai_manual:'+model,item)
