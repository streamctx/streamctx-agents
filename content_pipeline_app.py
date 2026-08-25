"""Standalone Streamlit page for the Marketing Agent Content Pipeline board.

Tracking UI only. Does not load or change the marketing agent.
Run:  streamlit run content_pipeline_app.py
The same board is also a tab in dashboard.py.
"""

from __future__ import annotations

import streamlit as st

from content_pipeline import render_pipeline_tab

st.set_page_config(
    page_title="Marketing Agent Content Pipeline",
    layout="wide",
    page_icon="📋",
)
render_pipeline_tab(st)
