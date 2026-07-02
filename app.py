from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Kongre Yönetimi Sistemi",
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
</style>
""",
    unsafe_allow_html=True,
)

component_dir = Path(__file__).parent.resolve()
kongre_component = components.declare_component("kongre_yonetimi_sistemi", path=str(component_dir))
kongre_component(key="kongre_yonetimi_sistemi", default=None)
