"""Ask Pulse chat app.  Run: streamlit run app/streamlit_app.py

Needs the same SNOWFLAKE_* environment variables as the loader (or Streamlit secrets with the same names).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # Streamlit Community Cloud: secrets with the same names as the environment variables
    for k, v in st.secrets.items():
        os.environ.setdefault(k, str(v))
except Exception:  # noqa: BLE001  no secrets file locally
    pass

from askpulse import agent, snow  # noqa: E402

SEMANTIC = os.getenv("ASK_PULSE_SEMANTIC", "@ASK_PULSE.CORE.SEMANTIC_MODELS/pulse_v3.yaml")
EXAMPLES = [
    "Which language had the most page views in the latest hour?",
    "Which Telugu pages are trending right now?",
    "Rank the languages by total views over the last 24 hours.",
    "Why is the top trending Hindi page trending?",
]

st.set_page_config(page_title="Ask Pulse", page_icon="📈")
st.title("Ask Pulse")
st.caption("Ask what India is reading on Wikipedia. Answers come from live data in Snowflake "
           "(Cortex Analyst for numbers, Cortex Search for 'why').")


@st.cache_resource
def conn():
    return snow.connect()


if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.subheader("Try")
    for q in EXAMPLES:
        if st.button(q, use_container_width=True):
            st.session_state.pending = q

question = st.chat_input("Ask a question") or st.session_state.pop("pending", None)
if question:
    with st.spinner("Thinking..."):
        st.session_state.history.append(agent.ask(conn(), question, SEMANTIC))

for i, a in enumerate(st.session_state.history):
    with st.chat_message("user"):
        st.write(a.question)
    with st.chat_message("assistant"):
        st.write(a.answer)
        if a.rows:
            st.dataframe(pd.DataFrame(a.rows, columns=a.columns), hide_index=True)
        if a.sources:
            with st.expander("Sources"):
                for s in a.sources:
                    st.markdown(f"- **{s.get('PAGE_TITLE', '').replace('_', ' ')}** "
                                f"({s.get('LANGUAGE_NAME') or s.get('LANGUAGE_CODE')}): {s.get('WHY_REASON_EN')}")
        if a.sql:
            with st.expander("SQL"):
                st.code(a.sql, language="sql")
        c1, c2, _ = st.columns([1, 1, 8])
        if c1.button("👍", key=f"up{i}"):
            agent.feedback(conn(), a.trace_id, "up")
            st.toast("Thanks!")
        if c2.button("👎", key=f"down{i}"):
            agent.feedback(conn(), a.trace_id, "down")
            st.toast("Thanks, this question will be reviewed for the eval set.")
        st.caption(f"{a.route} · {a.status} · {a.latency_ms} ms")
