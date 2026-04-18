import json
import logging
import logging.handlers
import random
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.vinted.de"
API_URL = f"{BASE_URL}/api/v2/catalog/items"


@dataclass
class KeywordConfig:
    keyword: str
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None


@dataclass
class AppConfig:
    sleep: float
    timeout: int
    per_page: int
    warmup_cycles: int
    confirm_seen_count: int
    status_every: int
    retries_total: int
    backoff_factor: float
    log_file: str
    log_level: str
    discord_username: str
    discord_avatar_url: str
    discord_webhooks: List[str]
    headers: Dict[str, str]
    keywords: List[KeywordConfig]


class PrettyConsoleFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[0;37m",
        logging.INFO: "\033[1;36m",
        logging.WARNING: "\033[1;33m",
        logging.ERROR: "\033[1;31m",
        logging.CRITICAL: "\033[1;41m",
    }
    ICONS = {
        "system": "●",
        "startup": "●",
        "warmup": "◔",
        "watch": "∙",
        "new": "✓",
        "discord": "✓",
        "error": "✕",
    }
    RESET = "\033[0m"
    MAX_MESSAGE_LENGTH = 160

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        keyword = getattr(record, "keyword", "SYSTEM")
        event = getattr(record, "event", "").strip()
        message = record.getMessage().replace("\n", " ").strip()

        if len(message) > self.MAX_MESSAGE_LENGTH:
            message = message[: self.MAX_MESSAGE_LENGTH - 1] + "…"

        icon = self.ICONS.get(event, "•")
        color = self.COLORS.get(record.levelno, self.RESET)

        if event:
            line = f"{timestamp}  {icon}  {keyword:<14} {event:<8} {message}"
        else:
            line = f"{timestamp}  {icon}  {keyword:<14} {message}"

        return f"{color}{line}{self.RESET}"


class ContextFileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "keyword"):
            record.keyword = "SYSTEM"
        if not hasattr(record, "event"):
            record.event = "system"
        return super().format(record)


