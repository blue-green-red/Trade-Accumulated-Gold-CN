"""微信推送：优先 PushPlus（免费额度大），其次 Server酱（免费版每天条数很少）。都没配置时只打日志。"""

from __future__ import annotations

import logging
import re

import requests

from .config import Settings

log = logging.getLogger("monitor.notify")

PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"


class Notifier:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.last_receipt = {}

    @property
    def channel(self) -> str:
        if self.dry_run:
            return "dry-run"
        if self.settings.pushplus_token:
            return "pushplus"
        if self.settings.serverchan_sendkey:
            return "serverchan"
        return "log-only"

    def send(self, title: str, body_markdown: str, *, body_html: str | None = None) -> bool:
        """Return provider acceptance, not proof of delivery to or reading on the phone."""
        title = title[:32]
        channel = self.channel
        self.last_receipt = {'channel':channel,'status':'not_sent','provider_id':None,'provider_code':None}
        if channel in ("dry-run", "log-only"):
            log.info("[%s] %s\n%s", channel, title, body_markdown)
            return False
        try:
            if channel == "pushplus":
                resp = requests.post(
                    PUSHPLUS_URL,
                    json={"token": self.settings.pushplus_token, "title": title,
                          "content": body_html if body_html is not None else body_markdown,
                          "template": "html" if body_html is not None else "markdown", "channel":"wechat"},
                    timeout=15,
                )
                payload=resp.json();ok = resp.ok and payload.get("code") == 200
                message_id=payload.get('data')
                if isinstance(message_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',message_id) and message_id!=self.settings.pushplus_token:
                    self.last_receipt['provider_id']=message_id
            else:
                resp = requests.post(
                    SERVERCHAN_URL.format(key=self.settings.serverchan_sendkey),
                    data={"title": title, "desp": body_markdown},
                    timeout=15,
                )
                payload=resp.json();ok = resp.ok and payload.get("code") == 0
            self.last_receipt.update(status='accepted' if ok else 'failed',provider_code=str(payload.get('code'))[:32])
            if not ok:
                log.warning("推送失败 [%s]: HTTP %s, provider code %s",channel,resp.status_code,self.last_receipt['provider_code'])
            return ok
        except Exception as exc:
            # Read timeouts / broken connections can happen after provider acceptance.
            self.last_receipt['status']='failed' if isinstance(exc,requests.ConnectTimeout) else 'uncertain'
            log.warning("推送异常 [%s]: %s", channel, type(exc).__name__)
            return False
