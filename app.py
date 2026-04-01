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

_CACHE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_BAB_CACHE     = os.path.join(_CACHE_DIR, "bab_results.json")

_DEFAULT_ACCOUNTS = [
    {"label": "Account 1", "supplier_id": "", "password": "", "postcode": "3173"},
    {"label": "Account 2", "supplier_id": "", "password": "", "postcode": "3130"},
    {"label": "Account 3", "supplier_id": "", "password": "", "postcode": "3199"},
]

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
# GitHub Gist cloud helpers  (accounts sync across machines / Streamlit Cloud)
# ---------------------------------------------------------------------------
# Requires two keys in .streamlit/secrets.toml  (or Streamlit Cloud Secrets):
#   [gist]
#   token   = "ghp_xxxxxxxxxxxxxxxxxxxx"   # GitHub PAT with gist scope
#   gist_id = "xxxxxxxxxxxxxxxxxxxxxxxx"   # ID of an existing private Gist
#
# The Gist must contain a file called  "bab_accounts.json".
# ---------------------------------------------------------------------------

import requests as _requests

_GIST_FILENAME = "bab_accounts.json"

def _gist_token() -> str | None:
    """Return GitHub token from st.secrets, or None if not configured."""
    try:
        return st.secrets["gist"]["token"]
    except Exception:
        return None

def _gist_id() -> str | None:
    """Return Gist ID from st.secrets, or None if not configured."""
    try:
        return st.secrets["gist"]["gist_id"]
    except Exception:
        return None

def _gist_load() -> list | None:
    """
    Fetch account list from the private GitHub Gist.
    Returns the parsed list, or None on any error / not configured.
    """
    token = _gist_token()
    gid   = _gist_id()
    if not token or not gid:
        return None
    try:
        resp = _requests.get(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=8,
        )
        resp.raise_for_status()
        content = resp.json()["files"][_GIST_FILENAME]["content"]
        data = json.loads(content)
        return data if isinstance(data, list) else None
    except Exception:
        return None

def _gist_save(accounts: list) -> bool:
    """
    Write account list to the private GitHub Gist.
    Returns True on success, False on failure / not configured.
    """
    token = _gist_token()
    gid   = _gist_id()
    if not token or not gid:
        return False
    try:
        resp = _requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"files": {_GIST_FILENAME: {"content": json.dumps(accounts, indent=2)}}},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False


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

# Restore saved accounts — Gist only (no local cache to avoid conflicts)
if "bab_accounts" not in st.session_state:
    _acc = _gist_load()
    # Normalise: ensure exactly 3 well-formed account dicts regardless of what
    # the Gist returns (empty list, partial list, wrong types, etc.)
    if not isinstance(_acc, list):
        _acc = []
    _acc = [
        a if isinstance(a, dict) else {}
        for a in _acc
    ]
    # Pad to 3 entries, fill missing keys with defaults
    while len(_acc) < 3:
        _acc.append({})
    for idx, _default in enumerate(_DEFAULT_ACCOUNTS):
        for key, val in _default.items():
            _acc[idx].setdefault(key, val)
    st.session_state["bab_accounts"] = _acc[:3]

