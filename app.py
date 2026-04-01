"""
app.py  –  Streamlit web UI for SkipBin Price Checker
Deploy to Streamlit Community Cloud (free) from GitHub.
"""

import queue
import threading
import time

import pandas as pd
import streamlit as st

import Bookabin
import Bestpriceskipbins as BPSB
import Skipbinfinder as SBF
import Skipbinsonline as SBO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_df(results: dict, waste_types_map: dict, all_sizes: list) -> pd.DataFrame:
    rows = []
    for wt in waste_types_map:
        row = {"Waste Type": wt}
        for s in all_sizes:
            price = results.get(wt, {}).get(s)
            if isinstance(price, (int, float)):
                row[f"{s} m³"] = f"${price:,.0f}"
            else:
                row[f"{s} m³"] = "N/A"
        rows.append(row)
    return pd.DataFrame(rows).set_index("Waste Type")


def _parse_bpsb_date(d_slash: str) -> str:
    """Convert D/MM/YYYY → dd-MM-yyyy for BestPriceSkipBins."""
    parts = d_slash.strip().split("/")
    if len(parts) != 3:
        return d_slash
    day, month, year = parts
    return f"{int(day):02d}-{int(month):02d}-{year}"


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SkipBin Price Checker",
    page_icon="🗑️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Sidebar – Navigation
# ---------------------------------------------------------------------------

if "page" not in st.session_state:
    st.session_state.page = "Home"

def _go_home():
    st.session_state.page = "Home"
    st.session_state.supplier_nav = None

with st.sidebar:
    st.title("🗑️ SkipBin Tool")
    st.markdown("---")

    st.button("🏠  Home", use_container_width=True, on_click=_go_home)

    st.markdown("---")
    st.caption("Suppliers Action")

    supplier_nav = st.radio(
        "supplier_nav",
        options=["BookABin", "BestPriceSkipBins", "SkipBinFinder", "SkipBinsOnline"],
        label_visibility="collapsed",
        format_func=lambda x: f"— {x}",
        index=None,
        key="supplier_nav",
    )
    if supplier_nav:
        st.session_state.page = supplier_nav

page = st.session_state.page

# ===========================================================================
# PAGE: Home
# ===========================================================================

