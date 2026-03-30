"""
app.py  –  Streamlit web UI for SkipBin Price Checker
Deploy to Streamlit Community Cloud (free) from GitHub.

Runs all four scrapers concurrently then displays results in tabs.
"""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

import Bookabin
import Bestpriceskipbins as BPSB
import Skipbinfinder as SBF
import Skipbinsonline as SBO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect(run_fn, *args):
    """Run a scraper's run_search and collect all (wt, size, price) results."""
    cell_q   = queue.Queue()
    status_q = queue.Queue()
    done     = threading.Event()
    t = threading.Thread(
        target=run_fn,
        args=(*args, cell_q, status_q, done),
        daemon=True,
    )
    t.start()
    done.wait()
    results = {}
    while not cell_q.empty():
        wt, size, price = cell_q.get_nowait()
        results.setdefault(wt, {})[size] = price
    return results


def _to_df(results: dict, waste_types_map: dict, all_sizes: list) -> pd.DataFrame:
    rows = []
    for wt, valid_sizes in waste_types_map.items():
        row = {"Waste Type": wt}
        for s in all_sizes:
            if s not in valid_sizes:
                row[f"{s} m³"] = "—"
            else:
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

st.title("🗑️ SkipBin Price Checker")
st.caption("BookABin · BestPriceSkipBins · SkipBinFinder · SkipBinsOnline — one search, four sources")

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

if search:
    if not postcode or not dod or not pud:
        st.error("Please fill in all fields.")
        st.stop()

    bpsb_dod = _parse_bpsb_date(dod)
    bpsb_pud = _parse_bpsb_date(pud)

    status = st.empty()
    prog   = st.progress(0, text="Starting scrapers …")

    results_store = {}

    def run_bab():
        results_store["bab"] = _collect(Bookabin.run_search, postcode, dod, pud)

    def run_bpsb():
        results_store["bpsb"] = _collect(BPSB.run_search, postcode, bpsb_dod, bpsb_pud)

    def run_sbf():
        results_store["sbf"] = _collect(SBF.run_search, postcode, dod, pud)

    def run_sbo():
        results_store["sbo"] = _collect(SBO.run_search, postcode, dod, pud)

    scrapers = [
        ("BookABin",          run_bab),
        ("BestPriceSkipBins", run_bpsb),
        ("SkipBinFinder",     run_sbf),
        ("SkipBinsOnline",    run_sbo),
    ]

    done_count = [0]
    lock = threading.Lock()

    def run_with_progress(name, fn):
        fn()
        with lock:
            done_count[0] += 1
            prog.progress(done_count[0] / len(scrapers),
                          text=f"{name} done ✓  ({done_count[0]}/{len(scrapers)})")

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(run_with_progress, name, fn) for name, fn in scrapers]
        for f in futures:
            f.result()

    prog.empty()
    status.success(f"Done — Postcode {postcode}  |  {dod} → {pud}  |  All four sources complete.")

    # ── Display results ──────────────────────────────────────────────────────
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
            # SBO waste types are dynamic — build from what was returned
            sbo_waste = {wt: list(sizes.keys()) for wt, sizes in sbo_res.items()}
            all_sbo_sizes = SBO.ALL_SIZES
            df = _to_df(sbo_res, sbo_waste, all_sbo_sizes)
            st.dataframe(df, use_container_width=True)
        else:
            st.warning("No data returned from SkipBinsOnline.")
