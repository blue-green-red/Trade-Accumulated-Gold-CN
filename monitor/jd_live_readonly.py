"""Separate live-account ledger boundary. No login secrets, model execution or order submission."""
from .jd_readonly_probe import BANKS

SCHEMA='''CREATE TABLE IF NOT EXISTS jd_live_readonly (
 bank TEXT PRIMARY KEY, checked_at TEXT, public_status TEXT NOT NULL DEFAULT 'not_checked'
);'''


def ensure_schema(conn):
 with conn:
  conn.executescript(SCHEMA)
  for bank in BANKS:conn.execute('INSERT OR IGNORE INTO jd_live_readonly(bank) VALUES(?)',(bank,))


def accounts(conn):
 exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jd_live_readonly'").fetchone()
 records={r[0]:{'checked_at':r[1],'public_status':r[2]} for r in conn.execute('SELECT bank,checked_at,public_status FROM jd_live_readonly')} if exists else {}
 return [{'bank':bank,'name':data['name'],'official_url':data['url'],**records.get(bank,{'checked_at':None,'public_status':'not_checked'}),
          'status':'login_required','connected':False,'order_submission_enabled':False,
          'cash_cny':None,'total_grams':None,'sellable_grams':None,'orders':None,
          'message':'公开页面可查询，真实账户仍需登录并验证字段映射；未同步余额、持仓或订单。'} for bank,data in BANKS.items()]
