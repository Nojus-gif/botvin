"""Phone notifications. ntfy (simplest: install the ntfy app, subscribe to your
topic) and/or Telegram (create a bot with @BotFather). Console always logs."""

from __future__ import annotations

import logging

import requests

from .models import Item
from .pricing import Estimate
from .scorer import Verdict

log = logging.getLogger(__name__)


def format_message(item: Item, est: Estimate, v: Verdict,
                   heating_up: bool = False) -> tuple[str, str]:
    prefix = "🔥 " if v.demand.level == "hot" else ""
    title = f"{prefix}+{v.profit:.0f}€  {est.brand or item.brand} {est.category} · {item.total_price:.2f}€"
    lines = [
        item.title,
        f"Buy {item.total_price:.2f}€ → resell ~{est.resale:.0f}€  (margin {v.margin:.0%})",
        f"Size {item.size or '?'} · {item.condition.replace('_', ' ')}",
        v.demand.describe(),
    ]
    if heating_up:
        lines.insert(0, "Demand picked up since it was listed:")
    if est.market_samples:
        lines.append(f"Market: median {est.market_median:.0f}€ over {est.market_samples} listings")
    if est.flags:
        lines.append("⚠ " + ", ".join(est.flags))
    return title, "\n".join(lines)


class Notifier:
    def __init__(self, cfg: dict):
        self.ntfy = cfg.get("ntfy") or {}
        self.telegram = cfg.get("telegram") or {}

    def send(self, item: Item, est: Estimate, v: Verdict, heating_up: bool = False) -> None:
        title, body = format_message(item, est, v, heating_up)
        log.info("DEAL %s\n%s\n%s", title, body, item.url)
        if self.ntfy.get("topic"):
            self._ntfy(title, body, item, v)
        if self.telegram.get("token") and self.telegram.get("chat_id"):
            self._telegram(title, body, item)

    def send_text(self, title: str, body: str) -> None:
        if self.ntfy.get("topic"):
            self._ntfy_post({"title": title, "message": body})
        if self.telegram.get("token") and self.telegram.get("chat_id"):
            self._tg("sendMessage", {"chat_id": self.telegram["chat_id"], "text": f"{title}\n{body}"})

    def _ntfy(self, title: str, body: str, item: Item, v: Verdict) -> None:
        payload = {
            "title": title,
            "message": body,
            "click": item.url,
            "priority": 5 if v.priority == "high" else 4,
            "tags": ["moneybag"],
            "actions": [{"action": "view", "label": "Open on Vinted", "url": item.url}],
        }
        if item.photo_url:
            payload["attach"] = item.photo_url
        self._ntfy_post(payload)

    def _ntfy_post(self, payload: dict) -> None:
        # JSON publishing handles UTF-8 titles (€, ♥, Lithuanian letters) cleanly.
        server = self.ntfy.get("server", "https://ntfy.sh").rstrip("/")
        headers = {}
        if self.ntfy.get("token"):
            headers["Authorization"] = f"Bearer {self.ntfy['token']}"
        try:
            requests.post(server, json={"topic": self.ntfy["topic"], **payload},
                          headers=headers, timeout=10).raise_for_status()
        except requests.RequestException as e:
            log.error("ntfy failed: %s", e)

    def _telegram(self, title: str, body: str, item: Item) -> None:
        text = f"<b>{_esc(title)}</b>\n{_esc(body)}\n<a href=\"{item.url}\">Open on Vinted</a>"
        chat = self.telegram["chat_id"]
        if item.photo_url:
            ok = self._tg("sendPhoto", {"chat_id": chat, "photo": item.photo_url,
                                        "caption": text[:1024], "parse_mode": "HTML"})
            if ok:
                return
        self._tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML"})

    def _tg(self, method: str, payload: dict) -> bool:
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.telegram['token']}/{method}",
                              json=payload, timeout=10)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("telegram %s failed: %s", method, e)
            return False


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
