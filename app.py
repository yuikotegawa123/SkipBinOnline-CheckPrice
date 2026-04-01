"""
app.py  –  Streamlit web UI for SkipBin Price Checker
Deploy to Streamlit Community Cloud (free) from GitHub.
"""

import json
import os
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

# Full union of every size across all four scrapers, sorted numerically.
FULL_SIZES = sorted(
    set(Bookabin.ALL_SIZES) | set(BPSB.ALL_SIZES) | set(SBF.ALL_SIZES) | set(SBO.ALL_SIZES),
    key=float,
)


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
# Disk cache helpers  (survive F5 / page refresh)
# ---------------------------------------------------------------------------

_CACHE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_BAB_CACHE  = os.path.join(_CACHE_DIR, "bab_results.json")

def _save_cache(path: str, data: dict) -> None:
    """Persist arbitrary dict to a JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

def _load_cache(path: str) -> dict | None:
    """Load dict from JSON file; return None if missing or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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

# Restore disk-cached results into session_state on first run (survives F5)
if "bab_results" not in st.session_state:
    _cached = _load_cache(_BAB_CACHE)
    if _cached:
        st.session_state["bab_results"]    = _cached.get("results", {})
        st.session_state["bab_search_pc"]  = _cached.get("pc", "")
        st.session_state["bab_search_dod"] = _cached.get("dod", "")
        st.session_state["bab_search_pud"] = _cached.get("pud", "")

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
                st.subheader("Available Sizes")
                st.dataframe(_to_df(bab_res, Bookabin.WASTE_TYPES, Bookabin.ALL_SIZES), use_container_width=True)
                st.subheader("Full Range")
                st.dataframe(_to_df(bab_res, Bookabin.WASTE_TYPES, FULL_SIZES), use_container_width=True)
            else:
                st.warning("No data returned from BookABin.")

        with tab_bpsb:
            st.caption("Prices from bestpriceskipbins.com.au — cheapest available supplier.")
            bpsb_res = results_store.get("bpsb", {})
            if bpsb_res:
                st.subheader("Available Sizes")
                st.dataframe(_to_df(bpsb_res, BPSB.WASTE_TYPES, BPSB.ALL_SIZES), use_container_width=True)
                st.subheader("Full Range")
                st.dataframe(_to_df(bpsb_res, BPSB.WASTE_TYPES, FULL_SIZES), use_container_width=True)
            else:
                st.warning("No data returned from BestPriceSkipBins.")

        with tab_sbf:
            st.caption("Prices from skipbinfinder.com.au — cheapest available supplier.")
            sbf_res = results_store.get("sbf", {})
            if sbf_res:
                st.subheader("Available Sizes")
                st.dataframe(_to_df(sbf_res, SBF.WASTE_TYPES, SBF.ALL_SIZES), use_container_width=True)
                st.subheader("Full Range")
                st.dataframe(_to_df(sbf_res, SBF.WASTE_TYPES, FULL_SIZES), use_container_width=True)
            else:
                st.warning("No data returned from SkipBinFinder.")

        with tab_sbo:
            st.caption("Prices from skipbinsonline.com.au — bin sizes fetched live per postcode.")
            sbo_res = results_store.get("sbo", {})
            if sbo_res:
                sbo_waste = {wt: list(sizes.keys()) for wt, sizes in sbo_res.items()}
                st.subheader("Available Sizes")
                st.dataframe(_to_df(sbo_res, sbo_waste, SBO.ALL_SIZES), use_container_width=True)
                st.subheader("Full Range")
                st.dataframe(_to_df(sbo_res, sbo_waste, FULL_SIZES), use_container_width=True)
            else:
                st.warning("No data returned from SkipBinsOnline.")

# ===========================================================================
# PAGE: BookABin
# ===========================================================================

