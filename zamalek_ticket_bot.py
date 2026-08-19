#!/usr/bin/env python3
"""
zamalek_ticket_bot.py
======================
High-resilience Tazkarti Matches Monitor (https://www.tazkarti.com/#/matches)
Optimized for SPA rendering, Anti-Bot bypass, and silent monitoring.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

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
    tazkarti_url: str = _env_str("TAZKARTI_URL", "https://www.tazkarti.com/#/matches")
    
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

    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 10.0)
    request_timeout_seconds: float = _env_float("REQUEST_TIMEOUT_SECONDS", 30.0)
    state_file: Path = Path(_env_str("STATE_FILE", "zamalek_bot_state.json"))

    def validate(self) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")


CONFIG = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("zamalek_bot")


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


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

    def alert_ticket_available(self, match_name: str, url: str) -> bool:
        text = (
            "🚨 <b>تذاكر الزمالك نزلت الآن!</b> 🚨\n\n"
            f"🏟️ <b>{match_name}</b>\n"
            f"🎟️ الحالة: <b>متاح للحجز الآن</b>\n\n"
            f"👉 <a href=\"{url}\">اضغط هنا للحجز فوراً من تذكرتي</a>"
        )
        return self.send(text)


class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.notifier = TelegramNotifier(cfg)
        self._stop = asyncio.Event()
        self.total_polls = 0
        self.start_time = time.time()
        self.last_poll_time = "جارٍ الفحص..."
        self.already_alerted = False

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
                                "🟢 <b>البوت يعمل بنجاح ويراقب صفحة الماتشات:</b>\n\n"
                                f"⏱️ <b>مدة العمل:</b> {h} ساعة و {m} دقيقة\n"
                                f"🔄 <b>مرات الفحص:</b> {self.total_polls}\n"
                                f"🕒 <b>آخر فحص:</b> {self.last_poll_time}\n"
                                f"🎯 <b>الرابط:</b> Tazkarti Matches"
                            )
                            self.notifier.send(status_text)
            except Exception:
                pass
            await asyncio.sleep(2)

    async def run(self) -> None:
        log.info("Starting matches browser monitoring...")
        asyncio.create_task(self._telegram_listener_loop())

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="ar-EG",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()

            while not self._stop.is_set():
                cycle_start = time.monotonic()
                try:
                    await page.goto(
                        self.cfg.tazkarti_url,
                        wait_until="networkidle",
                        timeout=int(self.cfg.request_timeout_seconds * 1000)
                    )
                    # إعطاء فرصة لإطارات الجافاسكريبت لتحميل كروت المباريات
                    await page.wait_for_timeout(2500)
                    content = await page.content()
                    
                    self.total_polls += 1
                    self.last_poll_time = datetime.now().strftime("%I:%M:%S %p")
                    log.info("Successfully checked matches. Poll count: %d", self.total_polls)

                    soup = BeautifulSoup(content, "lxml")
                    text_content = soup.get_text()

                    # التحقق من وجود الزمالك وحالة التوفر
                    has_zamalek = any(k.lower() in text_content.lower() for k in self.cfg.team_keywords)
                    has_available = any(k.lower() in text_content.lower() for k in self.cfg.available_keywords)

                    if has_zamalek and has_available:
                        if not self.already_alerted:
                            log.warning("ZAMALEK MATCH TICKETS DETECTED!")
                            self.notifier.alert_ticket_available("مباراة لنادي الزمالك", self.cfg.tazkarti_url)
                            self.already_alerted = True
                    else:
                        self.already_alerted = False

                except Exception as exc:
                    log.error("Check failed: %s", exc)

                elapsed = time.monotonic() - cycle_start
                sleep_for = max(3.0, self.cfg.check_interval_seconds - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

            await browser.close()


async def _amain() -> None:
    CONFIG.validate()
    monitor = Monitor(CONFIG)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, monitor.request_stop)
        except NotImplementedError:
            pass
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(_amain())
