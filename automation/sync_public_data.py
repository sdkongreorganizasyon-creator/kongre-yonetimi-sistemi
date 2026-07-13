"""EVENTIX public-data synchronizer.

This script is designed for GitHub Actions. It reads authorized JSON feeds,
normalizes them, and writes only the new EKAP/economy collections in Firestore.
Existing EVENTIX collections and localStorage records are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ModuleNotFoundError:
    firebase_admin = None
    credentials = None
    firestore = None

ISTANBUL = ZoneInfo("Europe/Istanbul")


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_istanbul() -> date:
    return datetime.now(ISTANBUL).date()


def first_value(source: dict[str, Any], keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def normalize_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    raw = str(value).strip()
    if not raw:
        return ""

    # ISO date/datetime.
    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if iso_match:
        return "-".join(iso_match.groups())

    # Common Turkish date format.
    tr_match = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})", raw)
    if tr_match:
        day, month, year = tr_match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    return raw[:10]


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


def request_json(url: str, token: str = "") -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "EVENTIX-Public-Data-Sync/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=45) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Veri kaynağı okunamadı: {exc}") from exc


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "data", "records", "tenders", "rates"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def suitability_keywords() -> list[str]:
    default = (
        "kongre,organizasyon,etkinlik,konferans,sempozyum,zırve,zirve,toplantı,"
        "lansman,eğitim,seminer,festival,fuar,sahne,ses sistemi,ışık sistemi,"
        "led ekran,catering,karşılama,hostes,transfer,konaklama"
    )
    return [item.strip().casefold() for item in env("EKAP_KEYWORDS", default).split(",") if item.strip()]


def normalize_tender(item: dict[str, Any], keywords: list[str]) -> dict[str, Any]:
    ekap_no = str(first_value(item, ("ekapNo", "ekap_no", "EKAP No", "ihaleNo", "tenderNo", "id"))).strip()
    institution = str(first_value(item, ("institution", "kurum", "idare", "authority", "buyer"))).strip()
    tender_name = str(first_value(item, ("tenderName", "ihaleAdi", "ihale_adı", "title", "name"))).strip()
    description = str(first_value(item, ("description", "aciklama", "açıklama", "details", "category"))).strip()
    publication_date = normalize_date(first_value(item, ("publicationDate", "yayinTarihi", "yayınTarihi", "publishedAt", "date")))
    deadline = normalize_date(first_value(item, ("deadline", "sonBasvuruTarihi", "sonBaşvuruTarihi", "closingDate", "endDate")))
    estimated_cost = normalize_number(first_value(item, ("estimatedCost", "yaklasikMaliyet", "yaklaşıkMaliyet", "amount", "budget")))
    status = str(first_value(item, ("status", "durum"), "Yayında")).strip() or "Yayında"
    source_url = str(first_value(item, ("sourceUrl", "url", "link"))).strip()

    haystack = " ".join((tender_name, description, institution)).casefold()
    matched = [keyword for keyword in keywords if keyword in haystack]
    suitable = bool(matched)
    suitability = "Uygun" if suitable else "Uygun Değil"

    if deadline:
        try:
            days_left = (date.fromisoformat(deadline) - today_istanbul()).days
            if 0 <= days_left <= 7 and status == "Yayında":
                status = "Yaklaşan Son Tarih"
        except ValueError:
            pass

    stable_source = ekap_no or source_url or f"{institution}|{tender_name}|{deadline}"
    doc_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:40]

    return {
        "id": doc_id,
        "no": ekap_no or f"AUTO-{doc_id[:10].upper()}",
        "ekapNo": ekap_no,
        "institution": institution,
        "tenderName": tender_name,
        "publicationDate": publication_date,
        "deadline": deadline,
        "estimatedCost": estimated_cost,
        "status": status,
        "suitable": suitable,
        "suitability": suitability,
        "matchedKeywords": matched,
        "sourceUrl": source_url,
        "description": description,
        "source": "AUTO_EKAP",
        "syncedAt": now_iso(),
        "updatedAt": now_iso(),
        "isDeleted": False,
    }


def normalize_economy_item(item: dict[str, Any]) -> dict[str, Any]:
    code = str(first_value(item, ("code", "symbol", "ticker", "name"))).strip()
    label = str(first_value(item, ("label", "title", "displayName", "name"), code)).strip()
    value = normalize_number(first_value(item, ("value", "price", "rate", "last", "buying")))
    change = normalize_number(first_value(item, ("changePercent", "change", "percentChange", "dailyChange")))
    note = str(first_value(item, ("note", "unit", "description"))).strip()
    return {
        "code": code,
        "label": label or code or "Gösterge",
        "value": value,
        "changePercent": change,
        "note": note,
    }


def firestore_client():
    raw = env("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    if firebase_admin is None or credentials is None or firestore is None:
        raise RuntimeError("firebase-admin paketi yüklü değil. automation/requirements.txt dosyasını kurun.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON geçerli JSON değil.") from exc
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def sync_tenders(db) -> None:
    url = env("EKAP_FEED_URL")
    if not url:
        print("EKAP_FEED_URL tanımlı değil; ihale taraması güvenli biçimde atlandı.")
        return

    payload = request_json(url, env("EKAP_FEED_TOKEN"))
    rows = [normalize_tender(item, suitability_keywords()) for item in extract_items(payload)]
    rows = [row for row in rows if row["tenderName"] or row["ekapNo"]]

    batch = db.batch()
    for row in rows:
        reference = db.collection("ekap_tenders").document(row["id"])
        batch.set(reference, row, merge=True)
    if rows:
        batch.commit()

    today_key = today_istanbul().isoformat()
    status = {
        "lastScanAt": now_iso(),
        "totalFetched": len(rows),
        "publishedToday": sum(1 for row in rows if row["publicationDate"] == today_key),
        "suitableCount": sum(1 for row in rows if row["suitable"]),
        "message": "Yetkili veri kaynağı tarandı.",
    }
    db.collection("system_public").document("ekap_status").set(status, merge=True)
    print(f"{len(rows)} ihale kaydı işlendi.")


def sync_economy(db) -> None:
    url = env("ECONOMY_FEED_URL")
    if not url:
        print("ECONOMY_FEED_URL tanımlı değil; ekonomi güncellemesi güvenli biçimde atlandı.")
        return

    payload = request_json(url, env("ECONOMY_FEED_TOKEN"))
    items = [normalize_economy_item(item) for item in extract_items(payload)]
    items = [item for item in items if item["code"] or item["label"]]
    snapshot = {
        "items": items[:10],
        "lastUpdated": now_iso(),
        "source": "AUTHORIZED_FEED",
    }
    db.collection("system_public").document("economy").set(snapshot, merge=True)
    print(f"{len(snapshot['items'])} ekonomi göstergesi işlendi.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("all", "tenders", "economy"), default="all")
    args = parser.parse_args()

    db = firestore_client()
    if db is None:
        print("FIREBASE_SERVICE_ACCOUNT_JSON tanımlı değil; senkronizasyon atlandı.")
        return 0

    if args.mode in ("all", "tenders"):
        sync_tenders(db)
    if args.mode in ("all", "economy"):
        sync_economy(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