if page == "Home":
    st.title("🗑️ SkipBin Price Checker")
    st.caption("BookABin · BestPriceSkipBins · SkipBinFinder · SkipBinsOnline — one search, four sources")
    st.markdown("---")

    col1, col2, col3, col4 = st.columns([1, 1.4, 1.4, 1])
    with col1:
        postcode = st.text_input("Postcode", value="3173")
    with col2:
        dod = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026")
    with col3:
        pud = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026")
    with col4:
        st.write("")
        st.write("")
        search = st.button("🔍 Search All", use_container_width=True, type="primary")

    if search:
        if not postcode or not dod or not pud:
            st.error("Please fill in all fields.")
            st.stop()

        bpsb_dod = _parse_bpsb_date(dod)
        bpsb_pud = _parse_bpsb_date(pud)

        results_store = {}
        status_store  = {}
        lock = threading.Lock()

        def _run(key, run_fn, *args):
            cell_q   = queue.Queue()
            status_q = queue.Queue()
            done     = threading.Event()
            threading.Thread(target=run_fn, args=(*args, cell_q, status_q, done), daemon=True).start()
            done.wait()
            out = {}
            while not cell_q.empty():
                wt, size, price = cell_q.get_nowait()
                out.setdefault(wt, {})[size] = price
            with lock:
                results_store[key] = out
                status_store[key]  = "done"

        threads = [
            threading.Thread(target=_run, args=("bab",  Bookabin.run_search, postcode, dod, pud),           daemon=True),
            threading.Thread(target=_run, args=("bpsb", BPSB.run_search,     postcode, bpsb_dod, bpsb_pud), daemon=True),
            threading.Thread(target=_run, args=("sbf",  SBF.run_search,      postcode, dod, pud),           daemon=True),
            threading.Thread(target=_run, args=("sbo",  SBO.run_search,      postcode, dod, pud),           daemon=True),
        ]
        for t in threads:
            t.start()

        labels = {
            "bab":  "📦 BookABin",
            "bpsb": "💰 BestPriceSkipBins",
            "sbf":  "🔍 SkipBinFinder",
            "sbo":  "🌐 SkipBinsOnline",
        }
        keys = ["bab", "bpsb", "sbf", "sbo"]

        prog = st.progress(0, text="Searching …")
        cols = st.columns(4)
        placeholders = {k: cols[i].empty() for i, k in enumerate(keys)}
        for k, ph in placeholders.items():
            ph.info(f"{labels[k]}\n\n⏳ Running …")

        while any(t.is_alive() for t in threads):
            n_done = sum(1 for k in keys if status_store.get(k) == "done")
            prog.progress(n_done / 4, text=f"Completed {n_done} / 4 sources …")
            for k, ph in placeholders.items():
                if status_store.get(k) == "done":
                    ph.success(f"{labels[k]}\n\n✅ Done")
            time.sleep(1)

        for k, ph in placeholders.items():
            if status_store.get(k) == "done":
                ph.success(f"{labels[k]}\n\n✅ Done")
            else:
                ph.error(f"{labels[k]}\n\n❌ Failed")
        prog.progress(1.0, text="All done!")

        st.success(f"✅ Done — Postcode {postcode}  |  {dod} → {pud}  |  All four sources complete.")

        tab_bab, tab_bpsb, tab_sbf, tab_sbo = st.tabs([
            "BookABin", "BestPriceSkipBins", "SkipBinFinder", "SkipBinsOnline"
        ])

        with tab_bab:
            st.caption("Prices from bookabin.com.au — cheapest available supplier.")
            bab_res = results_store.get("bab", {})
            if bab_res:
                df = _to_df(bab_res, Bookabin.WASTE_TYPES, Bookabin.ALL_SIZES)
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No data returned from BookABin.")

        with tab_bpsb:
            st.caption("Prices from bestpriceskipbins.com.au — cheapest available supplier.")
            bpsb_res = results_store.get("bpsb", {})
            if bpsb_res:
                df = _to_df(bpsb_res, BPSB.WASTE_TYPES, BPSB.ALL_SIZES)
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No data returned from BestPriceSkipBins.")

        with tab_sbf:
            st.caption("Prices from skipbinfinder.com.au — cheapest available supplier.")
            sbf_res = results_store.get("sbf", {})
            if sbf_res:
                df = _to_df(sbf_res, SBF.WASTE_TYPES, SBF.ALL_SIZES)
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No data returned from SkipBinFinder.")

        with tab_sbo:
            st.caption("Prices from skipbinsonline.com.au — bin sizes fetched live per postcode.")
            sbo_res = results_store.get("sbo", {})
            if sbo_res:
                sbo_waste = {wt: list(sizes.keys()) for wt, sizes in sbo_res.items()}
                df = _to_df(sbo_res, sbo_waste, SBO.ALL_SIZES)
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No data returned from SkipBinsOnline.")

# ===========================================================================
# PAGE: BookABin Sign In
# ===========================================================================

elif page == "BookABin":
    st.title("📦 BookABin — Supplier Sign In")
    st.markdown("Log in to your BookABin supplier account.")
    st.markdown("---")

    bab_account  = st.text_input("Account (Supplier ID)", key="bab_acc")
    bab_password = st.text_input("Password", type="password", key="bab_pwd")
    sign_in_btn  = st.button("Sign In", type="primary")

    if sign_in_btn:
        if not bab_account or not bab_password:
            st.error("Please enter your Supplier ID and password.")
        else:
            with st.spinner("Signing in to BookABin…"):
                ok, msg, screenshot = Bookabin.login(bab_account, bab_password)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
            if screenshot:
                st.image(screenshot, caption="Page after login attempt", use_container_width=True)

# ===========================================================================
# PAGE: BestPriceSkipBins Sign In
# ===========================================================================

elif page == "BestPriceSkipBins":
    st.title("💰 BestPriceSkipBins — Supplier Sign In")
    st.markdown("Log in to your BestPriceSkipBins supplier account.")
    st.markdown("---")
    st.info("Login automation for BestPriceSkipBins is not yet configured.")

# ===========================================================================
# PAGE: SkipBinFinder Sign In
# ===========================================================================

elif page == "SkipBinFinder":
    st.title("🔍 SkipBinFinder — Supplier Sign In")
    st.markdown("Log in to your SkipBinFinder supplier account.")
    st.markdown("---")
    st.info("Login automation for SkipBinFinder is not yet configured.")

# ===========================================================================
# PAGE: SkipBinsOnline Sign In
# ===========================================================================

elif page == "SkipBinsOnline":
    st.title("🌐 SkipBinsOnline — Supplier Sign In")
    st.markdown("Log in to your SkipBinsOnline supplier account.")
    st.markdown("---")
    st.info("Login automation for SkipBinsOnline is not yet configured.")