# Per-account unlock flags (which account is currently in edit mode)
if "bab_acc_unlocked" not in st.session_state:
    st.session_state["bab_acc_unlocked"] = [False, False, False]

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

    bab_tab_prices, bab_tab_signin = st.tabs(["🔍 Check Price", "� Sign In Information"])

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

            st.markdown("---")

            # ---------------------------------------------------------------
            # Update Price section
            # ---------------------------------------------------------------
            st.subheader("💲 Update Price on BookABin")
            st.caption("Log in with a saved account to update your supplier rates on rates_manage.aspx.")

            _accounts = st.session_state.get("bab_accounts", [])
            _acc_labels = [
                f"{a.get('label','?')}  (Postcode: {a.get('postcode','?')}  |  ID: {a.get('supplier_id','(not set)')})"
                for a in _accounts
            ]

            up_col1, up_col2, up_col3 = st.columns([2, 1, 1])
            with up_col1:
                _sel_acc_idx = st.selectbox(
                    "Select Account",
                    options=list(range(len(_acc_labels))),
                    format_func=lambda x: _acc_labels[x],
                    key="bab_up_acc",
                )
            with up_col2:
                st.write("")
                st.write("")
                _fetch_rates_btn = st.button("📥 Fetch Current Rates", use_container_width=True, key="bab_fetch_rates")
            with up_col3:
                st.write("")
                st.write("")
                _auto_update_btn = st.button(
                    "🤖 Auto-Update from Search Results",
                    use_container_width=True,
                    type="primary",
                    key="bab_auto_update",
                )

            # ── Auto-Update: use bab_results to set Day 1 = price - 1 ──
            if _auto_update_btn:
                _sel = _accounts[_sel_acc_idx]
                if not _sel.get("supplier_id") or not _sel.get("password"):
                    st.error("Selected account has no Supplier ID or password set. Go to the Sign In Information tab to add them.")
                else:
                    _au_done   = threading.Event()
                    _au_result = {}

                    def _do_auto_update():
                        r = Bookabin.auto_update_rates(
                            _sel["supplier_id"], _sel["password"],
                            saved_dod, bab_results,
                        )
                        _au_result.update(r)
                        _au_done.set()

                    threading.Thread(target=_do_auto_update, daemon=True).start()

                    with st.spinner(
                        f"Logging in as {_sel['supplier_id']} and updating Day 1 prices "
                        f"(search results − 1) for {saved_dod}…"
                    ):
                        _au_done.wait()

                    if _au_result.get("ok"):
                        st.success(_au_result["message"])
                    else:
                        st.error(_au_result["message"])

                    _upd = _au_result.get("updated", [])
                    _skp = _au_result.get("skipped", [])

                    if _upd:
                        import pandas as _pd
                        st.markdown("**✅ Updated rows:**")
                        st.dataframe(
                            _pd.DataFrame(_upd)[["description", "size", "old_price", "new_price"]],
                            use_container_width=True,
                        )
                    if _skp:
                        import pandas as _pd
                        with st.expander(f"⚠ {len(_skp)} row(s) skipped"):
                            st.dataframe(
                                _pd.DataFrame(_skp)[["description", "reason"]],
                                use_container_width=True,
                            )
                    if _au_result.get("raw"):
                        st.text_area("Raw page content", _au_result["raw"], height=200)
                    if _au_result.get("screenshot"):
                        st.image(_au_result["screenshot"], caption="Page after auto-update", use_container_width=True)
                    # Clear stale manual fetch cache after auto-update
                    st.session_state.pop("bab_rates_result", None)

            # ── Manual: Fetch → edit fields → submit ──
            if _fetch_rates_btn:
                _sel = _accounts[_sel_acc_idx]
                if not _sel.get("supplier_id") or not _sel.get("password"):
                    st.error("Selected account has no Supplier ID or password set. Go to the Sign In Information tab to add them.")
                else:
                    with st.spinner(f"Logging in as {_sel['supplier_id']} and fetching rates for {saved_dod}…"):
                        _rates = Bookabin.get_rates(_sel["supplier_id"], _sel["password"], saved_dod)
                    st.session_state["bab_rates_result"]  = _rates
                    st.session_state["bab_rates_acc_idx"] = _sel_acc_idx

            if "bab_rates_result" in st.session_state:
                _rates = st.session_state["bab_rates_result"]

                if not _rates["ok"]:
                    st.error(_rates["message"])
                    if _rates.get("raw"):
                        st.text(_rates["raw"])
                else:
                    st.success(_rates["message"])

                    # Show column headers if available
                    if _rates.get("headers"):
                        st.caption("Column headers: " + "  |  ".join(_rates["headers"]))

                    if _rates.get("rows"):
                        st.markdown("**Edit the values below, then click Update Price.**")

                        # Build an editable dict of field_id -> new value
                        _edit_updates = {}
                        for _row in _rates["rows"]:
                            # Show description + field_order hint
                            _col_hint = ""
                            if _rates.get("headers") and _row.get("field_order"):
                                # Match field_order (input fields only) to headers
                                # Headers include non-input cols; skip the first (description) header
                                _data_headers = [h for h in _rates["headers"] if h and h.lower() not in ("edit", "delete", "")]
                                _field_labels = _data_headers[1:len(_row["field_order"]) + 1] if len(_data_headers) > 1 else []
                                if _field_labels:
                                    _col_hint = "  ·  Columns: " + ", ".join(_field_labels)
                            st.markdown(f"**{_row['description']}**{_col_hint}")
                            _field_order = _row.get("field_order", list(_row["fields"].keys()))
                            _fcols = st.columns(min(len(_field_order), 5))
                            for _ci, _fid in enumerate(_field_order):
                                _fval = _row["fields"].get(_fid, "")
                                # Label: use matching header if available, else last segment of ID
                                _data_headers = [h for h in _rates.get("headers", []) if h and h.lower() not in ("edit", "delete", "")]
                                _label = (
                                    _data_headers[_ci + 1]
                                    if len(_data_headers) > _ci + 1
                                    else (_fid.split("_")[-1] if "_" in _fid else _fid)
                                )
                                with _fcols[_ci % len(_fcols)]:
                                    _new_val = st.text_input(
                                        _label,
                                        value=_fval,
                                        key=f"bab_rate_field_{_fid}",
                                    )
                                    if _new_val != _fval:
                                        _edit_updates[_fid] = _new_val

                        st.write("")
                        _up_col1, _up_col2 = st.columns([3, 1])
                        with _up_col1:
                            if _edit_updates:
                                st.info(f"🖊 {len(_edit_updates)} field(s) changed — ready to submit.")
                            else:
                                st.caption("No changes made yet.")
                        with _up_col2:
                            _submit_rates_btn = st.button(
                                "💾 Update Price",
                                use_container_width=True,
                                type="primary",
                                key="bab_submit_rates",
                                disabled=len(_edit_updates) == 0,
                            )

                        if _submit_rates_btn and _edit_updates:
                            _sel = _accounts[st.session_state.get("bab_rates_acc_idx", 0)]
                            _up_done = threading.Event()
                            _up_result = {}

                            def _do_update():
                                r = Bookabin.update_rates(
                                    _sel["supplier_id"], _sel["password"],
                                    saved_dod, _edit_updates,
                                )
                                _up_result.update(r)
                                _up_done.set()

                            threading.Thread(target=_do_update, daemon=True).start()

                            with st.spinner("Updating prices on BookABin…"):
                                _up_done.wait()

                            if _up_result.get("ok"):
                                st.success(_up_result["message"])
                            else:
                                st.error(_up_result["message"])
                            if _up_result.get("screenshot"):
                                st.image(_up_result["screenshot"], caption="Page after update", use_container_width=True)
                            # Clear cached rates so next fetch is fresh
                            st.session_state.pop("bab_rates_result", None)

                    elif _rates.get("raw"):
                        st.text_area("Raw page content", _rates["raw"], height=200)

    # -----------------------------------------------------------------------
    # Sub-tab: Sign In Information  (3 saved accounts, passwords locked)
    # -----------------------------------------------------------------------
    with bab_tab_signin:
        st.subheader("🔐 Sign In Information — bookabin.com.au")
        st.caption("Saved credentials for up to 3 BookABin supplier accounts. Passwords are hidden and protected.")
        st.markdown("---")

        accounts  = st.session_state["bab_accounts"]        # list of 3 dicts
        unlocked  = st.session_state["bab_acc_unlocked"]    # list of 3 bools

        for i in range(3):
            acc = accounts[i]
            with st.expander(
                f"**{acc['label']}**  —  Postcode: `{acc.get('postcode') or '(not set)'}`  |  Supplier ID: `{acc['supplier_id'] or '(not set)'}`",
                expanded=True,
            ):

                # ── View row: Postcode + Supplier ID + masked password ──
                v1, v2, v3 = st.columns([1, 1, 1])
                with v1:
                    new_pc = st.text_input(
                        "Postcode",
                        value=acc.get("postcode", ""),
                        key=f"bab_postcode_{i}",
                    )
                with v2:
                    new_id = st.text_input(
                        "Supplier ID",
                        value=acc["supplier_id"],
                        key=f"bab_sid_{i}",
                    )
                with v3:
                    # Always show masked placeholder; real value never printed
                    masked = "••••••••" if acc["password"] else "(not set)"
                    st.text_input(
                        "Password",
                        value=masked,
                        disabled=True,
                        key=f"bab_pwd_display_{i}",
                    )

                # Save Postcode or Supplier ID change immediately (no unlock needed)
                _changed = False
                if new_pc != acc.get("postcode", ""):
                    accounts[i]["postcode"] = new_pc
                    _changed = True
                if new_id != acc["supplier_id"]:
                    accounts[i]["supplier_id"] = new_id
                    _changed = True
                if _changed:
                    _gist_save(accounts)
                    st.session_state.pop("bab_accounts", None)  # force fresh Gist fetch on rerun
                    st.rerun()

                # ── Unlock / change password section ──
                if not unlocked[i]:
                    st.markdown("🔒 **Password is locked.** Enter the current password to unlock and change it.")
                    ul1, ul2 = st.columns([2, 1])
                    with ul1:
                        entered = st.text_input(
                            "Enter current password to unlock",
                            type="password",
                            key=f"bab_unlock_{i}",
                        )
                    with ul2:
                        st.write("")
                        st.write("")
                        unlock_btn = st.button("🔓 Unlock", key=f"bab_unlock_btn_{i}")

                    if unlock_btn:
                        if not acc["password"] or entered == acc["password"]:
                            # Empty password (not set yet) → allow unlock directly
                            unlocked[i] = True
                            st.session_state["bab_acc_unlocked"] = unlocked
                            st.rerun()
                        else:
                            st.error("❌ Incorrect password.")
                else:
                    # ── Edit mode: set new password ──
                    st.markdown("🔓 **Unlocked.** Set a new password below.")
                    e1, e2, e3 = st.columns([2, 2, 1])
                    with e1:
                        new_pwd = st.text_input("New Password", type="password", key=f"bab_newpwd_{i}")
                    with e2:
                        confirm_pwd = st.text_input("Confirm Password", type="password", key=f"bab_confirmpwd_{i}")
                    with e3:
                        st.write("")
                        st.write("")
                        save_btn = st.button("💾 Save", key=f"bab_savepwd_{i}", type="primary")

                    cancel_btn = st.button("🔒 Cancel & Lock", key=f"bab_cancel_{i}")

                    if save_btn:
                        if not new_pwd:
                            st.error("New password cannot be empty.")
                        elif new_pwd != confirm_pwd:
                            st.error("❌ Passwords do not match.")
                        else:
                            accounts[i]["password"] = new_pwd
                            _gist_save(accounts)
                            unlocked[i] = False
                            st.session_state["bab_acc_unlocked"] = unlocked
                            st.session_state.pop("bab_accounts", None)  # force fresh Gist fetch on rerun
                            st.success(f"✅ Password for {acc['label']} saved.")
                            st.rerun()

                    if cancel_btn:
                        unlocked[i] = False
                        st.session_state["bab_acc_unlocked"] = unlocked
                        st.session_state.pop("bab_accounts", None)  # force fresh Gist fetch on rerun
                        st.rerun()

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
