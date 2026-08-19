#!/usr/bin/env python3
"""
zamalek_ticket_bot.py
======================
Ultra-fast, direct JSON API monitor for Tazkarti Zamalek matches.
Polls every 3 seconds with zero overhead and full /ping support.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass
class Config:
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")
    
    # Endpoint المباشر السريع لموقع تذكرتي
    api_url: str = "https://tazkarti.com/data/matches-list-json.json"
    
    team_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "TEAM_KEYWORDS",
            ["Zamalek", "zamalek", "الزمالك", "Zamalek SC", "نادي الزمالك"]
        )
    )
    available_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "AVAILABLE_KEYWORDS",
            ["Book Ticket", "Book Now", "Available", "حجز تذكرة", "احجز الان", "احجز الآن", "متاح"]
        )
    )

    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 3.0)
    request_timeout_seconds: float = _env_float("REQUEST_TIMEOUT_SECONDS", 5.0)

    def validate(self) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")


CONFIG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("zamalek_bot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tazkarti.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class TelegramNotifier:
    API_BASE = "https://api.telegram.org"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()

    def send(self, text: str) -> bool:
        url = f"{self.API_BASE}/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.cfg.telegram_chat_id, "text": text, "parse_mode": "HTML"}
        try:
            r = self._session.post(url, json=payload, timeout=10)
            return r.status_code == 200
        except Exception as exc:
            log.error("Telegram send error: %s", exc)
            return False

    def alert_ticket_available(self, match_title: str, match_date: str = "") -> bool:
        text = (
            "🚨 <b>تذاكر الزمالك نزلت الآن!</b> 🚨\n\n"
            f"🏟️ <b>{match_title}</b>\n"
            f"🗓️ {match_date or 'الموعد محدد على الموقع'}\n"
            f"🎟️ الحالة: <b>متاح للحجز فوراً</b>\n\n"
            "👉 <a href=\"https://www.tazkarti.com/#/matches\">اضغط هنا للدخول على الحجز فوراً</a>"
        )
        return self.send(text)


class FastMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.notifier = TelegramNotifier(cfg)
        self._stop = asyncio.Event()
        self._session = requests.Session()
        self.total_polls = 0
        self.start_time = time.time()
        self.last_poll_time = "جارٍ البدء..."
        self.known_available_matches: set[str] = set()

    def request_stop(self) -> None:
        self._stop.set()

    async def _telegram_listener_loop(self) -> None:
        offset = 0
        url = f"{TelegramNotifier.API_BASE}/bot{self.cfg.telegram_bot_token}/getUpdates"
        
        while not self._stop.is_set():
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    url,
                    params={"offset": offset, "timeout": 0},
                    timeout=8
                )
                if resp.status_code == 200:
                    for item in resp.json().get("result", []):
                        offset = item["update_id"] + 1
                        msg = item.get("message", {})
                        text = msg.get("text", "").strip().lower()
                        sender_id = str(msg.get("chat", {}).get("id", ""))

                        if sender_id == str(self.cfg.telegram_chat_id) and text in ("/ping", "/status", "ping", "status"):
                            uptime_sec = int(time.time() - self.start_time)
                            m, s = divmod(uptime_sec, 60)
                            h, m = divmod(m, 60)
                            
                            status_text = (
                                "⚡ <b>البوت السريع شغال بأقصى كفاءة:</b>\n\n"
                                f"⏱️ <b>مدة العمل:</b> {h} ساعة و {m} دقيقة\n"
                                f"🔄 <b>مرات الفحص:</b> {self.total_polls}\n"
                                f"🕒 <b>آخر فحص:</b> {self.last_poll_time}\n"
                                f"🎯 <b>النوع:</b> Direct Fast API (~3s)"
                            )
                            self.notifier.send(status_text)
            except Exception:
                pass
            await asyncio.sleep(2)

    def _fetch_matches_json(self) -> list[dict]:
        # نضع timestamp في الرابط لمنع الـ Caching والحصول على أحدث داتا حية
        url = f"{self.cfg.api_url}?_={int(time.time() * 1000)}"
        resp = self._session.get(url, headers=HEADERS, timeout=self.cfg.request_timeout_seconds)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("data", []) or data.get("matches", []) or [data]
        return []

    async def run(self) -> None:
        log.info("Direct JSON Fast Monitor started. Polling every %.1fs", self.cfg.check_interval_seconds)
        asyncio.create_task(self._telegram_listener_loop())

        while not self._stop.is_set():
            cycle_start = time.monotonic()
            try:
                matches = await asyncio.to_thread(self._fetch_matches_json)
                self.total_polls += 1
                self.last_poll_time = datetime.now().strftime("%I:%M:%S %p")

                for match in matches:
                    # تحويل كائن الماتش بالكامل إلى نص لفحصه بسهولة
                    match_str = json.dumps(match, ensure_ascii=False)
                    
                    # التحقق من أن الماتش للزمالك
                    is_zamalek = any(k.lower() in match_str.lower() for k in self.cfg.team_keywords)
                    if not is_zamalek:
                        continue

                    # استخراج اسم المباراة أو الفرق
                    title = match.get("matchName") or match.get("title") or match.get("name") or "مباراة نادي الزمالك"
                    match_date = match.get("matchDate") or match.get("date") or ""
                    match_id = str(match.get("id") or match.get("matchId") or title)

                    # التحقق من حالة توفر التذاكر
                    is_available = any(k.lower() in match_str.lower() for k in self.cfg.available_keywords)

                    if is_available:
                        if match_id not in self.known_available_matches:
                            log.warning("ZAMALEK TICKETS AVAILABLE -> %s", title)
                            self.notifier.alert_ticket_available(title, match_date)
                            self.known_available_matches.add(match_id)
                    else:
                        self.known_available_matches.discard(match_id)

                log.info("Checked matches successfully. Total checks: %d", self.total_polls)

            except Exception as exc:
                log.error("Fetch API error: %s", exc)

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.5, self.cfg.check_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass


async def _amain() -> None:
    CONFIG.validate()
    monitor = FastMonitor(CONFIG)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, monitor.request_stop)
        except NotImplementedError:
            pass
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(_amain())
