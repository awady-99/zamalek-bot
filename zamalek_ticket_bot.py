#!/usr/bin/env python3
"""
zamalek_ticket_bot.py
======================

Production-ready monitor that watches Tazkarti (https://www.tazkarti.com) for
Zamalek SC football matches and sends an instant Telegram alert the moment a
match's ticket status flips to "Available" / "Book Now".

Now includes Telegram command support (/ping, /status) to monitor bot health.
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
        default_factory=lambda: _env_list("TEAM_KEYWORDS", ["Zamalek", "zamalek", "الزمالك"])
    )
    available_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "AVAILABLE_KEYWORDS", ["Book Now", "Buy Now", "Available", "احجز الان", "احجز الآن", "متاح"]
        )
    )
    sold_out_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "SOLD_OUT_KEYWORDS", ["Sold Out", "Coming Soon", "Not Available", "نفذت الكمية", "قريبا"]
        )
    )

    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 20.0)
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
    send_startup_message: bool = _env_bool("SEND_STARTUP_MESSAGE", True)
    proxy_url: str = _env_str("PROXY_URL", "")

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise SystemExit(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"Copy .env.example to .env and fill them in."
            )


CONFIG = Config()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(cfg: Config) -> logging.Logger:
    logger = logging.getLogger("zamalek_bot")
    logger.setLevel(cfg.log_level.upper())

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        cfg.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

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


# --------------------------------------------------------------------------- #
# Anti-bot: rotating User-Agents + realistic headers
# --------------------------------------------------------------------------- #

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
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
                    log.warning(
                        "curl_cffi response looked too small (%d chars); falling back to Playwright.",
                        len(html or ""),
                    )
                if self.cfg.use_playwright and HAVE_PLAYWRIGHT and self._browser:
                    html = await self._fetch_playwright(url)
                    if html:
                        return html
                html = await asyncio.to_thread(self._fetch_plain_requests, url)
                if html:
                    return html
                raise FetchError("All fetch strategies returned empty content")
            except Exception as exc:
                last_exc = exc
                delay = min(
                    self.cfg.backoff_base_seconds * (2 ** (attempt - 1)),
                    self.cfg.max_backoff_seconds,
                ) + random.uniform(0, 1.5)
                log.warning(
                    "Fetch attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempt,
                    self.cfg.max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise FetchError(f"Failed to fetch {url} after {self.cfg.max_retries} attempts") from last_exc

    def _proxies(self) -> Optional[dict]:
        if self.cfg.proxy_url:
            return {"http": self.cfg.proxy_url, "https": self.cfg.proxy_url}
        return None

    def _fetch_curl_cffi(self, url: str) -> str:
        resp = curl_requests.get(
            url,
            headers=build_headers(),
            impersonate=self.cfg.curl_impersonate,
            timeout=self.cfg.request_timeout_seconds,
            proxies=self._proxies(),
        )
        self._raise_for_status(resp.status_code, url)
        return resp.text

    def _fetch_plain_requests(self, url: str) -> str:
        resp = self._session.get(
            url,
            headers=build_headers(),
            timeout=self.cfg.request_timeout_seconds,
            proxies=self._proxies(),
        )
        self._raise_for_status(resp.status_code, url)
        return resp.text

    async def _fetch_playwright(self, url: str) -> str:
        context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ar-EG",
            timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 900},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        try:
            await page.goto(
                url, wait_until="networkidle", timeout=int(self.cfg.request_timeout_seconds * 1000)
            )
            await page.wait_for_timeout(1500)
            html = await page.content()
            return html
        finally:
            await context.close()

    @staticmethod
    def _raise_for_status(status_code: int, url: str) -> None:
        if status_code == 429:
            raise FetchError(f"Rate limited (429) fetching {url}")
        if status_code in (403, 503):
            raise FetchError(f"Blocked/Cloudflare challenge ({status_code}) fetching {url}")
        if status_code >= 400:
            raise FetchError(f"HTTP {status_code} fetching {url}")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

class Parser:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def parse(self, html: str, base_url: str) -> list[MatchInfo]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(self.cfg.selector_match_card)
        matches: list[MatchInfo] = []

        for card in cards:
            title = self._text(card, self.cfg.selector_title)
            dt_text = self._text(card, self.cfg.selector_datetime)
            status_text = self._text(card, self.cfg.selector_status)
            link = self._href(card, self.cfg.selector_link, base_url)

            if not title:
                continue

            matches.append(
                MatchInfo(
                    title=title,
                    datetime_text=dt_text,
                    status_text=status_text,
                    url=link or base_url,
                )
            )

        return matches

    @staticmethod
    def _text(node, selector: str) -> str:
        el = node.select_one(selector)
        if not el:
            return ""
        return re.sub(r"\s+", " ", el.get_text(strip=True))

    @staticmethod
    def _href(node, selector: str, base_url: str) -> str:
        el = node.select_one(selector)
        if not el:
            if node.name == "a" and node.has_attr("href"):
                return urljoin(base_url, node["href"])
            return ""
        href = el.get("href", "")
        return urljoin(base_url, href) if href else ""


# --------------------------------------------------------------------------- #
# State store
# --------------------------------------------------------------------------- #

class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._state: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state file (%s); starting fresh.", exc)
                self._state = {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.error("Could not write state file: %s", exc)

    def get_last_status(self, match_id: str) -> Optional[str]:
        entry = self._state.get(match_id)
        return entry.get("status") if entry else None

    def update(self, match: MatchInfo, status_kind: str) -> None:
        self._state[match.match_id] = {
            "title": match.title,
            "datetime_text": match.datetime_text,
            "status": status_kind,
            "url": match.url,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }


# --------------------------------------------------------------------------- #
# Telegram notifier & listener
# --------------------------------------------------------------------------- #

class TelegramNotifier:
    API_BASE = "https://api.telegram.org"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()

    def send(self, text: str, disable_preview: bool = False) -> bool:
        url = f"{self.API_BASE}/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        for attempt in range(1, 4):
            try:
                resp = self._session.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    return True
                log.error("Telegram API returned %s: %s", resp.status_code, resp.text[:300])
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(retry_after + 1)
                    continue
            except requests.RequestException as exc:
                log.error("Telegram send attempt %d failed: %s", attempt, exc)
            time.sleep(2 * attempt)
        return False

    def alert_ticket_available(self, match: MatchInfo) -> bool:
        text = (
            "🚨 <b>ZAMALEK TICKETS AVAILABLE!</b> 🚨\n\n"
            f"🏟️ <b>{_escape_html(match.title)}</b>\n"
            f"🗓️ {_escape_html(match.datetime_text) or 'Date TBA'}\n"
            f"🎟️ Status: {_escape_html(match.status_text) or 'Available'}\n\n"
            f"👉 <a href=\"{_escape_html(match.url)}\">Book now on Tazkarti</a>\n\n"
            f"<i>Detected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        return self.send(text)

    def notify_startup(self) -> None:
        if self.cfg.send_startup_message:
            self.send(
                "✅ <b>Zamalek Tazkarti ticket monitor started.</b>\n"
                f"Watching: {_escape_html(self.cfg.tazkarti_url)}\n"
                f"Interval: ~{self.cfg.check_interval_seconds:.0f}s\n\n"
                "💡 <i>Send /ping or /status anytime to check bot health!</i>"
            )

    def notify_error(self, message: str) -> None:
        self.send(f"⚠️ Zamalek ticket bot warning:\n{_escape_html(message)}")


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# Orchestrator
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
        self.last_poll_time: Optional[datetime] = None
        self.start_time = datetime.now()

    def request_stop(self) -> None:
        self._stop.set()

    async def _listen_telegram_commands(self) -> None:
        """Listens for /ping and /status commands without interfering with polling."""
        offset = 0
        url = f"{TelegramNotifier.API_BASE}/bot{self.cfg.telegram_bot_token}/getUpdates"
        
        while not self._stop.is_set():
            try:
                params = {"offset": offset, "timeout": 20}
                resp = await asyncio.to_thread(requests.get, url, params=params, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))

                        if chat_id == str(self.cfg.telegram_chat_id) and text in ("/ping", "/status", "ping", "status"):
                            uptime_sec = int((datetime.now() - self.start_time).total_seconds())
                            hours, rem = divmod(uptime_sec, 3600)
                            minutes, seconds = divmod(rem, 60)
                            uptime_str = f"{hours}h {minutes}m {seconds}s"

                            last_seen_str = (
                                self.last_poll_time.strftime("%H:%M:%S")
                                if self.last_poll_time else "Just started"
                            )
                            
                            status_msg = (
                                "🟢 <b>Bot Status: Online & Running!</b>\n\n"
                                f"⏱️ <b>Uptime:</b> {uptime_str}\n"
                                f"🔄 <b>Total Polls:</b> {self.total_polls}\n"
                                f"🕒 <b>Last Check:</b> {last_seen_str}\n"
                                f"⚠️ <b>Failures:</b> {self._consecutive_failures}\n"
                                f"🎯 <b>Target:</b> Tazkarti Events"
                            )
                            self.notifier.send(status_msg)
            except Exception as e:
                log.debug("Telegram polling listener error (harmless): %s", e)
            
            await asyncio.sleep(2)

    async def run(self) -> None:
        self.notifier.notify_startup()
        log.info("Monitoring loop starting. Target: %s", self.cfg.tazkarti_url)

        # Start the telegram listener task in background
        command_task = asyncio.create_task(self._listen_telegram_commands())

        async with Fetcher(self.cfg) as fetcher:
            while not self._stop.is_set():
                cycle_start = time.monotonic()
                try:
                    await self._poll_once(fetcher)
                    self._consecutive_failures = 0
                    self.total_polls += 1
                    self.last_poll_time = datetime.now()
                except Exception as exc:
                    self._consecutive_failures += 1
                    log.exception("Poll cycle failed: %s", exc)
                    if self._consecutive_failures == self.cfg.consecutive_failures_before_slowdown:
                        self.notifier.notify_error(
                            f"{self._consecutive_failures} consecutive failures. "
                            "Slowing down polling to avoid a ban."
                        )

                await self._sleep_until_next_cycle(cycle_start)

        command_task.cancel()
        log.info("Monitoring loop stopped cleanly.")

    async def _poll_once(self, fetcher: Fetcher) -> None:
        html = await fetcher.fetch_html(self.cfg.tazkarti_url)
        all_matches = self.parser.parse(html, self.cfg.tazkarti_url)
        zamalek_matches = [
            m for m in all_matches if m.is_target_team(self.cfg.team_keywords)
        ]

        log.info(
            "Polled OK: %d card(s) parsed, %d Zamalek match(es) found.",
            len(all_matches),
            len(zamalek_matches),
        )

        for match in zamalek_matches:
            kind = match.status_kind(
                self.cfg.available_keywords, self.cfg.sold_out_keywords
            )
            previous = self.state.get_last_status(match.match_id)

            log.info(
                "  - %s | %s | status=%s (was %s)",
                match.title,
                match.datetime_text,
                kind,
                previous,
            )

            if kind == "available" and previous != "available":
                log.warning("STATUS CHANGE -> AVAILABLE: %s", match.title)
                sent = self.notifier.alert_ticket_available(match)
                if not sent:
                    log.error(
                        "Failed to deliver Telegram alert for %s; will retry next cycle.",
                        match.title,
                    )
                    continue

            self.state.update(match, kind)

        self.state.save()

    async def _sleep_until_next_cycle(self, cycle_start: float) -> None:
        elapsed = time.monotonic() - cycle_start
        base_interval = self.cfg.check_interval_seconds

        if self._consecutive_failures >= self.cfg.consecutive_failures_before_slowdown:
            base_interval *= self.cfg.slowdown_multiplier

        jitter = random.uniform(0, self.cfg.jitter_seconds)
        sleep_for = max(0.5, base_interval + jitter - elapsed)

        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- #
# Entry point
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
        log.info("Interrupted by user. Bye.")


if __name__ == "__main__":
    main()     badge, and its "Book Now" button/link.
  3. Copy the appropriate CSS selectors into your .env file (see
     .env.example). Sensible best-guess defaults are provided, but ticketing
     sites change their markup often.

See SETUP.md for the full walkthrough.
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
    from curl_cffi import requests as curl_requests  # browser-impersonating HTTP client
    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_CURL_CFFI = False

try:
    from playwright.async_api import async_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - optional dependency
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
    # --- Telegram ---
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")

    # --- Target ---
    tazkarti_url: str = _env_str(
        "TAZKARTI_URL", "https://www.tazkarti.com/ar/events"
    )
    team_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "TEAM_KEYWORDS", ["Zamalek", "zamalek", "الزمالك"]
        )
    )
    available_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "AVAILABLE_KEYWORDS",
            ["Book Now", "Buy Now", "Available", "احجز الان", "احجز الآن", "متاح"],
        )
    )
    sold_out_keywords: list[str] = field(
        default_factory=lambda: _env_list(
            "SOLD_OUT_KEYWORDS",
            ["Sold Out", "Coming Soon", "Not Available", "نفذت الكمية", "قريبا"],
        )
    )

    # --- Polling ---
    check_interval_seconds: float = _env_float("CHECK_INTERVAL_SECONDS", 20.0)
    jitter_seconds: float = _env_float("JITTER_SECONDS", 4.0)
    request_timeout_seconds: float = _env_float("REQUEST_TIMEOUT_SECONDS", 20.0)
    max_retries: int = _env_int("MAX_RETRIES", 4)
    backoff_base_seconds: float = _env_float("BACKOFF_BASE_SECONDS", 2.0)
    max_backoff_seconds: float = _env_float("MAX_BACKOFF_SECONDS", 300.0)
    consecutive_failures_before_slowdown: int = _env_int(
        "CONSECUTIVE_FAILURES_BEFORE_SLOWDOWN", 5
    )
    slowdown_multiplier: float = _env_float("SLOWDOWN_MULTIPLIER", 3.0)

    # --- Scraping engine ---
    use_playwright: bool = _env_bool("USE_PLAYWRIGHT", True)
    playwright_headless: bool = _env_bool("PLAYWRIGHT_HEADLESS", True)
    curl_impersonate: str = _env_str("CURL_IMPERSONATE", "chrome124")

    # --- CSS selectors (ADAPT THESE to Tazkarti's real markup, see SETUP.md) ---
    selector_match_card: str = _env_str(
        "SELECTOR_MATCH_CARD", "[class*='event-card'], [class*='match-card'], .card"
    )
    selector_title: str = _env_str(
        "SELECTOR_TITLE", "[class*='title'], h2, h3"
    )
    selector_datetime: str = _env_str(
        "SELECTOR_DATETIME", "[class*='date'], time"
    )
    selector_status: str = _env_str(
        "SELECTOR_STATUS", "[class*='status'], [class*='btn'], button, a[class*='book']"
    )
    selector_link: str = _env_str("SELECTOR_LINK", "a")

    # --- Storage / logging ---
    state_file: Path = Path(_env_str("STATE_FILE", "zamalek_bot_state.json"))
    log_file: Path = Path(_env_str("LOG_FILE", "zamalek_bot.log"))
    log_level: str = _env_str("LOG_LEVEL", "INFO")

    # --- Misc ---
    send_startup_message: bool = _env_bool("SEND_STARTUP_MESSAGE", True)
    proxy_url: str = _env_str("PROXY_URL", "")

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise SystemExit(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"Copy .env.example to .env and fill them in."
            )


CONFIG = Config()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(cfg: Config) -> logging.Logger:
    logger = logging.getLogger("zamalek_bot")
    logger.setLevel(cfg.log_level.upper())

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        cfg.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

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


# --------------------------------------------------------------------------- #
# Anti-bot: rotating User-Agents + realistic headers
# --------------------------------------------------------------------------- #

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
    }


# --------------------------------------------------------------------------- #
# Fetcher: resilient HTTP retrieval with retries + backoff + fallback engines
# --------------------------------------------------------------------------- #

class FetchError(Exception):
    pass


class Fetcher:
    """
    Tries, in order:
      1. curl_cffi with a real Chrome TLS/JA3 fingerprint (best defense
         against Cloudflare's TLS fingerprinting, cheap and fast).
      2. Playwright headless Chromium (executes JavaScript; needed if
         ticket status is injected client-side after an XHR call).
      3. Plain `requests` as a last-resort fallback.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()
        self._playwright_ctx = None
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
                    log.warning(
                        "curl_cffi response looked too small (%d chars); "
                        "falling back to Playwright.",
                        len(html or ""),
                    )
                if self.cfg.use_playwright and HAVE_PLAYWRIGHT and self._browser:
                    html = await self._fetch_playwright(url)
                    if html:
                        return html
                # last resort
                html = await asyncio.to_thread(self._fetch_plain_requests, url)
                if html:
                    return html
                raise FetchError("All fetch strategies returned empty content")
            except Exception as exc:  # noqa: BLE001 - we want to retry broadly
                last_exc = exc
                delay = min(
                    self.cfg.backoff_base_seconds * (2 ** (attempt - 1)),
                    self.cfg.max_backoff_seconds,
                ) + random.uniform(0, 1.5)
                log.warning(
                    "Fetch attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempt,
                    self.cfg.max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise FetchError(f"Failed to fetch {url} after {self.cfg.max_retries} attempts") from last_exc

    def _proxies(self) -> Optional[dict]:
        if self.cfg.proxy_url:
            return {"http": self.cfg.proxy_url, "https": self.cfg.proxy_url}
        return None

    def _fetch_curl_cffi(self, url: str) -> str:
        resp = curl_requests.get(
            url,
            headers=build_headers(),
            impersonate=self.cfg.curl_impersonate,
            timeout=self.cfg.request_timeout_seconds,
            proxies=self._proxies(),
        )
        self._raise_for_status(resp.status_code, url)
        return resp.text

    def _fetch_plain_requests(self, url: str) -> str:
        resp = self._session.get(
            url,
            headers=build_headers(),
            timeout=self.cfg.request_timeout_seconds,
            proxies=self._proxies(),
        )
        self._raise_for_status(resp.status_code, url)
        return resp.text

    async def _fetch_playwright(self, url: str) -> str:
        context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ar-EG",
            timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 900},
        )
        # Light stealth: hide the webdriver flag.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        try:
            await page.goto(
                url, wait_until="networkidle", timeout=int(self.cfg.request_timeout_seconds * 1000)
            )
            # Give any lazy-loaded ticket-status widgets a moment to render.
            await page.wait_for_timeout(1500)
            html = await page.content()
            return html
        finally:
            await context.close()

    @staticmethod
    def _raise_for_status(status_code: int, url: str) -> None:
        if status_code == 429:
            raise FetchError(f"Rate limited (429) fetching {url}")
        if status_code in (403, 503):
            raise FetchError(f"Blocked/Cloudflare challenge ({status_code}) fetching {url}")
        if status_code >= 400:
            raise FetchError(f"HTTP {status_code} fetching {url}")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

