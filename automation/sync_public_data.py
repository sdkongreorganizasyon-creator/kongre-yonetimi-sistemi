"""EVENTIX market-data synchronizer.

The job reads the public pages requested by the user and stores one compact
snapshot in Firestore at ``system_public/economy``.

Sources:
- Altinkaynak live rates: USD, EUR, GBP, gram gold, quarter gold
- Borsa Istanbul home page: BIST 30, BIST 50, BIST 100

Tender records are not read, imported, updated, or deleted by this script.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
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

ALTINKAYNAK_URL = "https://www.altinkaynak.com/canli-kurlar/"
BORSA_ISTANBUL_URL = "https://www.borsaistanbul.com/"

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

ASSETS: dict[str, dict[str, Any]] = {
    "USDTRY": {
        "label": "Dolar",
        "aliases": ("AMERIKAN DOLARI", "ABD DOLARI", "DOLAR", "USD"),
        "bounds": (1.0, 500.0),
        "source": "ALTINKAYNAK",
        "unit": "TRY",
    },
    "EURTRY": {
        "label": "Euro",
        "aliases": ("EURO", "EUR"),
        "bounds": (1.0, 600.0),
        "source": "ALTINKAYNAK",
        "unit": "TRY",
    },
    "GBPTRY": {
        "label": "Sterlin",
        "aliases": ("INGILIZ STERLINI", "STERLIN", "GBP"),
        "bounds": (1.0, 800.0),
        "source": "ALTINKAYNAK",
        "unit": "TRY",
    },
    "GRAM_GOLD": {
        "label": "Gram Altın",
        "aliases": ("GRAM ALTIN", "GRAM ALTIN 24", "HAS ALTIN"),
        "bounds": (100.0, 100000.0),
        "source": "ALTINKAYNAK",
        "unit": "TRY",
    },
    "QUARTER_GOLD": {
        "label": "Çeyrek Altın",
        "aliases": ("CEYREK ALTIN", "YENI CEYREK", "CEYREK"),
        "bounds": (100.0, 500000.0),
        "source": "ALTINKAYNAK",
        "unit": "TRY",
    },
    "XU030": {
        "label": "BIST 30",
        "aliases": ("BIST 30", "XU030", "BIST30"),
        "bounds": (100.0, 1000000.0),
        "source": "BORSA_ISTANBUL",
        "unit": "INDEX",
    },
    "XU050": {
        "label": "BIST 50",
        "aliases": ("BIST 50", "XU050", "BIST50"),
        "bounds": (100.0, 1000000.0),
        "source": "BORSA_ISTANBUL",
        "unit": "INDEX",
    },
    "XU100": {
        "label": "BIST 100",
        "aliases": ("BIST 100", "XU100", "BIST100"),
        "bounds": (100.0, 1000000.0),
        "source": "BORSA_ISTANBUL",
        "unit": "INDEX",
    },
}


class PageCollector(HTMLParser):
    """Collect table rows, scripts, and visible text without extra packages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.scripts: list[str] = []
        self.text_parts: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._script: list[str] | None = None
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"style", "noscript"}:
            self._hidden_depth += 1
        elif tag == "script":
            self._script = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag == "script":
            if self._script is not None:
                value = compact_text(" ".join(self._script))
                if value:
                    self.scripts.append(value)
            self._script = None
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
        value = compact_text(data)
        if not value:
            return
        if self._script is not None:
            self._script.append(value)
            return
        if self._cell is not None:
            self._cell.append(value)
        if not self._hidden_depth:
            self.text_parts.append(value)


NUMBER_RE = re.compile(r"(?<![\w/])[-+]?\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|(?<![\w/])[-+]?\d+(?:[.,]\d+)?")
PERCENT_RE = re.compile(r"([-+]?\d+(?:[.,]\d+)?)\s*%")


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalized_text(value: Any) -> str:
    text = compact_text(value).upper()
    text = text.translate(str.maketrans({"İ": "I", "I": "I", "Ş": "S", "Ğ": "G", "Ü": "U", "Ö": "O", "Ç": "C"}))
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_number(value: Any) -> float | None:
    raw = compact_text(value)
    if not raw:
        return None
    raw = raw.replace("₺", "").replace("TL", "").replace("TRY", "").replace("%", "").replace(" ", "")
    if not raw:
        return None

    # Turkish-formatted values normally use a dot for thousands and comma for decimals.
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")

    raw = re.sub(r"[^0-9+\-.]", "", raw)
    try:
        return float(raw)
    except ValueError:
        return None