def setup_logger(log_file: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("vinted_monitor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    level_value = getattr(logging, level.upper(), logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level_value)
    console_handler.setFormatter(PrettyConsoleFormatter())

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level_value)
    file_handler.setFormatter(
        ContextFileFormatter(
            "%(asctime)s | %(levelname)s | %(threadName)s | [%(keyword)s] %(event)s | %(message)s",
            datefmt="%d-%m-%Y %H:%M:%S",
        )
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def log_event(logger: logging.Logger, level: int, keyword: str, event: str, message: str) -> None:
    logger.log(level, message, extra={"keyword": keyword, "event": event})


def parse_decimal(value: object) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None


def normalize_price_text(value: Optional[Decimal], currency: str = "EUR") -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} {currency}".replace(".00", ".00")


def random_string(length: int) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def create_session(headers: Dict[str, str], retries_total: int, backoff_factor: float) -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)

    retry = Retry(
        total=retries_total,
        connect=retries_total,
        read=retries_total,
        status=retries_total,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def load_settings(path: str = "settings.json") -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if "headers" not in raw:
        raise ValueError("Missing required setting: headers")
    if "keywords" not in raw or not isinstance(raw["keywords"], list) or not raw["keywords"]:
        raise ValueError("Missing or empty setting: keywords")

    keywords: List[KeywordConfig] = []
    for entry in raw["keywords"]:
        keyword = str(entry.get("keyword", "")).strip()
        if not keyword:
            continue

        min_price = parse_decimal(entry.get("min_price"))
        max_price = parse_decimal(entry.get("max_price"))

        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError(f"Invalid price range for keyword '{keyword}': min_price > max_price")

        keywords.append(
            KeywordConfig(
                keyword=keyword,
                min_price=min_price,
                max_price=max_price,
            )
        )

    if not keywords:
        raise ValueError("No valid keywords found in settings.json")

    return AppConfig(
        sleep=float(raw.get("sleep", 3.0)),
        timeout=int(raw.get("timeout", 15)),
        per_page=int(raw.get("per_page", 96)),
        warmup_cycles=int(raw.get("warmup_cycles", 8)),
        confirm_seen_count=int(raw.get("confirm_seen_count", 2)),
        status_every=max(1, int(raw.get("status_every", 5))),
        retries_total=int(raw.get("retries_total", 2)),
        backoff_factor=float(raw.get("backoff_factor", 0.5)),
        log_file=str(raw.get("log_file", "vinted_monitor.log")),
        log_level=str(raw.get("log_level", "INFO")),
        discord_username=str(raw.get("discord_username", "Vinted Monitor")),
        discord_avatar_url=str(raw.get("discord_avatar_url", "")),
        discord_webhooks=list(raw.get("discord_webhooks", [])),
        headers=dict(raw["headers"]),
        keywords=keywords,
    )


def extract_price_and_currency(item: dict) -> Tuple[Optional[Decimal], str]:
    price_obj = item.get("price")
    currency = str(item.get("currency", "EUR")).strip() or "EUR"

    if isinstance(price_obj, dict):
        amount = price_obj.get("amount")
        currency = str(price_obj.get("currency_code", currency)).strip() or currency
        return parse_decimal(amount), currency

    return parse_decimal(price_obj), currency


def parse_item(item: dict) -> Optional[dict]:
    try:
        item_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()

        photo = item.get("photo") or {}
        photo_url = str(photo.get("url", "")).strip()

        price_value, currency = extract_price_and_currency(item)

        if not title or not url:
            return None

        unique_key = item_id or f"{title}|{url}|{price_value}"

        return {
            "id": item_id,
            "key": unique_key,
            "title": title,
            "url": url,
            "photo_url": photo_url,
            "price_value": price_value,
            "currency": currency,
        }
    except Exception:
        return None


def item_matches_price(item: dict, keyword_cfg: KeywordConfig) -> bool:
    price_value: Optional[Decimal] = item.get("price_value")

    if price_value is None:
        return keyword_cfg.min_price is None and keyword_cfg.max_price is None

    if keyword_cfg.min_price is not None and price_value < keyword_cfg.min_price:
        return False
    if keyword_cfg.max_price is not None and price_value > keyword_cfg.max_price:
        return False
    return True


def fetch_items(session: requests.Session, keyword: str, timeout: int, per_page: int) -> List[dict]:
    params = {
        "page": 1,
        "per_page": per_page,
        "time": int(time.time()),
        "search_text": keyword,
        "catalog_ids": "",
        "size_ids": "",
        "brand_ids": "",
        "status_ids": "",
        "color_ids": "",
        "material_ids": "",
        random_string(10): random_string(10),
    }

    response = session.get(API_URL, params=params, timeout=timeout)
    response.raise_for_status()

    data = response.json()
    raw_items = data.get("items", [])

    result = []
    for raw_item in raw_items:
        parsed = parse_item(raw_item)
        if parsed:
            result.append(parsed)
    return result


def discord_message(
    logger: logging.Logger,
    product: str,
    link: str,
    price: str,
    product_img: str,
    keyword: str,
    webhook_links: List[str],
    username: str,
    avatar_url: str,
    timeout: int,
) -> None:
    if not webhook_links:
        log_event(logger, logging.INFO, keyword, "discord", "no webhooks configured")
        return

    embed = {
        "title": product[:256],
        "url": link,
        "description": f"**Keyword:** `{keyword}`\n**Price:** `{price}`",
        "color": 5763719,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Vinted Monitor"},
    }

    if product_img:
        embed["thumbnail"] = {"url": product_img}

    payload = {
        "username": username,
        "embeds": [embed],
    }

    if avatar_url:
        payload["avatar_url"] = avatar_url

    for webhook in webhook_links:
        try:
            response = requests.post(webhook, json=payload, timeout=timeout)
            if response.status_code in (200, 204):
                log_event(logger, logging.INFO, keyword, "discord", f"sent for '{product}'")
            else:
                log_event(
                    logger,
                    logging.ERROR,
                    keyword,
                    "discord",
                    f"status={response.status_code} body={response.text.strip()}",
                )
        except Exception as e:
            log_event(logger, logging.ERROR, keyword, "discord", f"send failed: {e}")


def price_filter_label(keyword_cfg: KeywordConfig) -> str:
    if keyword_cfg.min_price is None and keyword_cfg.max_price is None:
        return "none"
    if keyword_cfg.min_price is not None and keyword_cfg.max_price is not None:
        return f"{keyword_cfg.min_price:.2f}-{keyword_cfg.max_price:.2f} EUR"
    if keyword_cfg.min_price is not None:
        return f">= {keyword_cfg.min_price:.2f} EUR"
    return f"<= {keyword_cfg.max_price:.2f} EUR"


def monitor_keyword(cfg: AppConfig, keyword_cfg: KeywordConfig, logger: logging.Logger) -> None:
    keyword = keyword_cfg.keyword
    session = create_session(cfg.headers, cfg.retries_total, cfg.backoff_factor)

    seen_items: Set[str] = set()
    pending_hits: Dict[str, int] = {}
    cycle = 0
    watch_cycle = 0
    last_pending_count = 0

    try:
        session.get(BASE_URL, timeout=cfg.timeout)
    except Exception as e:
        log_event(logger, logging.WARNING, keyword, "startup", f"homepage request failed: {e}")

    log_event(
        logger,
        logging.INFO,
        keyword,
        "startup",
        f"started | warmup={cfg.warmup_cycles} confirm={cfg.confirm_seen_count} price_filter={price_filter_label(keyword_cfg)}",
    )

    while True:
        try:
            items = fetch_items(session, keyword, cfg.timeout, cfg.per_page)
            matching_items = [item for item in items if item_matches_price(item, keyword_cfg)]
            current_visible = len(items)
            current_matching = len(matching_items)
            cycle += 1

            if cycle <= cfg.warmup_cycles:
                added = 0
                for item in matching_items:
                    if item["key"] not in seen_items:
                        seen_items.add(item["key"])
                        added += 1

                should_log = (
                    cycle == 1
                    or cycle == cfg.warmup_cycles
                    or added > 0
                    or cycle % cfg.status_every == 0
                )

                if should_log:
                    log_event(
                        logger,
                        logging.INFO,
                        keyword,
                        "warmup",
                        f"{cycle}/{cfg.warmup_cycles}  vis:{current_visible}  match:{current_matching}  base:{len(seen_items)}  +{added}",
                    )
            else:
                watch_cycle += 1
                new_alerts = 0

                for item in matching_items:
                    key = item["key"]
                    if key in seen_items:
                        continue

                    pending_hits[key] = pending_hits.get(key, 0) + 1

                    if pending_hits[key] >= cfg.confirm_seen_count:
                        seen_items.add(key)
                        pending_hits.pop(key, None)
                        new_alerts += 1

                        price_display = normalize_price_text(item["price_value"], item["currency"])

                        log_event(
                            logger,
                            logging.INFO,
                            keyword,
                            "new",
                            f"{item['title']} | {price_display}",
                        )

                        discord_message(
                            logger=logger,
                            product=item["title"],
                            link=item["url"],
                            price=price_display,
                            product_img=item["photo_url"],
                            keyword=keyword,
                            webhook_links=cfg.discord_webhooks,
                            username=cfg.discord_username,
                            avatar_url=cfg.discord_avatar_url,
                            timeout=cfg.timeout,
                        )

                pending_count = len(pending_hits)
                should_log = (
                    new_alerts > 0
                    or pending_count != last_pending_count
                    or watch_cycle % cfg.status_every == 0
                )

                if should_log:
                    log_event(
                        logger,
                        logging.INFO,
                        keyword,
                        "watch",
                        f"vis:{current_visible}  match:{current_matching}  track:{len(seen_items)}  pending:{pending_count}",
                    )

                last_pending_count = pending_count

        except requests.exceptions.Timeout:
            log_event(logger, logging.ERROR, keyword, "error", "timeout while fetching items")
        except requests.exceptions.TooManyRedirects:
            log_event(logger, logging.ERROR, keyword, "error", "too many redirects")
        except requests.exceptions.ConnectionError as e:
            log_event(logger, logging.ERROR, keyword, "error", f"connection error: {e}")
        except requests.exceptions.RequestException as e:
            log_event(logger, logging.ERROR, keyword, "error", f"request exception: {e}")
        except Exception as e:
            log_event(logger, logging.ERROR, keyword, "error", f"unexpected error: {e}")

        time.sleep(cfg.sleep)


def main() -> None:
    settings_path = Path("settings.json")
    if not settings_path.exists():
        raise FileNotFoundError(
            "settings.json not found. Copy settings.example.json to settings.json and fill it in."
        )

    cfg = load_settings(str(settings_path))
    logger = setup_logger(cfg.log_file, cfg.log_level)

    log_event(logger, logging.INFO, "SYSTEM", "system", "loaded configuration")

    threads = []
    for index, keyword_cfg in enumerate(cfg.keywords, start=1):
        thread = threading.Thread(
            target=monitor_keyword,
            args=(cfg, keyword_cfg, logger),
            name=f"T{index}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        log_event(
            logger,
            logging.INFO,
            "SYSTEM",
            "system",
            f"started {thread.name} for keyword '{keyword_cfg.keyword}'",
        )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log_event(logger, logging.INFO, "SYSTEM", "system", "stopping due to KeyboardInterrupt")


if __name__ == "__main__":
    main()