class Parser:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def parse(self, html: str, base_url: str) -> list[MatchInfo]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(self.cfg.selector_match_card)
        matches: list[MatchInfo] = []

        for card in cards:
            title = self._text(card, self.cfg.selector_title)
            dt_text = self._text(card, self.cfg.selector_datetime)
            status_text = self._text(card, self.cfg.selector_status)
            link = self._href(card, self.cfg.selector_link, base_url)

            if not title:
                continue

            matches.append(
                MatchInfo(
                    title=title,
                    datetime_text=dt_text,
                    status_text=status_text,
                    url=link or base_url,
                )
            )

        if not matches:
            log.debug(
                "Parser found 0 match cards using selector %r. "
                "The site markup likely differs — inspect the page and "
                "update SELECTOR_* values in .env.",
                self.cfg.selector_match_card,
            )

        return matches

    @staticmethod
    def _text(node, selector: str) -> str:
        el = node.select_one(selector)
        if not el:
            return ""
        return re.sub(r"\s+", " ", el.get_text(strip=True))

    @staticmethod
    def _href(node, selector: str, base_url: str) -> str:
        el = node.select_one(selector)
        if not el:
            # the card itself might be the <a>
            if node.name == "a" and node.has_attr("href"):
                return urljoin(base_url, node["href"])
            return ""
        href = el.get("href", "")
        return urljoin(base_url, href) if href else ""


