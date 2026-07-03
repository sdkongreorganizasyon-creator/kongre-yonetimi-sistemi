from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

try:
    from google import genai
except Exception:  # The app should still open if the dependency is missing.
    genai = None


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


def _make_prompt(payload: dict) -> str:
    prompt_key = payload.get("promptKey", "ai")
    admin_prompt = (payload.get("adminPrompt") or "").strip()

    if prompt_key == "simulation":
        body = f"""
Sen bir organizasyon yönetimi, bütçe, risk ve operasyon analizi asistanısın.
Aşağıdaki senaryoyu Türkçe, kurumsal ve uygulanabilir şekilde analiz et.

Admin promptu:
{admin_prompt or "Admin promptu girilmemiş."}

Bağlı etkinlik: {payload.get("organization") or "Manuel / Genel"}
Senaryo: {payload.get("scenario") or "Senaryo girilmedi"}
Planlanan gelir: {payload.get("plannedRevenue", 0)}
Planlanan gider: {payload.get("plannedExpense", 0)}
Değişim / oran / fark: {payload.get("change", 0)}
Mevcut katılımcı: {payload.get("currentParticipants", 0)}
Yeni katılımcı: {payload.get("newParticipants", 0)}
Ek not: {payload.get("notes") or "Ek not yok."}

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
    if not isinstance(value, dict) or value.get("type") != "ai_request":
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
)
_handle_component_value(component_value)

