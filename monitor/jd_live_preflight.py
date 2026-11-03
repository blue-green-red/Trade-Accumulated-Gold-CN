"""Refresh real-account inputs before taking the shared AI snapshot; never run a browser here."""
from datetime import datetime, timezone
import time

from .jd_live_readonly import accounts, age
from .jd_live_sync import request_refresh

MAX_INPUT_AGE = 120
WAIT_SECONDS = 45
RETRY_SECONDS = 300
TERMINAL = {'completed', 'login_required', 'unavailable', 'waiting_for_resources'}


def missing_inputs(conn, now=None):
    now = now or datetime.now(timezone.utc)
    rows = accounts(conn, now, include_advice=False)
    if not any(a.get('fetched_at') for a in rows):
        return None  # No real account configured; simulation-only deployments do not start collection.
    missing = {}
    for a in rows:
        needs = []
        times = a.get('component_times') or {}
        if not a['connected'] or not 0 <= age(times.get('account') or a.get('fetched_at'), now) <= MAX_INPUT_AGE:
            needs.append('account')
        if a.get('cash_cny') is None or (a.get('cash_source') == 'jd_electronic_account_api' and
                (a.get('coverage', {}).get('cash_balance') != 'available' or
                 not 0 <= age(times.get('cash_balance'), now) <= MAX_INPUT_AGE)):
            needs.append('cash')
        orders = a.get('pending_orders') or {}
        if orders.get('status') != 'fresh' or not orders.get('complete') or not 0 <= age(times.get('pending_orders'), now) <= MAX_INPUT_AGE:
            needs.append('orders')
        if (a.get('quote') or {}).get('status') != 'fresh':
            needs.append('quote')
        if needs:
            missing[a['bank']] = needs
    return missing


def prepare(store, wait_seconds=WAIT_SECONDS):
    """Share one bounded refresh across all models. Terminal failures permit explicitly degraded input.

    If collection is still in flight, defer the AI instead of overlapping cookie-recovery Chrome.
    No invocation count, manual request, or trigger event is consumed by this wait.
    """
    started = time.monotonic()
    missing = missing_inputs(store.conn)
    def finish(status, can_analyze=True, requested_at=None):
        result = {'status': status, 'can_analyze': can_analyze, 'missing': missing or {},
                  'checked_at': datetime.now(timezone.utc).isoformat(),
                  'waited_seconds': round(time.monotonic() - started, 2), 'requested_at': requested_at}
        store.set_state('jd_live_preflight', result)
        return result
    if missing is None:
        return {'status': 'not_configured', 'can_analyze': True, 'waited_seconds': 0}
    sync = store.get_state('jd_live_sync_status', {})
    if not missing and not (sync.get('status')=='running' and age(sync.get('at'),datetime.now(timezone.utc))<300):
        return finish('ready')
    old = store.get_state('jd_live_preflight', {})
    now = datetime.now(timezone.utc)
    if (old.get('status') == 'degraded' and age(old.get('checked_at'), now) < RETRY_SECONDS and
            old.get('missing') == missing and sync.get('status') != 'running'):
        # Keep the original cooldown anchor; otherwise every AI round would postpone recovery forever.
        return {**old, 'waited_seconds': 0}
    request = request_refresh(store.conn, components=('core', 'quote', 'orders'), source='analysis')
    requested = request['requested_at']
    store.set_state('jd_live_preflight', {'status': 'waiting', 'missing': missing,
                                        'requested_at': requested, 'checked_at': now.isoformat()})
    while True:
        current = store.get_state('jd_live_refresh', {})
        sync = store.get_state('jd_live_sync_status', {})
        # A newer manual refresh may supersede ours; it covers the same critical components.
        if current.get('requested_at', '') >= requested and current.get('status') in TERMINAL:
            missing = missing_inputs(store.conn)
            if current.get('status')=='waiting_for_resources':
                return finish('deferred',can_analyze=False,requested_at=requested)
            return finish('ready' if not missing else 'degraded', requested_at=requested)
        if time.monotonic() - started >= wait_seconds:
            missing=missing_inputs(store.conn)
            collecting=sync.get('status')=='running' and age(sync.get('at'),datetime.now(timezone.utc))<300
            return finish('deferred' if collecting else 'degraded', can_analyze=not collecting, requested_at=requested)
        time.sleep(min(1, max(.01, wait_seconds - (time.monotonic() - started))))
