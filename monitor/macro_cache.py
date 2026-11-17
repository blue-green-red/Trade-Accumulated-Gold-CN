"""宏观报价与日历独立刷新；仅程序取数，不触发模型调用。"""
from datetime import datetime

QUOTE_SECONDS = 300
CALENDAR_SECONDS = 1800
EVENT_SECONDS = 60


def age_seconds(stamp, now):
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def calendar_interval(events, now):
    # 与现有 AI 数据公布触发范围一致：美国重大/重要数据，公布前 15 分钟至后 30 分钟。
    for event in events or []:
        if event.get("country") != "US" or event.get("tier", 3) > 2:
            continue
        distance = age_seconds(event.get("time_utc"), now)
        if -900 <= distance <= 1800:
            return EVENT_SECONDS
    return CALENDAR_SECONDS


def refresh(store, now):
    import macro_data
    cached = store.get_state("web_macro", {})
    for kind in ("quotes", "calendar"):
        if kind in cached:
            cached.setdefault(kind + "_fetched_at", cached.get("fetched_at"))
    errors = dict(cached.get("refresh_errors") or {})
    changed = False
    previous_fetch = cached.get("fetched_at")
    for kind, interval, fetch in (
        ("quotes", QUOTE_SECONDS, macro_data.fetch_macro_quotes),
        ("calendar", calendar_interval(cached.get("calendar"), now), macro_data.fetch_calendar),
    ):
        stamp = cached.get(kind + "_checked_at", cached.get(kind + "_fetched_at", previous_fetch))
        age = age_seconds(stamp, now)
        if 0 <= age < interval:
            continue
        cached[kind + "_checked_at"] = now.isoformat(timespec="seconds")
        changed = True
        try:
            value=fetch()
            if kind=='calendar':
                from .official_calendar import supplement,merge
                official,coverage=supplement(store,now)
                value=merge(value,official)
                cached['calendar_sources']=coverage
            cached[kind] = value
            cached[kind + "_fetched_at"] = now.isoformat(timespec="seconds")
            errors.pop(kind, None)
            cached["fetched_at"] = now.isoformat(timespec="seconds")
        except Exception as exc:
            # 保留上次成功数据及其时间，失败不会伪装成新数据。
            errors[kind] = type(exc).__name__
    cached["hard_events_24h"] = [e for e in cached.get("calendar") or []
        if e.get("tier") == 1 and not e.get("released")
        and -86400 <= age_seconds(e.get("time_utc"), now) <= 0]
    cached["calendar_refresh_seconds"] = calendar_interval(cached.get("calendar"), now)
    cached["refresh_errors"] = errors
    cached["errors"] = [f"{kind}: {error}" for kind, error in errors.items()]
    # 时间推进也会改变 upcoming 硬事件，不依赖网络请求才重算。
    if changed or cached != store.get_state("web_macro", {}):
        store.set_state("web_macro", cached)
    return cached
