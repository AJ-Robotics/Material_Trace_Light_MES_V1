"""
Material Tracking System - lightweight entry point
"""
import pandas as pd
import streamlit as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from database.db import db
from ui.login import LoginUI
from ui.operator import OperatorUI
from ui.dashboard import render_dashboard
from ui.reports import render_reports
from ui.config import render_configuration
from ui.admin import admin_pages, render_admin_page
from ui.mo_master import render_mo_master
from ui import theme
from services.transaction_service import TransactionService

st.set_page_config(page_title="Material Tracking System", page_icon="🏭", layout="wide")


@st.cache_resource
def init():
    db.initialize()
    return True


def sidebar_logout():
    if st.button("Logout", use_container_width=True, type="secondary"):
        st.session_state.authenticated = False
        st.session_state.user = None
        st.session_state.page = "dashboard"
        st.rerun()


def main():
    st.session_state.setdefault("authenticated", False)
    st.session_state.setdefault("user", None)
    st.session_state.setdefault("page", "dashboard")

    init()
    theme.inject()

    if not st.session_state.authenticated or not st.session_state.user:
        theme.top_bar()
        LoginUI().render()
        return

    user = st.session_state.user
    role = user["role"]
    station = user.get("station_code")

    theme.top_bar(user=user["full_name"], station=station)

    if role == "OPERATOR":
        with st.sidebar:
            st.markdown(
                f'<div style="font-weight:700;color:#1e3c72;">{user["full_name"]}</div>'
                f'<div style="font-size:12px;color:#6b7a90;">Operator · {station or "no station"}</div>',
                unsafe_allow_html=True,
            )
            st.divider()
            page = st.radio("View", ["Operator Interface", "My Transactions"], label_visibility="collapsed")
            st.divider()
            sidebar_logout()
        if page == "Operator Interface":
            OperatorUI().render()
        else:
            theme.page_header("My Transactions", "Every barcode you have recorded and every QC decision you made")
            rows = TransactionService().transactions({"operator_id": user["id"]})
            if rows:
                df = pd.DataFrame(rows)[["timestamp", "kind", "station_code", "serial_number",
                                         "mo_number", "material_code", "material_description",
                                         "material_barcode", "result", "remarks"]]
                df["material_code"] = df.apply(
                    lambda r: "QC inspection" if r["kind"] == "QC" else (r["material_code"] or "-"), axis=1)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("No transactions yet.")
        return

    if role == "VIEWER":
        menu = [("Live Dashboard", "dashboard"),
                ("Transactions", "transactions"),
                ("Exceptions", "exceptions"),
                ("Reports", "reports")]
    else:
        menu = ([("Live Dashboard", "dashboard"), ("Operator Interface", "operator")]
                + [(label, key) for key, label in admin_pages()]
                + [("Reports", "reports"), ("Configuration", "configuration")])

    with st.sidebar:
        st.markdown(
            f'<div style="font-weight:700;color:#1e3c72;">{user["full_name"]}</div>'
            f'<div style="font-size:12px;color:#6b7a90;">{role.title()}</div>',
            unsafe_allow_html=True,
        )
        st.divider()
        sel = st.radio("Menu", [m[0] for m in menu], label_visibility="collapsed")
        st.divider()
        sidebar_logout()

    page_key = next(key for label, key in menu if label == sel)
    if page_key == "operator":
        OperatorUI().render()
    elif page_key == "dashboard":
        render_dashboard()
    elif page_key == "reports":
        render_reports()
    elif page_key == "configuration":
        render_configuration()
    elif page_key == "mo_master":
        render_mo_master()
    else:
        render_admin_page(page_key)


if __name__ == "__main__":
    main()