def number_tokens(text: str, bounds: tuple[float, float]) -> list[float]:
    lower, upper = bounds
    values: list[float] = []
    for match in NUMBER_RE.findall(compact_text(text)):
        value = normalize_number(match)
        if value is None or not (lower <= abs(value) <= upper):
            continue
        # Skip likely years and compact dates for currency rows.
        if lower < 100 and 1900 <= value <= 2100:
            continue
        values.append(value)
    return values


def percent_from_text(text: str) -> float | None:
    matches = PERCENT_RE.findall(compact_text(text))
    if not matches:
        return None
    return normalize_number(matches[-1])


def fetch_page(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 EVENTIX/3.0"
            ),
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"{url} okunamadı: {exc}") from exc

    for encoding in (charset, "utf-8", "iso-8859-9", "windows-1254"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_page(page_html: str) -> PageCollector:
    collector = PageCollector()
    collector.feed(page_html)
    collector.close()
    return collector


def alias_match(text: str, aliases: Iterable[str]) -> bool:
    normalized = normalized_text(text)
    return any(normalized_text(alias) in normalized for alias in aliases)


def key_value_near_alias(text: str, aliases: Iterable[str], keys: Iterable[str], bounds: tuple[float, float]) -> float | None:
    normalized = normalized_text(text)
    alias_positions = [normalized.find(normalized_text(alias)) for alias in aliases]
    alias_positions = [position for position in alias_positions if position >= 0]
    if not alias_positions:
        return None

    position = min(alias_positions)
    window = text[max(0, position - 800) : position + 1600]
    for key in keys:
        pattern = re.compile(
            rf"[\"']?{re.escape(key)}[\"']?\s*[:=]\s*[\"']?([-+]?\d[\d.,]*)",
            re.IGNORECASE,
        )
        match = pattern.search(window)
        if not match:
            continue
        value = normalize_number(match.group(1))
        if value is not None and bounds[0] <= abs(value) <= bounds[1]:
            return value
    return None


def parse_structured_asset(text: str, code: str) -> dict[str, Any] | None:
    spec = ASSETS[code]
    aliases = spec["aliases"]
    bounds = spec["bounds"]
    if not alias_match(text, aliases):
        return None

    buying = key_value_near_alias(text, aliases, ("alis", "alış", "buy", "buying", "bid"), bounds)
    selling = key_value_near_alias(text, aliases, ("satis", "satış", "sell", "selling", "ask"), bounds)
    value = key_value_near_alias(text, aliases, ("last", "value", "price", "close", "current", "rate"), bounds)
    change = key_value_near_alias(
        text,
        aliases,
        ("changePercent", "change_percent", "percentChange", "dailyChange", "degisim", "değişim", "yuzde"),
        (-1000.0, 1000.0),
    )

    selected = selling or value or buying
    if selected is None:
        return None
    return build_item(code, selected, buying, selling, change)


def parse_row_asset(rows: list[list[str]], code: str) -> dict[str, Any] | None:
    spec = ASSETS[code]
    aliases = spec["aliases"]
    bounds = spec["bounds"]
    for row in rows:
        joined = " | ".join(row)
        if not alias_match(joined, aliases):
            continue

        percent = percent_from_text(joined)
        numeric_cells: list[float] = []
        for cell in row:
            if "%" in cell or alias_match(cell, aliases):
                continue
            numeric_cells.extend(number_tokens(cell, bounds))

        # Remove duplicate values caused by nested spans in the same row.
        deduped: list[float] = []
        for value in numeric_cells:
            if not deduped or abs(deduped[-1] - value) > 1e-12:
                deduped.append(value)

        if not deduped:
            continue
        if spec["unit"] == "INDEX":
            return build_item(code, deduped[0], None, None, percent)

        buying = deduped[0]
        selling = deduped[1] if len(deduped) > 1 else None
        return build_item(code, selling or buying, buying, selling, percent)
    return None


def parse_window_asset(text: str, code: str) -> dict[str, Any] | None:
    spec = ASSETS[code]
    normalized = normalized_text(text)
    bounds = spec["bounds"]
    for alias in spec["aliases"]:
        marker = normalized_text(alias)
        position = normalized.find(marker)
        if position < 0:
            continue
        window = text[max(0, position - 150) : position + 700]
        for candidate in spec["aliases"]:
            window = re.sub(re.escape(candidate), " ", window, flags=re.IGNORECASE)
        values = number_tokens(window, bounds)
        if not values:
            continue
        percent = percent_from_text(window)
        if spec["unit"] == "INDEX":
            return build_item(code, values[0], None, None, percent)
        buying = values[0]
        selling = values[1] if len(values) > 1 else None
        return build_item(code, selling or buying, buying, selling, percent)
    return None


def build_item(
    code: str,
    value: float,
    buying: float | None,
    selling: float | None,
    change: float | None,
) -> dict[str, Any]:
    spec = ASSETS[code]
    item: dict[str, Any] = {
        "code": code,
        "label": spec["label"],
        "value": round(float(value), 6),
        "changePercent": None if change is None else round(float(change), 4),
        "unit": spec["unit"],
        "source": spec["source"],
        "sourceUrl": ALTINKAYNAK_URL if spec["source"] == "ALTINKAYNAK" else BORSA_ISTANBUL_URL,
    }
    if buying is not None:
        item["buying"] = round(float(buying), 6)
    if selling is not None:
        item["selling"] = round(float(selling), 6)

    if spec["unit"] == "TRY":
        note_parts: list[str] = []
        if buying is not None:
            note_parts.append(f"Alış {buying:.2f}")
        if selling is not None:
            note_parts.append(f"Satış {selling:.2f}")
        item["note"] = " • ".join(note_parts)
    else:
        item["note"] = "Borsa İstanbul endeksi"
    return item


def extract_assets(page_html: str, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    collector = parse_page(page_html)
    script_text = "\n".join(collector.scripts)
    visible_text = " | ".join(collector.text_parts)
    full_text = compact_text(page_html)
    results: dict[str, dict[str, Any]] = {}

    for code in codes:
        item = parse_row_asset(collector.rows, code)
        if item is None and script_text:
            item = parse_structured_asset(script_text, code)
        if item is None:
            item = parse_structured_asset(full_text, code)
        if item is None:
            item = parse_window_asset(visible_text, code)
        if item is not None:
            results[code] = item
    return results


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
        str(item.get("code")): item
        for item in items
        if isinstance(item, dict) and item.get("code")
    }


def source_item_codes(source: str) -> tuple[str, ...]:
    return tuple(code for code in ORDER if ASSETS[code]["source"] == source)


def sync_economy(db) -> None:
    fresh: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    try:
        altinkaynak_html = fetch_page(ALTINKAYNAK_URL)
        fresh.update(extract_assets(altinkaynak_html, source_item_codes("ALTINKAYNAK")))
    except Exception as exc:  # The other source can still be refreshed.
        errors["Altınkaynak"] = str(exc)

    try:
        borsa_html = fetch_page(BORSA_ISTANBUL_URL)
        fresh.update(extract_assets(borsa_html, source_item_codes("BORSA_ISTANBUL")))
    except Exception as exc:  # The other source can still be refreshed.
        errors["Borsa İstanbul"] = str(exc)

    previous = existing_items(db)
    final_items: list[dict[str, Any]] = []
    missing: list[str] = []
    stale_codes: list[str] = []

    for code in ORDER:
        item = fresh.get(code)
        if item is not None:
            old_item = previous.get(code)
            if item.get("changePercent") is None and old_item is not None:
                old_value = normalize_number(old_item.get("value"))
                new_value = normalize_number(item.get("value"))
                if old_value not in (None, 0) and new_value is not None:
                    item["changePercent"] = round(((new_value - old_value) / old_value) * 100, 4)
            item["stale"] = False
            item["fetchedAt"] = now_iso()
            final_items.append(item)
            continue

        old_item = previous.get(code)
        if old_item is not None:
            fallback = dict(old_item)
            fallback["stale"] = True
            fallback["note"] = compact_text(fallback.get("note") or "Önceki başarılı güncellemeden kalan veri")
            final_items.append(fallback)
            stale_codes.append(code)
        else:
            missing.append(code)

    if missing:
        details = "; ".join(f"{name}: {message}" for name, message in errors.items())
        raise RuntimeError(
            "Ekonomi paneli için ilk veri seti tamamlanamadı. "
            f"Eksik göstergeler: {', '.join(missing)}. {details}".strip()
        )

    snapshot = {
        "items": final_items,
        "lastUpdated": now_iso(),
        "source": "ALTINKAYNAK_BORSAISTANBUL",
        "sourceLabel": "Altınkaynak • Borsa İstanbul",
        "sourceUrls": {
            "altinkaynak": ALTINKAYNAK_URL,
            "borsaIstanbul": BORSA_ISTANBUL_URL,
        },
        "partial": bool(errors or stale_codes),
        "staleCodes": stale_codes,
        "sourceErrors": errors,
    }
    db.collection("system_public").document("economy").set(snapshot)
    fresh_count = len([item for item in final_items if not item.get("stale")])
    print(
        f"{fresh_count}/{len(final_items)} ekonomi göstergesi güncellendi. "
        "Kaynaklar: Altınkaynak ve Borsa İstanbul."
    )
    if errors:
        print("Kısmi kaynak uyarıları: " + "; ".join(f"{key}: {value}" for key, value in errors.items()))
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
