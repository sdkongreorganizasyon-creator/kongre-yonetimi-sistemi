from pathlib import Path
from datetime import date, datetime
import json

import streamlit as st
import streamlit.components.v1 as components

try:
    from google import genai
except Exception:  # The app should still open if the dependency is missing.
    genai = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:  # Existing localStorage modules must continue to work without Firebase.
    firebase_admin = None
    credentials = None
    firestore = None


st.set_page_config(
    page_title="EVENTIX",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
html, body, .stApp, [data-testid="stAppViewContainer"] {
    width: 100vw !important;
    height: 100vh !important;
    min-height: 100dvh !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}
.block-container {
    padding: 0 !important;
    margin: 0 !important;
    max-width: 100% !important;
}
[data-testid="stHeader"], [data-testid="stToolbar"], footer, #MainMenu {
    display: none !important;
}
[data-testid="stIFrame"], iframe {
    display: block !important;
    width: 100vw !important;
    min-width: 100vw !important;
    height: 100vh !important;
    height: 100dvh !important;
    min-height: 100vh !important;
    min-height: 100dvh !important;
    border: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* Additional fullscreen hardening for Streamlit wrapper */
.stApp, .main, .block-container, section.main, div[data-testid="stVerticalBlock"], div[data-testid="stElementContainer"] {
    margin: 0 !important;
    padding: 0 !important;
    gap: 0 !important;
    border: 0 !important;
    background: transparent !important;
}
div[data-testid="stIFrame"] {
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}

/* v4 fullscreen hardening for Streamlit Cloud wrappers */
[data-testid="stAppViewContainer"],
[data-testid="stAppViewBlockContainer"],
[data-testid="stMain"],
[data-testid="stMainBlockContainer"],
section[data-testid="stSidebar"],
section.main,
main,
.stMain,
.stAppViewContainer,
.stMainBlockContainer {
    margin: 0 !important;
    padding: 0 !important;
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100vh !important;
    height: 100dvh !important;
    min-height: 100vh !important;
    min-height: 100dvh !important;
    overflow: hidden !important;
}
iframe[title="kongre_yonetimi_sistemi"] {
    width: 100vw !important;
    height: 100vh !important;
    height: 100dvh !important;
}

</style>
""",
    unsafe_allow_html=True,
)


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default) or default)
    except Exception:
        return default



def _secret_bool(name: str, default: bool = False) -> bool:
    raw = _secret(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@st.cache_resource
def _firestore_client():
    if not _secret_bool("FIREBASE_ENABLED", False):
        return None
    if firebase_admin is None or credentials is None or firestore is None:
        return None

    raw = _secret("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None

    info = json.loads(raw)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@st.cache_data(ttl=60, show_spinner=False)
def _public_data_snapshot() -> dict:
    empty = {
        "economy": {},
        "meta": {
            "firebaseEnabled": False,
            "economyLastUpdated": None,
        },
    }

    try:
        client = _firestore_client()
        if client is None:
            return empty

        economy_doc = client.collection("system_public").document("economy").get()
        economy = _json_safe(economy_doc.to_dict() or {}) if economy_doc.exists else {}

        return {
            "economy": economy,
            "meta": {
                "firebaseEnabled": True,
                "economyLastUpdated": economy.get("lastUpdated"),
                "economySource": economy.get("sourceLabel") or economy.get("source", ""),
            },
        }
    except Exception as exc:
        empty["meta"]["error"] = str(exc)
        return empty


def _handle_public_data_refresh(value: dict) -> None:
    request_id = str(value.get("requestId") or "")
    if not request_id:
        return

    processed = st.session_state.setdefault("processed_public_data_request_ids", [])
    if request_id in processed:
        return

    _public_data_snapshot.clear()
    processed.append(request_id)
    st.session_state["processed_public_data_request_ids"] = processed[-30:]
    st.session_state["public_data_response"] = {
        "requestId": request_id,
        "ok": True,
        "message": "Ekonomi verileri yenilendi." if value.get("manual") else "",
    }
    st.rerun()

def _make_prompt(payload: dict) -> str:
    prompt_key = payload.get("promptKey", "ai")
    admin_prompt = (payload.get("adminPrompt") or "").strip()

    if prompt_key == "simulation":
        body = f"""
Sen bir organizasyon yönetimi, bütçe, risk ve operasyon analizi asistanısın.
Aşağıdaki senaryoyu Türkçe, kurumsal ve uygulanabilir şekilde analiz et.

Admin promptu:
{admin_prompt or "Admin promptu girilmemiş."}

GENEL
Etkinlik adı: {payload.get("eventName") or "Belirtilmedi"}
Etkinlik türü: {payload.get("eventType") or "Belirtilmedi"}
Başlangıç tarihi: {payload.get("startDate") or "Belirtilmedi"}
Bitiş tarihi: {payload.get("endDate") or "Belirtilmedi"}
Mekân adı: {payload.get("venueName") or "Belirtilmedi"}
Şehir/İlçe: {payload.get("location") or "Belirtilmedi"}
Beklenen davetli sayısı: {payload.get("expectedGuests", 0)}
VIP katılımcı sayısı: {payload.get("vipGuests", 0)}
Görevli personel sayısı: {payload.get("staffCount", 0)}

SAHNE VE TEKNİK
Sahne kurulumu: {payload.get("stageSetup") or "Yok"}
Ses sistemi: {payload.get("soundSystem") or "Yok"}
Işık sistemi: {payload.get("lightingSystem") or "Yok"}
LED ekran: {payload.get("ledScreen") or "Yok"}

ORGANİZASYON
Hostes: {payload.get("hostess") or "Yok"}
Karşılama ekibi: {payload.get("welcomeTeam") or "Yok"}
Fotoğrafçı: {payload.get("photographer") or "Yok"}
Kameraman: {payload.get("cameraman") or "Yok"}

CATERING
Kokteyl: {payload.get("cocktail") or "Yok"}
Açık büfe: {payload.get("openBuffet") or "Yok"}
Set menü: {payload.get("setMenu") or "Yok"}
İçecek servisi: {payload.get("beverageService") or "Yok"}

ULAŞIM VE KONAKLAMA
Araç tipi: {payload.get("vehicleType") or "Belirtilmedi"}
Araç sayısı: {payload.get("vehicleCount", 0)}
Tahmini yakıt gideri: {payload.get("fuelCost", 0)}
Konaklama ihtiyacı: {payload.get("accommodation") or "Yok"}

FİNANSAL
Tahmini gelir: {payload.get("estimatedRevenue", 0)}
Tahmini gider: {payload.get("estimatedExpense", 0)}
Beklenmeyen gider oranı (%): {payload.get("unexpectedExpenseRate", 0)}

SENARYO
{payload.get("scenario") or "Senaryo girilmedi"}

Yanıt formatı:
- Kısa yönetici özeti
- Finansal etki
- Operasyonel etki
- Riskler
- Önerilen aksiyonlar
- Kontrol edilmesi gereken bilgiler
"""
    else:
        body = f"""
Sen kurumsal organizasyon yönetimi süreçleri için çalışan profesyonel bir Türkçe metin asistanısın.
Kullanıcının verdiği bilgiye göre açık, düzgün ve kullanılabilir bir taslak üret.

Admin promptu:
{admin_prompt or "Admin promptu girilmemiş."}

İşlem türü: {payload.get("mode") or "Belirtilmedi"}
Bağlı etkinlik: {payload.get("event") or "Belirtilmedi"}
Ham bilgi / toplantı notu:
{payload.get("text") or "Ek bilgi girilmedi."}

Yanıt formatı:
- Kullanıma hazır taslak metin
- Eksik/kontrol edilmesi gereken bilgiler
"""

    return body.strip()


def _call_gemini(payload: dict) -> str:
    if genai is None:
        raise RuntimeError("google-genai kütüphanesi yüklenemedi. requirements.txt içinde google-genai satırını kontrol edin.")

    api_key = _secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY bulunamadı. Streamlit Secrets alanını kontrol edin.")

    model = _secret("GEMINI_MODEL", "gemini-2.5-flash-lite")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=_make_prompt(payload))
    text = getattr(response, "text", None)
    if text:
        return text.strip()
    return str(response).strip()


def _handle_component_value(value: object) -> None:
    if not isinstance(value, dict):
        return

    if value.get("type") == "public_data_refresh":
        _handle_public_data_refresh(value)
        return

    if value.get("type") != "ai_request":
        return

    request_id = str(value.get("requestId") or "")
    if not request_id:
        return

    processed = st.session_state.setdefault("processed_ai_request_ids", [])
    if request_id in processed:
        return

    payload = value.get("payload") or {}
    target_id = value.get("targetId")
    title = payload.get("title") or ("Simülasyon Sonucu" if payload.get("promptKey") == "simulation" else "Yapay Zeka Yanıtı")

    try:
        answer = _call_gemini(payload)
        ai_response = {
            "type": "ai_response",
            "requestId": request_id,
            "targetId": target_id,
            "promptKey": payload.get("promptKey"),
            "title": title,
            "ok": True,
            "text": answer,
        }
    except Exception as exc:
        ai_response = {
            "type": "ai_response",
            "requestId": request_id,
            "targetId": target_id,
            "promptKey": payload.get("promptKey"),
            "title": title,
            "ok": False,
            "error": str(exc),
        }

    processed.append(request_id)
    st.session_state["processed_ai_request_ids"] = processed[-20:]
    st.session_state["ai_response"] = ai_response
    st.rerun()


component_dir = Path(__file__).parent.resolve()
kongre_component = components.declare_component("kongre_yonetimi_sistemi", path=str(component_dir))
component_value = kongre_component(
    key="kongre_yonetimi_sistemi",
    default=None,
    ai_response=st.session_state.get("ai_response"),
    public_data=_public_data_snapshot(),
    public_data_response=st.session_state.get("public_data_response"),
)
_handle_component_value(component_value)

