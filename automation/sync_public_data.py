"""EVENTIX live market-data synchronizer.

Writes a compact economy snapshot to Firestore document
``system_public/economy`` without changing any other application data.

Sources
-------
Altinkaynak official data services:
- Currency.json: USD, EUR, GBP
- Gold.json: gram gold, quarter gold

Bloomberg HT public Borsa page:
- /borsa: BIST 30, BIST 50 and BIST 100

The page data is supplied by Foreks. The synchronizer reads only the three
public index rows requested by the application.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ModuleNotFoundError:
    firebase_admin = None
    credentials = None
    firestore = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

ALTINKAYNAK_CURRENCY_URL = "https://rest.altinkaynak.com/Currency.json"
ALTINKAYNAK_GOLD_URL = "https://rest.altinkaynak.com/Gold.json"
ALTINKAYNAK_CURRENCY_PAGE = "https://www.altinkaynak.com/Doviz/Kur/Guncel"
ALTINKAYNAK_GOLD_PAGE = "https://www.altinkaynak.com/Altin/Kur/Guncel"
ALTINKAYNAK_LIVE_PAGE = "https://www.altinkaynak.com/canli-kurlar/"

BLOOMBERGHT_BORSA_URL = "https://www.bloomberght.com/borsa"

# Keep the existing code-to-URL mapping so the rest of the application can
# continue using XU030, XU050 and XU100 without any UI/data-model changes.
BIST_URLS = {
    "XU030": BLOOMBERGHT_BORSA_URL,
    "XU050": BLOOMBERGHT_BORSA_URL,
    "XU100": BLOOMBERGHT_BORSA_URL,
}

ORDER = (
    "USDTRY",
    "EURTRY",
    "GBPTRY",
    "GRAM_GOLD",
    "QUARTER_GOLD",
    "XU030",
    "XU050",
    "XU100",
)

LABELS = {
    "USDTRY": "Dolar",
    "EURTRY": "Euro",
    "GBPTRY": "Sterlin",
    "GRAM_GOLD": "Gram Altın",
    "QUARTER_GOLD": "Çeyrek Altın",
    "XU030": "BIST 30",
    "XU050": "BIST 50",
    "XU100": "BIST 100",
}

ALTINKAYNAK_CODE_MAP = {
    "USD": "USDTRY",
    "EUR": "EURTRY",
    "GBP": "GBPTRY",
    "GA": "GRAM_GOLD",
    "C": "QUARTER_GOLD",
}

ALTINKAYNAK_ALIASES = {
    "USDTRY": ("USD", "AMERIKAN DOLARI", "ABD DOLARI", "DOLAR"),
    "EURTRY": ("EUR", "EURO"),
    "GBPTRY": ("GBP", "INGILIZ STERLINI", "STERLIN"),
    "GRAM_GOLD": ("GRAM ALTIN", "GRAM", "24 AYAR"),
    "QUARTER_GOLD": ("CEYREK ALTIN", "CEYREK"),
}

VALUE_BOUNDS = {
    "USDTRY": (1.0, 1000.0),
    "EURTRY": (1.0, 1000.0),
    "GBPTRY": (1.0, 1000.0),
    "GRAM_GOLD": (100.0, 1_000_000.0),
    "QUARTER_GOLD": (100.0, 2_000_000.0),
    "XU030": (100.0, 2_000_000.0),
    "XU050": (100.0, 2_000_000.0),
    "XU100": (100.0, 2_000_000.0),
}

NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|[-+]?\d+(?:[.,]\d+)?")
DATE_TIME_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}\s*(?:-|–)?\s*\d{2}:\d{2}:\d{2}")


class VisibleTextCollector(HTMLParser):
    """Collect visible text tokens and table rows using the standard library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden += 1
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            value = compact_text(" ".join(self._cell))
            self._row.append(value)
            self._cell = None
        elif tag == "tr":
            if self._row and any(self._row):
                self.rows.append(self._row)
            self._row = None
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        value = compact_text(data)
        if not value:
            return
        self.tokens.append(value)
        if self._cell is not None:
            self._cell.append(value)


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalized_text(value: Any) -> str:
    text = compact_text(value).upper().translate(
        str.maketrans({"İ": "I", "Ş": "S", "Ğ": "G", "Ü": "U", "Ö": "O", "Ç": "C"})
    )
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    raw = compact_text(value)
    if not raw:
        return None
    raw = raw.replace("₺", "").replace("TL", "").replace("TRY", "").replace("%", "").replace(" ", "")
    raw = re.sub(r"[^0-9,\.\-+]", "", raw)
    if not raw or raw in {"-", "+", ".", ","}:
        return None

    if "," in raw and "." in raw:
        # Turkish style: 12.345,67. International style: 12,345.67.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")

    try:
        return float(raw)
    except ValueError:
        return None