# --------------------------------------------------------------------------- #
# State store (persists last-known status per match across restarts)
# --------------------------------------------------------------------------- #

class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._state: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state file (%s); starting fresh.", exc)
                self._state = {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.error("Could not write state file: %s", exc)

    def get_last_status(self, match_id: str) -> Optional[str]:
        entry = self._state.get(match_id)
        return entry.get("status") if entry else None

    def update(self, match: MatchInfo, status_kind: str) -> None:
        self._state[match.match_id] = {
            "title": match.title,
            "datetime_text": match.datetime_text,
            "status": status_kind,
            "url": match.url,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }


# --------------------------------------------------------------------------- #
# Telegram notifier
# --------------------------------------------------------------------------- #

class TelegramNotifier:
    API_BASE = "https://api.telegram.org"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._session = requests.Session()

    def send(self, text: str, disable_preview: bool = False) -> bool:
        url = f"{self.API_BASE}/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        for attempt in range(1, 4):
            try:
                resp = self._session.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    return True
                log.error(
                    "Telegram API returned %s: %s", resp.status_code, resp.text[:300]
                )
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(retry_after + 1)
                    continue
            except requests.RequestException as exc:
                log.error("Telegram send attempt %d failed: %s", attempt, exc)
            time.sleep(2 * attempt)
        return False

    def alert_ticket_available(self, match: MatchInfo) -> bool:
        text = (
            "🚨 <b>ZAMALEK TICKETS AVAILABLE!</b> 🚨\n\n"
            f"🏟️ <b>{_escape_html(match.title)}</b>\n"
            f"🗓️ {_escape_html(match.datetime_text) or 'Date TBA'}\n"
            f"🎟️ Status: {_escape_html(match.status_text) or 'Available'}\n\n"
            f"👉 <a href=\"{_escape_html(match.url)}\">Book now on Tazkarti</a>\n\n"
            f"<i>Detected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        return self.send(text)

    def notify_startup(self) -> None:
        if self.cfg.send_startup_message:
            self.send(
                "✅ Zamalek Tazkarti ticket monitor started.\n"
                f"Watching: {_escape_html(self.cfg.tazkarti_url)}\n"
                f"Interval: ~{self.cfg.check_interval_seconds:.0f}s"
            )

    def notify_error(self, message: str) -> None:
        self.send(f"⚠️ Zamalek ticket bot warning:\n{_escape_html(message)}")


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.parser = Parser(cfg)
        self.state = StateStore(cfg.state_file)
        self.notifier = TelegramNotifier(cfg)
        self._stop = asyncio.Event()
        self._consecutive_failures = 0

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.notifier.notify_startup()
        log.info("Monitoring loop starting. Target: %s", self.cfg.tazkarti_url)

        async with Fetcher(self.cfg) as fetcher:
            while not self._stop.is_set():
                cycle_start = time.monotonic()
                try:
                    await self._poll_once(fetcher)
                    self._consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001
                    self._consecutive_failures += 1
                    log.exception("Poll cycle failed: %s", exc)
                    if self._consecutive_failures == self.cfg.consecutive_failures_before_slowdown:
                        self.notifier.notify_error(
                            f"{self._consecutive_failures} consecutive failures. "
                            "Slowing down polling to avoid a ban."
                        )

                await self._sleep_until_next_cycle(cycle_start)

        log.info("Monitoring loop stopped cleanly.")

    async def _poll_once(self, fetcher: Fetcher) -> None:
        html = await fetcher.fetch_html(self.cfg.tazkarti_url)
        all_matches = self.parser.parse(html, self.cfg.tazkarti_url)
        zamalek_matches = [
            m for m in all_matches if m.is_target_team(self.cfg.team_keywords)
        ]

        log.info(
            "Polled OK: %d card(s) parsed, %d Zamalek match(es) found.",
            len(all_matches),
            len(zamalek_matches),
        )

        for match in zamalek_matches:
            kind = match.status_kind(
                self.cfg.available_keywords, self.cfg.sold_out_keywords
            )
            previous = self.state.get_last_status(match.match_id)

            log.info(
                "  - %s | %s | status=%s (was %s)",
                match.title,
                match.datetime_text,
                kind,
                previous,
            )

            if kind == "available" and previous != "available":
                log.warning("STATUS CHANGE -> AVAILABLE: %s", match.title)
                sent = self.notifier.alert_ticket_available(match)
                if not sent:
                    log.error(
                        "Failed to deliver Telegram alert for %s; will retry next cycle "
                        "(state not updated so we try again).",
                        match.title,
                    )
                    continue  # don't persist state; retry alert next loop

            self.state.update(match, kind)

        self.state.save()

    async def _sleep_until_next_cycle(self, cycle_start: float) -> None:
        elapsed = time.monotonic() - cycle_start
        base_interval = self.cfg.check_interval_seconds

        if self._consecutive_failures >= self.cfg.consecutive_failures_before_slowdown:
            base_interval *= self.cfg.slowdown_multiplier

        jitter = random.uniform(0, self.cfg.jitter_seconds)
        sleep_for = max(0.5, base_interval + jitter - elapsed)

        try:
            await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass  # normal case: timeout means "time for next poll"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def _amain() -> None:
    CONFIG.validate()

    if CONFIG.use_playwright and not HAVE_PLAYWRIGHT:
        log.warning(
            "USE_PLAYWRIGHT=true but the 'playwright' package is not installed. "
            "Falling back to curl_cffi/requests only. Run: "
            "pip install playwright && playwright install chromium"
        )
    if not HAVE_CURL_CFFI:
        log.warning(
            "curl_cffi is not installed; falling back to plain 'requests', which is "
            "more likely to be blocked by Cloudflare. Run: pip install curl_cffi"
        )

    monitor = Monitor(CONFIG)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, monitor.request_stop)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows for SIGTERM etc.
            pass

    await monitor.run()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Interrupted by user. Bye.")


if __name__ == "__main__":
    main()
