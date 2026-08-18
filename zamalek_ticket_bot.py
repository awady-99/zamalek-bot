#!/usr/bin/env python3
"""
zamalek_ticket_bot.py
======================
Silent Ticket Monitor for Zamalek SC matches on Tazkarti.
- Alerts ONLY when tickets become available.
- Responds on-demand to /ping or /status.
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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from curl_cffi import requests as curl_requests
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False

try:
    from playwright.async_api import async_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [item.strip() for item in val.split(",") if item.strip()]


@dataclass
class Config:
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")
    tazkarti_url: str = _env_str("TAZKARTI_URL", "https://www.tazkarti.com/ar/events")
    team_keywords: list[str] = field(
        default_factory=lambda: _env_list("TEAM_KEYWORDS", ["Zamalek", "zamalek", "الزمالك", "Zamalek SC", "zamalek sc", "نادي الزمالك"])
    )
    available_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "AVAILABLE_KEYWORDS", [
                "Book Now", "Buy Now", "Available", "Book Ticket", "book ticket",
                "احجز الان", "احجز الآن", "متاح", "حجز تذكرة", "حجز تذكره"
            ]
        )
    )
    sold_out_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "SOLD_OUT_KEYWORDS", ["Sold Out", "Coming Soon", "Not Available", "نفذت الكمية", "قريبا"]
        )
    )

    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 1.0)
    jitter_seconds: float = _env_float("JITTER_SECONDS", 4.0)
    request_timeout_seconds: float = _env_float("REQUEST_TIMEOUT_SECONDS", 20.0)
    max_retries: int = _env_int("MAX_RETRIES", 4)
    backoff_base_seconds: float = _env_float("BACKOFF_BASE_SECONDS", 2.0)
    max_backoff_seconds: float = _env_float("MAX_BACKOFF_SECONDS", 300.0)
    consecutive_failures_before_slowdown: int = _env_int("CONSECUTIVE_FAILURES_BEFORE_SLOWDOWN", 5)
    slowdown_multiplier: float = _env_float("SLOWDOWN_MULTIPLIER", 3.0)

    use_playwright: bool = _env_bool("USE_PLAYWRIGHT", True)
    playwright_headless: bool = _env_bool("PLAYWRIGHT_HEADLESS", True)
    curl_impersonate: str = _env_str("CURL_IMPERSONATE", "chrome124")

    selector_match_card: str = _env_str("SELECTOR_MATCH_CARD", "[class*='event-card'], [class*='match-card'], .card")
    selector_title: str = _env_str("SELECTOR_TITLE", "[class*='title'], h2, h3")
    selector_datetime: str = _env_str("SELECTOR_DATETIME", "[class*='date'], time")
    selector_status: str = _env_str("SELECTOR_STATUS", "[class*='status'], [class*='btn'], button, a[class*='book']")
    selector_link: str = _env_str("SELECTOR_LINK", "a")

    state_file: Path = Path(_env_str("STATE_FILE", "zamalek_bot_state.json"))
    log_file: Path = Path(_env_str("LOG_FILE", "zamalek_bot.log"))
    log_level: str = _env_str("LOG_LEVEL", "INFO")
    send_startup_message: bool = False  # معطل لتجنب الإزعاج
    proxy_url: str = _env_str("PROXY_URL", "")

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}.")


CONFIG = Config()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(cfg: Config) -> logging.Logger:
    logger = logging.getLogger("zamalek_bot")
    logger.setLevel(cfg.log_level.upper())
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


log = setup_logging(CONFIG)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class MatchInfo:
    title: str
    datetime_text: str
    status_text: str
    url: str

    @property
    def match_id(self) -> str:
        raw = f"{self.title.strip().lower()}|{self.datetime_text.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def is_target_team(self, keywords: list[str]) -> bool:
        haystack = self.title
        return any(kw.lower() in haystack.lower() for kw in keywords)

    def status_kind(self, available_kw: list[str], sold_out_kw: list[str]) -> str:
        text = self.status_text.lower()
        if any(kw.lower() in text for kw in available_kw):
            return "available"
        if any(kw.lower() in text for kw in sold_out_kw):
            return "sold_out"
        return "unknown"


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #

class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()
        self._playwright = None
        self._browser = None

    async def __aenter__(self) -> "Fetcher":
        if self.cfg.use_playwright and HAVE_PLAYWRIGHT:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.cfg.playwright_headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def fetch_html(self, url: str) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                if HAVE_CURL_CFFI:
                    html = await asyncio.to_thread(self._fetch_curl_cffi, url)
                    if html and len(html) > 500:
                        return html
                if self.cfg.use_playwright and HAVE_PLAYWRIGHT and self._browser:
                    html = await self._fetch_playwright(url)
                    if html:
                        return html
                html = await asyncio.to_thread(self._fetch_plain_requests, url)
                if html:
                    return html
                raise FetchError("Empty content returned")
            except Exception as exc:
                last_exc = exc
                delay = min(self.cfg.backoff_base_seconds * (2 ** (attempt - 1)), self.cfg.max_backoff_seconds)
                await asyncio.sleep(delay)
        raise FetchError(f"Failed to fetch {url}") from last_exc

    def _fetch_curl_cffi(self, url: str) -> str:
        resp = curl_requests.get(url, headers=build_headers(), impersonate=self.cfg.curl_impersonate, timeout=self.cfg.request_timeout_seconds)
        return resp.text

    def _fetch_plain_requests(self, url: str) -> str:
        resp = self._session.get(url, headers=build_headers(), timeout=self.cfg.request_timeout_seconds)
        return resp.text

    async def _fetch_playwright(self, url: str) -> str:
        context = await self._browser.new_context(user_agent=random.choice(USER_AGENTS))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=int(self.cfg.request_timeout_seconds * 1000))
            await page.wait_for_timeout(1000)
            return await page.content()
        finally:
            await context.close()


# --------------------------------------------------------------------------- #
# Parser & State
# --------------------------------------------------------------------------- #

class Parser:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def parse(self, html: str, base_url: str) -> list[MatchInfo]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(self.cfg.selector_match_card)
        matches: list[MatchInfo] = []
        for card in cards:
            title_el = card.select_one(self.cfg.selector_title)
            if not title_el:
                continue
            title = re.sub(r"\s+", " ", title_el.get_text(strip=True))
            dt_el = card.select_one(self.cfg.selector_datetime)
            dt_text = re.sub(r"\s+", " ", dt_el.get_text(strip=True)) if dt_el else ""
            status_el = card.select_one(self.cfg.selector_status)
            status_text = re.sub(r"\s+", " ", status_el.get_text(strip=True)) if status_el else ""
            link_el = card.select_one(self.cfg.selector_link)
            link = urljoin(base_url, link_el["href"]) if link_el and link_el.has_attr("href") else base_url

            matches.append(MatchInfo(title=title, datetime_text=dt_text, status_text=status_text, url=link))
        return matches


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._state: dict = {}
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.error("Save state error: %s", e)

    def get_last_status(self, match_id: str) -> Optional[str]:
        return self._state.get(match_id, {}).get("status")

    def update(self, match: MatchInfo, status_kind: str) -> None:
        self._state[match.match_id] = {
            "title": match.title,
            "datetime_text": match.datetime_text,
            "status": status_kind,
            "url": match.url,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }


# --------------------------------------------------------------------------- #
# Telegram Notifier
# --------------------------------------------------------------------------- #

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

    def alert_ticket_available(self, match: MatchInfo) -> bool:
        text = (
            "🚨 <b>تذاكر الزمالك نزلت الآن!</b> 🚨\n\n"
            f"🏟️ <b>{match.title}</b>\n"
            f"🗓️ {match.datetime_text or 'الموعد لم يحدد'}\n"
            f"🎟️ الحالة: {match.status_text or 'احجز الآن'}\n\n"
            f"👉 <a href=\"{match.url}\">اضغط هنا للحجز من موقع تذكرتي</a>"
        )
        return self.send(text)


# --------------------------------------------------------------------------- #
# Monitor & Silent Listener
# --------------------------------------------------------------------------- #

class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.parser = Parser(cfg)
        self.state = StateStore(cfg.state_file)
        self.notifier = TelegramNotifier(cfg)
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        self.total_polls = 0
        self.start_time = time.time()
        self.last_poll_time = "جارٍ الفحص الأول..."

    def request_stop(self) -> None:
        self._stop.set()

    async def _telegram_listener_loop(self) -> None:
        """Polls Telegram updates strictly for manual /ping commands."""
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
                    data = resp.json()
                    for item in data.get("result", []):
                        offset = item["update_id"] + 1
                        msg = item.get("message", {})
                        text = msg.get("text", "").strip().lower()
                        sender_id = str(msg.get("chat", {}).get("id", ""))

                        if sender_id == str(self.cfg.telegram_chat_id) and text in ("/ping", "/status", "ping", "status"):
                            uptime_sec = int(time.time() - self.start_time)
                            m, s = divmod(uptime_sec, 60)
                            h, m = divmod(m, 60)
                            
                            status_text = (
                                "🟢 <b>البوت يعمل بنجاح ويراقب الموقع:</b>\n\n"
                                f"⏱️ <b>مدة العمل:</b> {h} ساعة و {m} دقيقة\n"
                                f"🔄 <b>مرات الفحص:</b> {self.total_polls}\n"
                                f"🕒 <b>آخر فحص:</b> {self.last_poll_time}"
                            )
                            self.notifier.send(status_text)
            except Exception as e:
                log.debug("Telegram command polling error: %s", e)

            await asyncio.sleep(2)

    async def run(self) -> None:
        log.info("Silent monitoring started.")
        asyncio.create_task(self._telegram_listener_loop())

        async with Fetcher(self.cfg) as fetcher:
            while not self._stop.is_set():
                cycle_start = time.monotonic()
                try:
                    await self._poll_once(fetcher)
                    self._consecutive_failures = 0
                    self.total_polls += 1
                    self.last_poll_time = datetime.now().strftime("%I:%M:%S %p")
                except Exception as exc:
                    self._consecutive_failures += 1
                    log.exception("Poll failed: %s", exc)

                elapsed = time.monotonic() - cycle_start
                sleep_for = max(0.5, self.cfg.check_interval_seconds - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

    async def _poll_once(self, fetcher: Fetcher) -> None:
        html = await fetcher.fetch_html(self.cfg.tazkarti_url)
        matches = self.parser.parse(html, self.cfg.tazkarti_url)
        zamalek = [m for m in matches if m.is_target_team(self.cfg.team_keywords)]

        for match in zamalek:
            kind = match.status_kind(self.cfg.available_keywords, self.cfg.sold_out_keywords)
            prev = self.state.get_last_status(match.match_id)
            if kind == "available" and prev != "available":
                self.notifier.alert_ticket_available(match)
            self.state.update(match, kind)
        self.state.save()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

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


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
