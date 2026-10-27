"""Dashboard visibility follows model switches; never deletes trading or training history."""
from .controls import MODELS,control


def hidden_models(conn):return {m for m in MODELS if not control(conn,m)['enabled']}


def owner(value,hidden):
    return isinstance(value,str) and any(value.lower()==m or value.lower().startswith(m+':') for m in hidden)


def clean(value,hidden):
    if isinstance(value,dict):
        if any(owner(value.get(k),hidden) for k in ('key','account','strategy','analyst','model','_analyst','name')):return None
        if any(m in str(value.get('title','')).lower() for m in hidden):return None
        return {k:clean(v,hidden) for k,v in value.items() if not owner(k,hidden)}
    if isinstance(value,list):return [v for item in value if (v:=clean(item,hidden)) is not None]
    return None if owner(value,hidden) else value


def dashboard_visible(conn,data):
    hidden=hidden_models(conn)
    controls=data.pop('model_controls',[])
    result=clean(data,hidden)
    result['model_controls']=[c if c['key'] not in hidden else {'key':c['key'],'name':c['name'],'enabled':False} for c in controls]
    return result
