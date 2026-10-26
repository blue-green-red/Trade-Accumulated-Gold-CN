"""User-defined JD weekly trading window; no holiday calendar."""
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')


def trading_session(now=None):
    now = (now or datetime.now(BJ)).astimezone(BJ)
    monday = datetime.combine((now-timedelta(days=now.weekday())).date(), time(9), BJ)
    close = datetime.combine(monday.date()+timedelta(days=5), time(2,30), BJ)
    opened = monday <= now < close
    next_open = monday if now < monday else monday+timedelta(days=7)
    return {'timezone':'Asia/Shanghai', 'rule':'周一09:00至周六02:30连续交易；其余时间休市（固定周时段，不另判节假日）',
            'is_open':opened, 'status':'交易中' if opened else '休市',
            'next_open':None if opened else next_open.isoformat(),
            'close_at':close.isoformat() if opened else None}
