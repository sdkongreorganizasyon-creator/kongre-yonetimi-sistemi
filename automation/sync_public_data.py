"""EVENTIX economy-data synchronizer.

This script runs in GitHub Actions and writes only the economy snapshot under
Firestore collection ``system_public`` / document ``economy``. It does not read,
write, import, or delete tender records.

Default source: TCMB current exchange-rate XML. A custom JSON/XML feed can be
used by defining ECONOMY_FEED_URL and, if required, ECONOMY_FEED_TOKEN.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ModuleNotFoundError:
    firebase_admin = None
    credentials = None
    firestore = None

DEFAULT_TCBM_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
DEFAULT_CURRENCY_CODES = ("USD", "EUR", "GBP", "CHF", "JPY")
CURRENCY_LABELS = {
    "USD": "ABD Doları / TL",
    "EUR": "Euro / TL",
    "GBP": "İngiliz Sterlini / TL",
    "CHF": "İsviçre Frangı / TL",
    "JPY": "Japon Yeni / TL",
}


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip().replace("₺", "").replace("TL", "").replace("TRY", "")
    raw = raw.replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    raw = re.sub(r"[^0-9.-]", "", raw)
    try:
        return float(raw)
    except ValueError:
        return 0.0


def first_value(source: dict[str, Any], keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def request_content(url: str, token: str = "") -> tuple[bytes, str]:
    headers = {
        "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.8",
        "User-Agent": "EVENTIX-Economy-Sync/2.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=45) as response:
            return response.read(), str(response.headers.get("Content-Type") or "")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Ekonomi veri kaynağı okunamadı: {exc}") from exc


def configured_codes() -> tuple[str, ...]:
    raw = env("ECONOMY_CURRENCY_CODES", ",".join(DEFAULT_CURRENCY_CODES))
    codes = tuple(code.strip().upper() for code in raw.split(",") if code.strip())
    return codes or DEFAULT_CURRENCY_CODES


def parse_tcmb_xml(content: bytes) -> tuple[list[dict[str, Any]], str]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise RuntimeError("TCMB XML verisi çözümlenemedi.") from exc

    wanted = set(configured_codes())
    items: list[dict[str, Any]] = []

    for currency in root.findall("Currency"):
        code = str(currency.attrib.get("CurrencyCode") or currency.attrib.get("Kod") or "").upper()
        if code not in wanted:
            continue

        unit = normalize_number(currency.findtext("Unit")) or 1.0
        buying = normalize_number(currency.findtext("ForexBuying"))
        selling = normalize_number(currency.findtext("ForexSelling"))
        selected = selling or buying
        if selected <= 0:
            continue

        value = selected / unit
        buying_per_unit = buying / unit if buying else 0.0
        selling_per_unit = selling / unit if selling else 0.0
        note_parts = []
        if buying_per_unit:
            note_parts.append(f"Alış {buying_per_unit:.4f}")
        if selling_per_unit:
            note_parts.append(f"Satış {selling_per_unit:.4f}")

        items.append(
            {
                "code": f"{code}TRY",
                "label": CURRENCY_LABELS.get(code, f"{code} / TL"),
                "value": round(value, 6),
                "changePercent": None,
                "note": " • ".join(note_parts) or "TCMB kuru",
            }
        )

    source_date = str(root.attrib.get("Tarih") or root.attrib.get("Date") or "").strip()
    return items, source_date


def extract_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "data", "records", "quotes"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize_custom_item(item: dict[str, Any]) -> dict[str, Any]:
    code = str(first_value(item, ("code", "symbol", "ticker", "name"))).strip()
    label = str(first_value(item, ("label", "title", "displayName", "name"), code)).strip()
    value = normalize_number(first_value(item, ("value", "price", "rate", "last", "buying", "selling")))
    raw_change = first_value(item, ("changePercent", "change", "percentChange", "dailyChange"), None)
    change = None if raw_change in (None, "") else normalize_number(raw_change)
    note = str(first_value(item, ("note", "unit", "description"))).strip()
    return {
        "code": code,
        "label": label or code or "Gösterge",
        "value": value,
        "changePercent": change,
        "note": note,
    }


def parse_rates_object(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        return []

    base = str(payload.get("base") or payload.get("base_code") or "USD").upper()
    try_rate = normalize_number(rates.get("TRY"))
    if try_rate <= 0:
        return []

    items: list[dict[str, Any]] = []
    for code in configured_codes():
        if code == base:
            value = try_rate
        else:
            cross = normalize_number(rates.get(code))
            if cross <= 0:
                continue
            value = try_rate / cross
        items.append(
            {
                "code": f"{code}TRY",
                "label": CURRENCY_LABELS.get(code, f"{code} / TL"),
                "value": round(value, 6),
                "changePercent": None,
                "note": "Güncel kur",
            }
        )
    return items


def parse_json(content: bytes) -> tuple[list[dict[str, Any]], str]:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Ekonomi JSON verisi çözümlenemedi.") from exc

    if isinstance(payload, dict):
        rate_items = parse_rates_object(payload)
        if rate_items:
            source_date = str(payload.get("date") or payload.get("time_last_update_utc") or "")
            return rate_items, source_date

    items = [normalize_custom_item(item) for item in extract_list(payload)]
    items = [item for item in items if item["code"] or item["label"]]
    source_date = str(payload.get("date") or payload.get("lastUpdated") or "") if isinstance(payload, dict) else ""
    return items, source_date


def parse_payload(content: bytes, content_type: str, source_url: str) -> tuple[list[dict[str, Any]], str, str]:
    stripped = content.lstrip()
    is_xml = "xml" in content_type.lower() or stripped.startswith(b"<")
    if is_xml:
        items, source_date = parse_tcmb_xml(content)
        source_label = "Türkiye Cumhuriyet Merkez Bankası (TCMB)"
    else:
        items, source_date = parse_json(content)
        source_label = "Yapılandırılmış ekonomi veri kaynağı"

    if not items:
        raise RuntimeError("Ekonomi veri kaynağı geçerli gösterge döndürmedi.")
    return items, source_date, source_label


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


def sync_economy(db) -> None:
    custom_url = env("ECONOMY_FEED_URL")
    source_url = custom_url or DEFAULT_TCBM_URL
    token = env("ECONOMY_FEED_TOKEN") if custom_url else ""

    content, content_type = request_content(source_url, token)
    items, source_date, source_label = parse_payload(content, content_type, source_url)

    snapshot = {
        "items": items[:10],
        "lastUpdated": now_iso(),
        "source": "CUSTOM_FEED" if custom_url else "TCMB",
        "sourceLabel": source_label,
        "sourceDate": source_date,
    }
    db.collection("system_public").document("economy").set(snapshot)
    print(f"{len(snapshot['items'])} ekonomi göstergesi Firestore'a yazıldı. Kaynak: {source_label}")


def main() -> int:
    db = firestore_client()
    if db is None:
        print("FIREBASE_SERVICE_ACCOUNT_JSON tanımlı değil; ekonomi senkronizasyonu atlandı.")
        return 0

    sync_economy(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