elif page == "BookABin":
    st.title("📦 BookABin")
    st.markdown("---")

    bab_tab_prices, bab_tab_signin = st.tabs(["🔍 Check Price", "🔑 Supplier Sign In"])

    # -----------------------------------------------------------------------
    # Sub-tab: Check Price (BookABin only)
    # -----------------------------------------------------------------------
    with bab_tab_prices:
        st.subheader("Check Prices — bookabin.com.au")
        st.caption("Search live prices from BookABin for your postcode and hire period.")

        bab_col1, bab_col2, bab_col3, bab_col4 = st.columns([1, 1.4, 1.4, 1])
        with bab_col1:
            bab_postcode = st.text_input("Postcode", value="3173", key="bab_pc")
        with bab_col2:
            bab_dod = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026", key="bab_dod")
        with bab_col3:
            bab_pud = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026", key="bab_pud")
        with bab_col4:
            st.write("")
            st.write("")
            bab_search = st.button("🔍 Search BookABin", use_container_width=True, type="primary", key="bab_search")

        if bab_search:
            if not bab_postcode or not bab_dod or not bab_pud:
                st.error("Please fill in all fields.")
                st.stop()

            def _run_bab(run_fn, *args):
                cell_q   = queue.Queue()
                status_q = queue.Queue()
                done     = threading.Event()
                threading.Thread(target=run_fn, args=(*args, cell_q, status_q, done), daemon=True).start()
                done.wait()
                out = {}
                while not cell_q.empty():
                    wt, size, price = cell_q.get_nowait()
                    out.setdefault(wt, {})[size] = price
                return out

            with st.spinner("🔍 Fetching prices from BookABin… (this may take a minute)"):
                _fetched = _run_bab(Bookabin.run_search, bab_postcode, bab_dod, bab_pud)

            if not _fetched:
                st.warning("No data returned from BookABin. Check the postcode and dates, then try again.")
                st.session_state.pop("bab_results", None)
            else:
                # ── Persist results so they survive tab switches AND F5 ──
                st.session_state["bab_results"]    = _fetched
                st.session_state["bab_search_pc"]  = bab_postcode
                st.session_state["bab_search_dod"] = bab_dod
                st.session_state["bab_search_pud"] = bab_pud
                _save_cache(_BAB_CACHE, {
                    "results": _fetched,
                    "pc":      bab_postcode,
                    "dod":     bab_dod,
                    "pud":     bab_pud,
                })

        # ── Render from session_state (persists across tab switches) ──
        if "bab_results" in st.session_state:
            bab_results  = st.session_state["bab_results"]
            saved_pc  = st.session_state.get("bab_search_pc", "")
            saved_dod = st.session_state.get("bab_search_dod", "")
            saved_pud = st.session_state.get("bab_search_pud", "")

            st.success(
                f"✅ Done — Postcode **{saved_pc}**  |  {saved_dod} → {saved_pud}  |  BookABin prices loaded."
            )

            # --- Cheapest price highlight ---
            st.subheader("💰 Cheapest Available Price")
            cheapest_price = None
            cheapest_wt    = None
            cheapest_size  = None
            for wt, sizes in bab_results.items():
                for sz, pr in sizes.items():
                    if isinstance(pr, (int, float)):
                        if cheapest_price is None or pr < cheapest_price:
                            cheapest_price = pr
                            cheapest_wt    = wt
                            cheapest_size  = sz

            if cheapest_price is not None:
                ch_col1, ch_col2, ch_col3 = st.columns(3)
                ch_col1.metric("Waste Type", cheapest_wt)
                ch_col2.metric("Bin Size",   f"{cheapest_size} m³")
                ch_col3.metric("Best Price", f"${cheapest_price:,.0f}")
            else:
                st.info("No priced results found for this postcode / date range.")

            st.markdown("---")

            # --- Full data tables ---
            st.subheader("📋 All Available Sizes")
            st.dataframe(
                _to_df(bab_results, Bookabin.WASTE_TYPES, Bookabin.ALL_SIZES),
                use_container_width=True,
            )

            st.subheader("📋 Full Size Range")
            st.dataframe(
                _to_df(bab_results, Bookabin.WASTE_TYPES, FULL_SIZES),
                use_container_width=True,
            )

    # -----------------------------------------------------------------------
    # Sub-tab: Supplier Sign In
    # -----------------------------------------------------------------------
    with bab_tab_signin:
        st.subheader("Supplier Sign In — bookabin.com.au")
        st.markdown("Log in to your BookABin supplier account.")

        bab_account  = st.text_input("Account (Supplier ID)", key="bab_acc")
        bab_password = st.text_input("Password", type="password", key="bab_pwd")
        sign_in_btn  = st.button("Sign In", type="primary", key="bab_signin_btn")

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
