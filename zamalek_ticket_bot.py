#!/usr/bin/env python3
"""
zamalek_ticket_bot.py
======================
Ultra-reliable Tazkarti JSON API monitor for Zamalek matches.
Includes a lightweight built-in HTTP server for keep-alive / 24-7 uptime pinging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from dotenv import load_dotenv

load_dotenv()


# --- Built-in Web Server for 24/7 Keep-Alive ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Zamalek Ticket Bot is Running 24/7!")

    def log_message(self, format, *args):
        # تعطيل طباعة لوجات الويب لتقليل الزحمة في الـ Console
        pass


def start_web_server(port: int = 8080):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception as exc:
        logging.error("Web server error: %s", exc)


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
    api_url: str = "https://tazkarti.com/data/matches-list-json.json"
    
    team_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "TEAM_KEYWORDS",
            ["Zamalek", "zamalek", "الزمالك", "Zamalek SC", "نادي الزمالك"]
        )
    )

    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 3.0)
    request_timeout_seconds: float = _env_float("REQUEST_TIMEOUT_SECONDS", 6.0)

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

    def alert_ticket_available(self, match_title: str, tournament_name: str = "") -> bool:
        text = (
            "فتح الحجز\n"
            f"{match_title}\n"
            f"{tournament_name}\n\n"
            "https://tazkarti.com/#/matches"
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
        self.last_poll_time = "جارٍ الفحص..."
        self.seen_matches: set[str] = set()
        self.latest_detected_titles: list[str] = []

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
                            
                            matches_info = "\n".join([f"• {t}" for t in self.latest_detected_titles[:5]]) if self.latest_detected_titles else "لا توجد مباريات معروضة حالياً"

                            status_text = (
                                "⚡ <b>حالة البوت المباشرة:</b>\n\n"
                                f"⏱️ <b>مدة العمل:</b> {h} ساعة و {m} دقيقة\n"
                                f"🔄 <b>مرات الفحص:</b> {self.total_polls}\n"
                                f"🕒 <b>آخر فحص:</b> {self.last_poll_time}\n\n"
                                f"📋 <b>المباريات الحقيقية المرصودة:</b>\n{matches_info}"
                            )
                            self.notifier.send(status_text)
            except Exception:
                pass
            await asyncio.sleep(2)

    def _fetch_matches_json(self) -> list[dict]:
        url = f"{self.cfg.api_url}?_={int(time.time() * 1000)}"
        resp = self._session.get(url, headers=HEADERS, timeout=self.cfg.request_timeout_seconds)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("data", []) or data.get("matches", []) or data.get("result", []) or [data]
        return []

    async def run(self) -> None:
        log.info("Direct JSON Monitor running with instant match detection.")
        asyncio.create_task(self._telegram_listener_loop())

        while not self._stop.is_set():
            cycle_start = time.monotonic()
            try:
                matches = await asyncio.to_thread(self._fetch_matches_json)
                self.total_polls += 1
                self.last_poll_time = datetime.now().strftime("%I:%M:%S %p")

                current_titles = []
                for match in matches:
                    raw_str = json.dumps(match, ensure_ascii=False)
                    
                    # استخراج أسماء الفرق إن وجدت
                    t1 = str(match.get('team1') or "").strip()
                    t2 = str(match.get('team2') or "").strip()
                    fallback_title = f"{t1} vs {t2}".strip()
                    
                    if fallback_title == "vs":
                        fallback_title = ""

                    title = (
                        match.get("matchName") or 
                        match.get("title") or 
                        match.get("name") or 
                        match.get("eventName") or 
                        fallback_title
                    )

                    # --- فلتر الأمان للقوالب الوهمية ---
                    if not title or str(title).strip().lower() == "vs":
                        continue
                    
                    title = str(title).strip()
                    current_titles.append(title)

                    is_zamalek = any(k.lower() in raw_str.lower() for k in self.cfg.team_keywords)
                    if not is_zamalek:
                        continue

                    match_key = str(match.get("id") or match.get("matchId") or title)
                    is_sold_out = any(bad in raw_str.lower() for bad in ["soldout", "sold_out", "نفذت", "closed"])
                    
                    if not is_sold_out and match_key not in self.seen_matches:
                        log.warning("ZAMALEK MATCH FOUND: %s", title)
                        
                        # استخراج اسم البطولة
                        tournament_info = str(match.get("championshipName") or match.get("tournamentName") or match.get("tournament") or "").strip()
                        
                        self.notifier.alert_ticket_available(title, tournament_info)
                        self.seen_matches.add(match_key)

                self.latest_detected_titles = current_titles

            except Exception as exc:
                log.error("API error: %s", exc)

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.5, self.cfg.check_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass


async def _amain() -> None:
    CONFIG.validate()
    
    # تشغيل خادم الويب على بورت 8080 في خلفية منفصلة
    port = int(os.getenv("PORT", 8080))
    web_thread = threading.Thread(target=start_web_server, args=(port,), daemon=True)
    web_thread.start()
    log.info("Keep-alive Web server listening on port %s", port)

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