def values_in_text(text: str, bounds: tuple[float, float]) -> list[float]:
    lower, upper = bounds
    values: list[float] = []
    for match in NUMBER_RE.findall(compact_text(text)):
        value = normalize_number(match)
        if value is None or not (lower <= abs(value) <= upper):
            continue
        values.append(value)
    return values


def request_bytes(url: str, *, accept: str, attempts: int = 3) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "Accept": accept,
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6",
                "Accept-Encoding": "gzip, identity",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read()
                if str(response.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw, str(response.headers.get("Content-Type") or "")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
    raise RuntimeError(f"{url} okunamadı: {last_error}") from last_error


def decode_html(raw: bytes, content_type: str = "") -> str:
    encodings: list[str] = []
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8", "windows-1254", "iso-8859-9"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def build_altinkaynak_item(code: str, record: dict[str, Any]) -> dict[str, Any] | None:
    buying = normalize_number(record.get("Alis") or record.get("alis") or record.get("buy"))
    selling = normalize_number(record.get("Satis") or record.get("satis") or record.get("sell"))
    value = selling or buying
    if value is None:
        return None

    lower, upper = VALUE_BOUNDS[code]
    if not (lower <= value <= upper):
        return None

    change = normalize_number(record.get("Change") or record.get("Degisim") or record.get("change"))
    updated = compact_text(
        record.get("GuncellenmeZamani")
        or record.get("guncellenmeZamani")
        or record.get("updatedAt")
        or ""
    )
    item: dict[str, Any] = {
        "code": code,
        "label": LABELS[code],
        "value": round(value, 6),
        "changePercent": None if change is None else round(change, 4),
        "unit": "TRY",
        "source": "ALTINKAYNAK",
        "sourceUrl": "https://www.altinkaynak.com/canli-kurlar/",
        "sourceDate": updated,
        "stale": False,
        "fetchedAt": now_iso(),
    }
    if buying is not None:
        item["buying"] = round(buying, 6)
    if selling is not None:
        item["selling"] = round(selling, 6)
    notes: list[str] = []
    if buying is not None:
        notes.append(f"Alış {buying:.2f}")
    if selling is not None:
        notes.append(f"Satış {selling:.2f}")
    item["note"] = " • ".join(notes)
    return item


def parse_altinkaynak_json(raw: bytes, wanted_codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Altınkaynak JSON verisi çözümlenemedi.") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Altınkaynak JSON yanıtı liste biçiminde değil.")

    wanted = set(wanted_codes)
    result: dict[str, dict[str, Any]] = {}
    for record in payload:
        if not isinstance(record, dict):
            continue
        provider_code = compact_text(record.get("Kod") or record.get("kod")).upper()
        code = ALTINKAYNAK_CODE_MAP.get(provider_code)
        if code not in wanted:
            continue
        item = build_altinkaynak_item(code, record)
        if item is not None:
            result[code] = item
    return result


def collector_from_html(page_html: str) -> VisibleTextCollector:
    collector = VisibleTextCollector()
    collector.feed(page_html)
    collector.close()
    return collector


def alias_match(value: str, aliases: Iterable[str]) -> bool:
    normalized = normalized_text(value)
    return any(normalized_text(alias) in normalized for alias in aliases)


def parse_altinkaynak_html(page_html: str, wanted_codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Fallback parser for Altinkaynak public pages."""

    collector = collector_from_html(page_html)
    wanted = set(wanted_codes)
    result: dict[str, dict[str, Any]] = {}

    for code in wanted:
        aliases = ALTINKAYNAK_ALIASES[code]
        bounds = VALUE_BOUNDS[code]
        for row in collector.rows:
            joined = " | ".join(row)
            if not alias_match(joined, aliases):
                continue
            prices: list[float] = []
            change: float | None = None
            for cell in row:
                if "%" in cell:
                    candidates = values_in_text(cell, (-1000.0, 1000.0))
                    if candidates:
                        change = candidates[-1]
                    continue
                if alias_match(cell, aliases):
                    continue
                prices.extend(values_in_text(cell, bounds))
            if len(prices) < 2:
                continue
            buying, selling = prices[-2], prices[-1]
            record = {
                "Alis": buying,
                "Satis": selling,
                "Change": change,
            }
            item = build_altinkaynak_item(code, record)
            if item is not None:
                result[code] = item
                break
    return result


def iter_nested_records(value: Any) -> Iterable[dict[str, Any]]:
    """Yield dictionaries from nested JSON payloads without assuming one fixed schema."""

    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nested_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_nested_records(child)


def first_record_value(record: dict[str, Any], keys: Iterable[str]) -> Any:
    lowered = {normalized_text(key): value for key, value in record.items()}
    for key in keys:
        candidate = lowered.get(normalized_text(key))
        if candidate not in (None, ""):
            return candidate
    return None


def parse_altinkaynak_payload(payload: Any, wanted_codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Parse Altinkaynak JSON/XHR responses captured by the rendered page."""

    wanted = set(wanted_codes)
    result: dict[str, dict[str, Any]] = {}
    code_keys = ("Kod", "Code", "Symbol", "Sembol", "CurrencyCode")
    name_keys = ("Aciklama", "Açıklama", "Description", "Name", "Ad", "Baslik", "Başlık")
    buy_keys = ("Alis", "Alış", "Buy", "Buying", "BuyingPrice", "Bid")
    sell_keys = ("Satis", "Satış", "Sell", "Selling", "SellingPrice", "Ask")
    change_keys = ("Change", "Degisim", "Değişim", "ChangePercent", "Rate")
    update_keys = ("GuncellenmeZamani", "GüncellenmeZamanı", "UpdatedAt", "Date", "Tarih")

    for record in iter_nested_records(payload):
        provider_code = compact_text(first_record_value(record, code_keys)).upper()
        code = ALTINKAYNAK_CODE_MAP.get(provider_code)

        if code not in wanted:
            name = compact_text(first_record_value(record, name_keys))
            if name:
                for candidate in wanted:
                    if alias_match(name, ALTINKAYNAK_ALIASES[candidate]):
                        code = candidate
                        break

        if code not in wanted or code in result:
            continue

        canonical = {
            "Alis": first_record_value(record, buy_keys),
            "Satis": first_record_value(record, sell_keys),
            "Change": first_record_value(record, change_keys),
            "GuncellenmeZamani": first_record_value(record, update_keys),
        }
        item = build_altinkaynak_item(code, canonical)
        if item is not None:
            result[code] = item

    return result


def parse_altinkaynak_text_rows(rows: Iterable[str], wanted_codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Parse visible rendered row/card text when Altinkaynak data is injected by JavaScript."""

    wanted = set(wanted_codes)
    result: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = compact_text(raw_row)
        if not row:
            continue
        row_without_dates = DATE_TIME_RE.sub(" ", row)
        row_without_dates = re.sub(r"\b\d{2}[./-]\d{2}[./-]\d{4}\b", " ", row_without_dates)
        row_without_dates = re.sub(r"\b\d{2}:\d{2}(?::\d{2})?\b", " ", row_without_dates)

        for code in wanted - result.keys():
            if not alias_match(row_without_dates, ALTINKAYNAK_ALIASES[code]):
                continue

            prices = values_in_text(row_without_dates, VALUE_BOUNDS[code])
            if not prices:
                continue

            # Rendered Altinkaynak rows normally contain buying and selling values.
            # If only one valid value is visible, preserve it as the displayed price
            # rather than inventing a second value.
            buying = prices[0]
            selling = prices[0]
            if len(prices) > 1:
                plausible_pair: tuple[float, float] | None = None
                for left, right in zip(prices, prices[1:]):
                    ratio = right / left if left else 0
                    if 0.75 <= ratio <= 1.35:
                        plausible_pair = (left, right)
                        break
                if plausible_pair is not None:
                    buying, selling = plausible_pair
                else:
                    buying, selling = prices[0], prices[1]
            change: float | None = None
            percent_match = re.search(r"%\s*([-+]?\d+(?:[.,]\d+)?)", row_without_dates)
            if percent_match is None:
                percent_match = re.search(
                    r"(?<![\d.,])([-+]?\d+(?:[.,]\d+)?)\s*%(?!\s*\d)",
                    row_without_dates,
                )
            if percent_match:
                change = normalize_number(percent_match.group(1))

            item = build_altinkaynak_item(
                code,
                {"Alis": buying, "Satis": selling, "Change": change},
            )
            if item is not None:
                if len(prices) == 1:
                    item.pop("buying", None)
                    item["note"] = f"Güncel değer {selling:.2f}"
                result[code] = item

    return result


def fetch_altinkaynak_rendered(codes: Iterable[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Render Altinkaynak pages and read XHR/DOM data as a fallback for 52x API errors."""

    wanted = list(dict.fromkeys(codes))
    if not wanted:
        return {}, {}
    if sync_playwright is None:
        return {}, {LABELS[code]: "playwright paketi yüklü değil." for code in wanted}

    result: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    executable = browser_executable()
    captured_payloads: list[Any] = []

    def capture_json(response: Any) -> None:
        try:
            content_type = str(response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            if "altinkaynak" not in str(response.url).lower():
                return
            captured_payloads.append(response.json())
        except Exception:
            return

    page_specs = (
        (ALTINKAYNAK_LIVE_PAGE, tuple(wanted)),
        (ALTINKAYNAK_CURRENCY_PAGE, tuple(code for code in wanted if code in {"USDTRY", "EURTRY", "GBPTRY"})),
        (ALTINKAYNAK_GOLD_PAGE, tuple(code for code in wanted if code in {"GRAM_GOLD", "QUARTER_GOLD"})),
    )

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if executable:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            try:
                for page_url, page_codes in page_specs:
                    unresolved = [code for code in page_codes if code not in result]
                    if not unresolved:
                        continue
                    page = context.new_page()
                    page.on("response", capture_json)
                    try:
                        page.goto(page_url, wait_until="domcontentloaded", timeout=90_000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=25_000)
                        except PlaywrightTimeoutError:
                            pass
                        page.wait_for_timeout(6_000)

                        # 1) Read JSON/XHR responses used by the page itself.
                        for payload in captured_payloads:
                            missing_now = [code for code in unresolved if code not in result]
                            if not missing_now:
                                break
                            result.update(parse_altinkaynak_payload(payload, missing_now))

                        # 2) Parse the fully rendered DOM, including JavaScript-injected rows.
                        missing_now = [code for code in unresolved if code not in result]
                        if missing_now:
                            rendered_html = page.content()
                            result.update(parse_altinkaynak_html(rendered_html, missing_now))

                        # 3) Parse visible table/card/body text as a final source-faithful fallback.
                        missing_now = [code for code in unresolved if code not in result]
                        if missing_now:
                            text_blocks: list[str] = []
                            for selector in (
                                "tr",
                                "[role=row]",
                                "li",
                                "[class*=card]",
                                "[class*=kur]",
                                "[class*=gold]",
                                "[class*=currency]",
                            ):
                                try:
                                    text_blocks.extend(page.locator(selector).all_inner_texts())
                                except Exception:
                                    continue

                            # Collect small ancestor blocks around the exact labels. This
                            # handles card-based layouts where labels and prices are split
                            # across sibling elements rather than a table row.
                            for code in missing_now:
                                for alias in ALTINKAYNAK_ALIASES[code]:
                                    try:
                                        matches = page.get_by_text(alias, exact=False)
                                        for index in range(min(matches.count(), 5)):
                                            node = matches.nth(index)
                                            for _level in range(5):
                                                try:
                                                    block = compact_text(node.inner_text(timeout=5_000))
                                                except Exception:
                                                    block = ""
                                                if block and len(block) <= 2_000:
                                                    text_blocks.append(block)
                                                node = node.locator("xpath=..")
                                    except Exception:
                                        continue

                            # Preserve line boundaries from the rendered body and create
                            # short windows around labels; never parse the whole page as one
                            # giant row, which could associate an unrelated price.
                            try:
                                body_text = page.locator("body").inner_text(timeout=30_000)
                                body_lines = [compact_text(line) for line in body_text.splitlines() if compact_text(line)]
                                text_blocks.extend(body_lines)
                                for index, line in enumerate(body_lines):
                                    if any(
                                        alias_match(line, ALTINKAYNAK_ALIASES[code])
                                        for code in missing_now
                                    ):
                                        text_blocks.append(" | ".join(body_lines[index : index + 8]))
                            except Exception:
                                pass

                            result.update(parse_altinkaynak_text_rows(text_blocks, missing_now))
                    except Exception as exc:
                        for code in unresolved:
                            errors.setdefault(LABELS[code], str(exc))
                    finally:
                        page.close()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        for code in wanted:
            errors.setdefault(LABELS[code], f"Altınkaynak tarayıcısı başlatılamadı: {exc}")

    for code in wanted:
        if code not in result:
            errors.setdefault(LABELS[code], "Render edilmiş Altınkaynak sayfasında veri bulunamadı.")
    return result, errors


def fetch_altinkaynak() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    result: dict[str, dict[str, Any]] = {}
    attempt_errors: dict[str, list[str]] = {}

    source_specs = (
        (
            "Altınkaynak döviz",
            ALTINKAYNAK_CURRENCY_URL,
            ALTINKAYNAK_CURRENCY_PAGE,
            ("USDTRY", "EURTRY", "GBPTRY"),
        ),
        (
            "Altınkaynak altın",
            ALTINKAYNAK_GOLD_URL,
            ALTINKAYNAK_GOLD_PAGE,
            ("GRAM_GOLD", "QUARTER_GOLD"),
        ),
    )

    # Fast path: official JSON services, then static public-page HTML.
    for source_name, json_url, page_url, codes in source_specs:
        source_errors: list[str] = []
        try:
            raw, _ = request_bytes(json_url, accept="application/json,text/plain;q=0.9,*/*;q=0.5")
            result.update(parse_altinkaynak_json(raw, codes))
        except Exception as exc:
            source_errors.append(f"JSON: {exc}")

        missing = [code for code in codes if code not in result]
        if missing:
            try:
                raw, content_type = request_bytes(page_url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
                result.update(parse_altinkaynak_html(decode_html(raw, content_type), missing))
            except Exception as exc:
                source_errors.append(f"HTML: {exc}")
        attempt_errors[source_name] = source_errors

    # Altinkaynak's REST host can return Cloudflare 522/523 to GitHub runners.
    # In that case use the exact public page requested by the user, render it in
    # the already-installed headless browser, and read the page's own XHR/DOM data.
    missing_all = [code for code in ("USDTRY", "EURTRY", "GBPTRY", "GRAM_GOLD", "QUARTER_GOLD") if code not in result]
    rendered_errors: dict[str, str] = {}
    if missing_all:
        rendered, rendered_errors = fetch_altinkaynak_rendered(missing_all)
        result.update(rendered)

    errors: dict[str, str] = {}
    for source_name, _json_url, _page_url, codes in source_specs:
        missing = [code for code in codes if code not in result]
        if not missing:
            continue
        messages = list(attempt_errors.get(source_name, []))
        for code in missing:
            message = rendered_errors.get(LABELS[code])
            if message:
                messages.append(f"{LABELS[code]} render: {message}")
        errors[source_name] = f"Eksik: {', '.join(missing)}. " + " | ".join(messages)

    return result, errors


def next_numeric_values(tokens: list[str], start: int, bounds: tuple[float, float], limit: int = 16) -> list[float]:
    values: list[float] = []
    for token in tokens[start : start + limit]:
        values.extend(values_in_text(token, bounds))
        if len(values) >= 2:
            break
    return values


def find_token(tokens: list[str], needle: str, start: int = 0, end: int | None = None) -> int:
    target = normalized_text(needle)
    upper = len(tokens) if end is None else min(end, len(tokens))
    for index in range(max(0, start), upper):
        if target in normalized_text(tokens[index]):
            return index
    return -1


def parse_bist_rows(rows: list[list[str]], code: str) -> tuple[float | None, float | None]:
    value: float | None = None
    change: float | None = None
    bounds = VALUE_BOUNDS[code]
    for row in rows:
        if not row:
            continue
        label = normalized_text(row[0])
        joined = " | ".join(row[1:])
        if label == "DEGER" or label.startswith("DEGER "):
            candidates = values_in_text(joined, bounds)
            if candidates:
                value = candidates[0]
        elif "ONCEKI KAPANISA GORE DEGISIM" in label:
            candidates = values_in_text(joined, (-1000.0, 1000.0))
            if candidates:
                change = candidates[0]
    return value, change


def parse_bist_text(tokens: list[str], code: str) -> tuple[float | None, float | None, str]:
    bounds = VALUE_BOUNDS[code]
    section = find_token(tokens, "Güncel Endeks Değerleri")
    if section < 0:
        section = find_token(tokens, LABELS[code])
    if section < 0:
        section = 0

    value: float | None = None
    change: float | None = None

    value_label = find_token(tokens, "Değer", section, section + 80)
    if value_label >= 0:
        candidates = next_numeric_values(tokens, value_label + 1, bounds, 12)
        if candidates:
            value = candidates[0]

    change_label = find_token(tokens, "Önceki Kapanışa Göre Değişim", section, section + 120)
    if change_label >= 0:
        candidates = next_numeric_values(tokens, change_label + 1, (-1000.0, 1000.0), 10)
        if candidates:
            change = candidates[0]

    source_date = ""
    for token in tokens[section : section + 30]:
        match = DATE_TIME_RE.search(token)
        if match:
            source_date = compact_text(match.group(0))
            break
    return value, change, source_date


def parse_bist_embedded(page_html: str, code: str) -> tuple[float | None, float | None]:
    """Last-resort parser for common JSON fields embedded in the page source."""

    normalized_code = re.escape(code)
    bounds = VALUE_BOUNDS[code]
    windows: list[str] = []
    for match in re.finditer(normalized_code, page_html, re.IGNORECASE):
        windows.append(page_html[max(0, match.start() - 1000) : match.start() + 3000])
        if len(windows) >= 8:
            break

    value_keys = ("last", "value", "currentValue", "indexValue", "close", "lastPrice")
    change_keys = ("changePercent", "changeRate", "percentChange", "dailyChange")
    value: float | None = None
    change: float | None = None

    for window in windows:
        for key in value_keys:
            match = re.search(rf'["\']?{re.escape(key)}["\']?\s*[:=]\s*["\']?([-+]?\d[\d.,]*)', window, re.IGNORECASE)
            if match:
                candidate = normalize_number(match.group(1))
                if candidate is not None and bounds[0] <= candidate <= bounds[1]:
                    value = candidate
                    break
        for key in change_keys:
            match = re.search(rf'["\']?{re.escape(key)}["\']?\s*[:=]\s*["\']?([-+]?\d[\d.,]*)', window, re.IGNORECASE)
            if match:
                candidate = normalize_number(match.group(1))
                if candidate is not None and -1000 <= candidate <= 1000:
                    change = candidate
                    break
        if value is not None:
            break
    return value, change


def parse_bist_page(page_html: str, code: str) -> dict[str, Any]:
    collector = collector_from_html(page_html)
    row_value, row_change = parse_bist_rows(collector.rows, code)
    text_value, text_change, source_date = parse_bist_text(collector.tokens, code)
    embedded_value, embedded_change = parse_bist_embedded(page_html, code)

    value = row_value or text_value or embedded_value
    change = row_change if row_change is not None else text_change
    if change is None:
        change = embedded_change
    if value is None:
        raise RuntimeError(f"{LABELS[code]} değeri Borsa İstanbul sayfasında bulunamadı.")

    return {
        "code": code,
        "label": LABELS[code],
        "value": round(value, 6),
        "changePercent": None if change is None else round(change, 4),
        "unit": "INDEX",
        "source": "BORSA_ISTANBUL",
        "sourceUrl": BIST_URLS[code],
        "sourceDate": source_date,
        "note": "Borsa İstanbul verisi (en az 15 dakika gecikmeli)",
        "stale": False,
        "fetchedAt": now_iso(),
    }


def parse_bist_rendered_text(body_text: str, code: str) -> dict[str, Any]:
    tokens = [compact_text(line) for line in str(body_text or "").splitlines() if compact_text(line)]
    value, change, source_date = parse_bist_text(tokens, code)
    if value is None:
        raise RuntimeError(f"{LABELS[code]} değeri render edilmiş Borsa İstanbul sayfasında bulunamadı.")
    return {
        "code": code,
        "label": LABELS[code],
        "value": round(value, 6),
        "changePercent": None if change is None else round(change, 4),
        "unit": "INDEX",
        "source": "BORSA_ISTANBUL",
        "sourceUrl": BIST_URLS[code],
        "sourceDate": source_date,
        "note": "Borsa İstanbul verisi (en az 15 dakika gecikmeli)",
        "stale": False,
        "fetchedAt": now_iso(),
    }


def browser_executable() -> str | None:
    configured = env("BROWSER_EXECUTABLE_PATH")
    if configured:
        return configured
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def fetch_bist_rendered(codes: Iterable[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    wanted = list(codes)
    if not wanted:
        return {}, {}
    if sync_playwright is None:
        return {}, {LABELS[code]: "playwright paketi yüklü değil." for code in wanted}

    result: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    executable = browser_executable()

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if executable:
                launch_options["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            try:
                for code in wanted:
                    page = context.new_page()
                    try:
                        page.goto(BIST_URLS[code], wait_until="domcontentloaded", timeout=90_000)
                        try:
                            page.get_by_text("Güncel Endeks Değerleri", exact=False).first.wait_for(timeout=45_000)
                        except PlaywrightTimeoutError:
                            pass
                        try:
                            page.wait_for_load_state("networkidle", timeout=20_000)
                        except PlaywrightTimeoutError:
                            pass
                        page.wait_for_timeout(4_000)
                        body_text = page.locator("body").inner_text(timeout=30_000)
                        result[code] = parse_bist_rendered_text(body_text, code)
                    except Exception as exc:
                        errors[LABELS[code]] = str(exc)
                    finally:
                        page.close()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        for code in wanted:
            errors.setdefault(LABELS[code], f"Tarayıcı başlatılamadı: {exc}")

    return result, errors


def parse_bloomberght_bist(page_html: str) -> dict[str, dict[str, Any]]:
    """Read XU030, XU050 and XU100 from Bloomberg HT's public Borsa table.

    The page currently renders rows in this order:
    symbol/name, last value, percentage change, point difference, monthly
    change and annual change. The parser deliberately uses only the first two
    numeric fields after each symbol so unrelated figures are ignored.
    """

    collector = collector_from_html(page_html)
    result: dict[str, dict[str, Any]] = {}

    def build_item(code: str, value: float, change: float | None) -> dict[str, Any]:
        return {
            "code": code,
            "label": LABELS[code],
            "value": round(value, 6),
            "changePercent": None if change is None else round(change, 4),
            "unit": "INDEX",
            "source": "BLOOMBERGHT_FOREKS",
            "sourceUrl": BLOOMBERGHT_BORSA_URL,
            "sourceDate": "",
            "note": "Bloomberg HT / Foreks Borsa verisi",
            "stale": False,
            "fetchedAt": now_iso(),
        }

    # Preferred path: parse each HTML table row. This is the least ambiguous
    # representation because the first numeric cell is SON and the second is %.
    for row in collector.rows:
        joined = normalized_text(" | ".join(row))
        for code in BIST_URLS:
            if code in result or code not in joined:
                continue

            value: float | None = None
            change: float | None = None
            marker_seen = False
            for cell in row:
                normalized = normalized_text(cell)
                if code in normalized:
                    marker_seen = True
                    continue
                if not marker_seen or "BIST" in normalized or "ENDEKS" in normalized:
                    continue

                if value is None:
                    candidates = values_in_text(cell, VALUE_BOUNDS[code])
                    if candidates:
                        value = candidates[0]
                        continue

                if value is not None and change is None:
                    candidates = values_in_text(cell, (-100.0, 100.0))
                    if candidates:
                        change = candidates[0]
                        break

            if value is not None:
                result[code] = build_item(code, value, change)

    # Fallback path: some page revisions may not expose semantic <tr> tags.
    # Scan visible tokens after each exact index symbol and take SON then %.
    tokens = collector.tokens
    for code in BIST_URLS:
        if code in result:
            continue

        marker = find_token(tokens, code)
        if marker < 0:
            continue

        value: float | None = None
        change: float | None = None
        for token in tokens[marker + 1 : marker + 18]:
            normalized = normalized_text(token)
            if any(other != code and other in normalized for other in BIST_URLS):
                break
            if code in normalized or "BIST" in normalized or "ENDEKS" in normalized:
                continue

            if value is None:
                candidates = values_in_text(token, VALUE_BOUNDS[code])
                if candidates:
                    value = candidates[0]
                    continue

            candidates = values_in_text(token, (-100.0, 100.0))
            if candidates:
                change = candidates[0]
                break

        if value is not None:
            result[code] = build_item(code, value, change)

    return result


def fetch_bist() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Fetch the Bloomberg HT Borsa page once and extract the three indices."""

    try:
        raw, content_type = request_bytes(
            BLOOMBERGHT_BORSA_URL,
            accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        )
        result = parse_bloomberght_bist(decode_html(raw, content_type))
    except Exception as exc:
        message = f"Bloomberg HT Borsa sayfası okunamadı: {exc}"
        return {}, {LABELS[code]: message for code in BIST_URLS}

    errors: dict[str, str] = {}
    for code in BIST_URLS:
        if code not in result:
            errors[LABELS[code]] = (
                f"{LABELS[code]} değeri Bloomberg HT Borsa tablosunda bulunamadı."
            )
    return result, errors


def firestore_client():
    raw = env("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    if firebase_admin is None or credentials is None or firestore is None:
        raise RuntimeError("firebase-admin paketi yüklü değil. automation/requirements.txt dosyasını kontrol edin.")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON geçerli JSON değil.") from exc

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def existing_items(db) -> dict[str, dict[str, Any]]:
    snapshot = db.collection("system_public").document("economy").get()
    if not snapshot.exists:
        return {}
    payload = snapshot.to_dict() or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return {}
    return {
        compact_text(item.get("code")): item
        for item in items
        if isinstance(item, dict) and item.get("code")
    }


def apply_calculated_change(item: dict[str, Any], old_item: dict[str, Any] | None) -> None:
    if item.get("changePercent") is not None or old_item is None:
        return
    old_value = normalize_number(old_item.get("value"))
    new_value = normalize_number(item.get("value"))
    if old_value in (None, 0) or new_value is None:
        return
    item["changePercent"] = round(((new_value - old_value) / old_value) * 100, 4)


def sync_economy(db) -> None:
    fresh: dict[str, dict[str, Any]] = {}
    source_errors: dict[str, str] = {}

    altinkaynak_items, altinkaynak_errors = fetch_altinkaynak()
    fresh.update(altinkaynak_items)
    source_errors.update(altinkaynak_errors)

    bist_items, bist_errors = fetch_bist()
    fresh.update(bist_items)
    source_errors.update(bist_errors)

    previous = existing_items(db)
    final_items: list[dict[str, Any]] = []
    missing: list[str] = []
    stale_codes: list[str] = []

    for code in ORDER:
        item = fresh.get(code)
        if item is not None:
            apply_calculated_change(item, previous.get(code))
            final_items.append(item)
            continue

        old_item = previous.get(code)
        if old_item is not None and normalize_number(old_item.get("value")) not in (None, 0):
            fallback = dict(old_item)
            fallback["stale"] = True
            fallback["note"] = compact_text(fallback.get("note") or "Önceki başarılı güncellemeden kalan veri")
            final_items.append(fallback)
            stale_codes.append(code)
        else:
            missing.append(code)

    if missing:
        details = "; ".join(f"{name}: {message}" for name, message in source_errors.items())
        raise RuntimeError(
            "Ekonomi paneli için ilk veri seti tamamlanamadı. "
            f"Eksik göstergeler: {', '.join(missing)}. {details}".strip()
        )

    snapshot = {
        "items": final_items,
        "lastUpdated": now_iso(),
        "source": "ALTINKAYNAK_BLOOMBERGHT",
        "sourceLabel": "Altınkaynak • Bloomberg HT / Foreks",
        "sourceUrls": {
            "altinkaynak": "https://www.altinkaynak.com/canli-kurlar/",
            "bloombergHT": BLOOMBERGHT_BORSA_URL,
        },
        "partial": bool(source_errors or stale_codes),
        "staleCodes": stale_codes,
        "sourceErrors": source_errors,
    }
    db.collection("system_public").document("economy").set(snapshot)

    fresh_count = len([item for item in final_items if not item.get("stale")])
    print(f"{fresh_count}/{len(final_items)} ekonomi göstergesi Firestore'a yazıldı.")
    print("Göstergeler: " + ", ".join(item["label"] for item in final_items))
    if source_errors:
        print("Kaynak uyarıları: " + "; ".join(f"{key}: {value}" for key, value in source_errors.items()))
    if stale_codes:
        print("Önceki veriden korunan göstergeler: " + ", ".join(stale_codes))


def main() -> int:
    db = firestore_client()
    if db is None:
        print("FIREBASE_SERVICE_ACCOUNT_JSON tanımlı değil; ekonomi senkronizasyonu atlandı.")
        return 0
    sync_economy(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
