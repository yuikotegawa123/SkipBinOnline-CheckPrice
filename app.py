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


def _dod_to_min_date(d_slash: str) -> str:
    """Convert D/MM/YYYY → YYYY-MM-DD for use in BPSB rates URL min_date param."""
    parts = d_slash.strip().split("/")
    if len(parts) != 3:
        return ""
    day, month, year = parts
    return f"{year}-{int(month):02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
# Disk cache helpers  (survive F5 / page refresh)
# ---------------------------------------------------------------------------

_CACHE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_BAB_CACHE     = os.path.join(_CACHE_DIR, "bab_results.json")
_BPSB_CACHE    = os.path.join(_CACHE_DIR, "bpsb_results.json")

_DEFAULT_ACCOUNTS = [
    {"label": "Account 1", "supplier_id": "", "password": "", "postcode": "3173"},
    {"label": "Account 2", "supplier_id": "", "password": "", "postcode": "3130"},
    {"label": "Account 3", "supplier_id": "", "password": "", "postcode": "3199"},
]

_DEFAULT_BPSB_ACCOUNTS = [
    {"label": "Account 1", "username": "", "password": "", "postcode": "3173"},
    {"label": "Account 2", "username": "", "password": "", "postcode": "3130"},
    {"label": "Account 3", "username": "", "password": "", "postcode": "3199"},
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

if "bpsb_results" not in st.session_state:
    _cached_bpsb = _load_cache(_BPSB_CACHE)
    if _cached_bpsb:
        st.session_state["bpsb_results"]    = _cached_bpsb.get("results", {})
        st.session_state["bpsb_search_pc"]  = _cached_bpsb.get("pc", "")
        st.session_state["bpsb_search_dod"] = _cached_bpsb.get("dod", "")
        st.session_state["bpsb_search_pud"] = _cached_bpsb.get("pud", "")

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

# BPSB accounts (stored locally only, no Gist)
if "bpsb_accounts" not in st.session_state:
    _bpsb_acc = _load_cache(os.path.join(_CACHE_DIR, "bpsb_accounts.json"))
    if not isinstance(_bpsb_acc, list):
        _bpsb_acc = []
    _bpsb_acc = [a if isinstance(a, dict) else {} for a in _bpsb_acc]
    while len(_bpsb_acc) < 3:
        _bpsb_acc.append({})
    for _idx, _def in enumerate(_DEFAULT_BPSB_ACCOUNTS):
        for _k, _v in _def.items():
            _bpsb_acc[_idx].setdefault(_k, _v)
    st.session_state["bpsb_accounts"] = _bpsb_acc[:3]

if "bpsb_acc_unlocked" not in st.session_state:
    st.session_state["bpsb_acc_unlocked"] = [False, False, False]

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
            st.subheader("💲 Update Price")
            st.caption("Rule: bin sizes **≤ 7.5 m³** → price − 1  |  bin sizes **> 7.5 m³** → unchanged")

            # Build price_map: (waste_type, size) → will_set_to
            # Rule: sizes <= 7.5 m³ → price - 1; sizes > 7.5 m³ → same price
            _price_map = {}   # (wt, sz) → will_set_to
            for _wt, _sizes in bab_results.items():
                for _sz, _pr in _sizes.items():
                    if isinstance(_pr, (int, float)):
                        try:
                            _sz_f = float(_sz)
                        except (ValueError, TypeError):
                            _sz_f = 0.0
                        _adj = int(_pr) - 1 if _sz_f <= 7.5 else int(_pr)
                        _key = (_wt, _sz)
                        if _key not in _price_map or _adj < _price_map[_key]:
                            _price_map[_key] = _adj

            if _price_map:
                # Ordered waste types and sizes matching All Available Sizes table
                _update_wts  = [wt for wt in Bookabin.WASTE_TYPES if any(k[0] == wt for k in _price_map)]
                _update_szs  = [sz for sz in Bookabin.ALL_SIZES if any(k[1] == sz for k in _price_map)]
                _preview_rows = []
                for _wt in _update_wts:
                    _row = {"Waste Type": _wt}
                    for _sz in _update_szs:
                        _val = _price_map.get((_wt, _sz))
                        _row[f"{_sz} m³"] = f"${_val:,.0f}" if _val is not None else "N/A"
                    _preview_rows.append(_row)
                _df_preview = pd.DataFrame(_preview_rows).set_index("Waste Type")
                st.dataframe(_df_preview, use_container_width=True)

                # Copy / Download buttons
                _copy_col, _dl_col = st.columns([1, 1])
                with _copy_col:
                    _csv_text = _df_preview.to_csv(index=True)
                    st.download_button(
                        "📋 Download as CSV",
                        data=_csv_text,
                        file_name="update_price.csv",
                        mime="text/csv",
                        use_container_width=True,
                        key="bab_dl_csv",
                    )
                with _dl_col:
                    # Build JSON: { "wasteType": { "_url": "...", "size": price, ... }, ... }
                    import json as _json
                    import urllib.parse as _urlparse
                    _waste_urls = {
                        'General Waste':       'https://www.bookabin.com.au/supplier/rates_manage.aspx',
                        'Mixed Heavy Waste':   'https://www.bookabin.com.au/supplier/rates_manage_mixedheavy.aspx',
                        'Cleanfill/Hardfill':  'https://www.bookabin.com.au/supplier/rates_manage_clean.aspx',
                        'Green Garden Waste':  'https://www.bookabin.com.au/supplier/rates_manage_green.aspx',
                        'Soil / Dirt':         'https://www.bookabin.com.au/supplier/rates_manage_dirt.aspx',
                    }
                    _json_data = {}
                    for _wt2 in _update_wts:
                        _json_data[_wt2] = {}
                        _base_url = _waste_urls.get(_wt2, '')
                        if _base_url and bab_dod:
                            _json_data[_wt2]['_url'] = _base_url + '?fromdate=' + _urlparse.quote(bab_dod, safe='')
                        for _sz2 in _update_szs:
                            _val2 = _price_map.get((_wt2, _sz2))
                            if _val2 is not None:
                                _json_data[_wt2][_sz2] = _val2
                    _json_text = _json.dumps(_json_data, indent=2)
                    _copy_js = f"""
                    <textarea id="_cp_buf" style="position:absolute;left:-9999px">{_json_text}</textarea>
                    <button onclick="var t=document.getElementById('_cp_buf');t.select();document.execCommand('copy');this.innerText='✅ Copied!';"
                        style="width:100%;padding:0.4rem 0.8rem;background:#ff4b4b;color:white;border:none;border-radius:4px;cursor:pointer;font-size:0.9rem;">
                        📋 Copy as JSON
                    </button>"""
                    st.components.v1.html(_copy_js, height=42)
            else:
                st.info("Run a search first to populate the price map.")

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
# PAGE: BestPriceSkipBins
# ===========================================================================

elif page == "BestPriceSkipBins":
    st.title("💰 BestPriceSkipBins")
    st.markdown("---")

    bpsb_tab_prices, bpsb_tab_signin, bpsb_tab_rates = st.tabs(["🔍 Check Price", "🔐 Sign In Information", "✏️ Update Rates"])

    # -----------------------------------------------------------------------
    # Sub-tab: Check Price
    # -----------------------------------------------------------------------
    with bpsb_tab_prices:
        st.subheader("Check Prices — bestpriceskipbins.com.au")
        st.caption("Search live prices from BestPriceSkipBins for your postcode and hire period.")

        bpsb_col1, bpsb_col2, bpsb_col3, bpsb_col4 = st.columns([1, 1.4, 1.4, 1])
        with bpsb_col1:
            bpsb_postcode = st.text_input("Postcode", value="3173", key="bpsb_pc")
        with bpsb_col2:
            bpsb_dod_raw = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026", key="bpsb_dod")
        with bpsb_col3:
            bpsb_pud_raw = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026", key="bpsb_pud")
        with bpsb_col4:
            st.write("")
            st.write("")
            bpsb_search = st.button("🔍 Search BestPriceSkipBins", use_container_width=True, type="primary", key="bpsb_search")

        if bpsb_search:
            if not bpsb_postcode or not bpsb_dod_raw or not bpsb_pud_raw:
                st.error("Please fill in all fields.")
                st.stop()

            bpsb_dod = _parse_bpsb_date(bpsb_dod_raw)
            bpsb_pud = _parse_bpsb_date(bpsb_pud_raw)

            def _run_bpsb(run_fn, *args):
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

            with st.spinner("🔍 Fetching prices from BestPriceSkipBins… (this may take a minute)"):
                _fetched = _run_bpsb(BPSB.run_search, bpsb_postcode, bpsb_dod, bpsb_pud)

            if not _fetched:
                st.warning("No data returned from BestPriceSkipBins. Check the postcode and dates, then try again.")
                st.session_state.pop("bpsb_results", None)
            else:
                st.session_state["bpsb_results"]    = _fetched
                st.session_state["bpsb_search_pc"]  = bpsb_postcode
                st.session_state["bpsb_search_dod"] = bpsb_dod_raw
                st.session_state["bpsb_search_pud"] = bpsb_pud_raw
                # Compute edited prices (< 7.5 m³) — stored separately, NOT in disk cache
                _lt75 = [s for s in BPSB.ALL_SIZES if float(s) < 7.5]
                _ep = {}
                for _wt in BPSB.WASTE_TYPES:
                    _ep[_wt] = {}
                    for _sz in _lt75:
                        _pr = _fetched.get(_wt, {}).get(_sz)
                        if isinstance(_pr, (int, float)):
                            _ep[_wt][_sz] = _pr
                st.session_state["bpsb_edited_prices"] = _ep
                _save_cache(_BPSB_CACHE, {
                    "results": _fetched,
                    "pc":      bpsb_postcode,
                    "dod":     bpsb_dod_raw,
                    "pud":     bpsb_pud_raw,
                })

        # ── Render from session_state (persists across tab switches) ──
        if "bpsb_results" in st.session_state:
            bpsb_results  = st.session_state["bpsb_results"]
            saved_pc  = st.session_state.get("bpsb_search_pc", "")
            saved_dod = st.session_state.get("bpsb_search_dod", "")
            saved_pud = st.session_state.get("bpsb_search_pud", "")

            st.success(
                f"✅ Done — Postcode **{saved_pc}**  |  {saved_dod} → {saved_pud}  |  BestPriceSkipBins prices loaded."
            )

            # --- Cheapest price highlight ---
            st.subheader("💰 Cheapest Available Price")
            cheapest_price = None
            cheapest_wt    = None
            cheapest_size  = None
            for wt, sizes in bpsb_results.items():
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

            # --- Single combined table: all waste types × all cube sizes ---
            st.subheader("📋 All Prices — All Sizes")
            st.dataframe(
                _to_df(bpsb_results, BPSB.WASTE_TYPES, BPSB.ALL_SIZES),
                use_container_width=True,
            )

            st.markdown("---")

            # --- Edited Price table: sizes < 7.5 m³, all waste types, price − 1 ---
            st.subheader("✏️ Edited Price (< 7.5 m³,  price − 1)")

            # Account checker — show which saved accounts match the searched postcode
            _bpsb_accs = st.session_state.get("bpsb_accounts", [])
            _matched = [
                a for a in _bpsb_accs
                if a.get("postcode", "").strip() == saved_pc.strip() and a.get("username", "").strip()
            ]
            _unmatched = [
                a for a in _bpsb_accs
                if a.get("postcode", "").strip() != saved_pc.strip() and a.get("username", "").strip()
            ]
            if _matched:
                st.success(
                    "**Accounts linked to postcode " + saved_pc + ":** "
                    + "  |  ".join(f"✅ {a['label']}: `{a['username']}`" for a in _matched)
                )
            else:
                st.warning(f"⚠️ No saved account has postcode **{saved_pc}**.")
            if _unmatched:
                st.caption(
                    "Other accounts: "
                    + "  |  ".join(f"{a['label']}: `{a['username']}` (postcode {a.get('postcode') or 'not set'})" for a in _unmatched)
                )
            _bpsb_lt75 = [s for s in BPSB.ALL_SIZES if float(s) < 7.5]
            # Read orig_prices from session state — only present after a fresh search
            _orig_prices = st.session_state.get("bpsb_edited_prices", {})

            if not _orig_prices:
                st.info("Run a price search above to populate the Edited Price table.")
            else:
                _min_date = _dod_to_min_date(saved_dod)
                _edit_acc = _matched[0] if _matched else None

                # One st.columns row per data row — same widths for header and data ensure alignment
                _col_widths = [2] + [1] * len(_bpsb_lt75) + [1, 1]

                # Header row (button columns intentionally empty)
                _hdr = st.columns(_col_widths)
                _hdr[0].markdown("**Waste Type**")
                for _hi, _sz in enumerate(_bpsb_lt75):
                    _hdr[1 + _hi].markdown(f"**{_sz} m³**")
                st.divider()

                # Data rows with inline Edit / Undo buttons
                _edit_btns = []
                _undo_btns = []
                for _wt_i, _wt in enumerate(BPSB.WASTE_TYPES):
                    _row = st.columns(_col_widths)
                    _row[0].write(_wt)
                    for _ci, _sz in enumerate(_bpsb_lt75):
                        _pr = _orig_prices.get(_wt, {}).get(_sz)
                        _row[1 + _ci].write(f"${_pr - 1:,.0f}" if _pr is not None else "N/A")
                    _edit_btns.append(_row[-2].button(
                        "✏️ Edit", key=f"bpsb_wt_edit_{_wt_i}",
                        disabled=_edit_acc is None, use_container_width=True,
                    ))
                    _undo_btns.append(_row[-1].button(
                        "↩️ Undo", key=f"bpsb_wt_undo_{_wt_i}",
                        disabled=_edit_acc is None, use_container_width=True,
                    ))

                # Result feedback + action processing per waste type
                for _wt_i, _wt in enumerate(BPSB.WASTE_TYPES):
                    _prev = st.session_state.get(f"bpsb_wt_result_{_wt_i}")
                    if _prev:
                        if _prev["ok"]:
                            st.success(f"**{_wt}** — {_prev['msg']}")
                        else:
                            st.error(f"**{_wt}** — {_prev['msg']}")
                        if _prev.get("shot"):
                            st.image(_prev["shot"], use_container_width=True)

                    if (_edit_btns[_wt_i] or _undo_btns[_wt_i]) and _edit_acc:
                        _wt_sizes = [s for s in _bpsb_lt75 if s in _orig_prices.get(_wt, {})]
                        if _undo_btns[_wt_i]:
                            _wt_updates = [(s, str(int(_orig_prices[_wt][s]))) for s in _wt_sizes]
                            _action_label = "Undo"
                        else:
                            _wt_updates = [(s, str(int(_orig_prices[_wt][s]) - 1)) for s in _wt_sizes]
                            _action_label = "Edit Price"

                        if not _wt_updates:
                            st.warning(f"⚠️ No priced sizes < 7.5 m³ found for {_wt}.")
                        else:
                            with st.spinner(
                                f"✏️ {_action_label}: {_wt} — "
                                f"logging in as {_edit_acc['label']} ({_edit_acc['username']})…"
                            ):
                                _wrok, _wrmsg, _wrshot = BPSB.update_waste_type_rates(
                                    _edit_acc["username"],
                                    _edit_acc["password"],
                                    waste_type=_wt,
                                    updates=_wt_updates,
                                    min_date=_min_date if _min_date else None,
                                    login_delay=6,
                                    edit_delay=3,
                                )
                            st.session_state[f"bpsb_wt_result_{_wt_i}"] = {
                                "ok": _wrok, "msg": _wrmsg, "shot": _wrshot
                            }
                            st.rerun()

    # -----------------------------------------------------------------------
    # Sub-tab: Sign In Information  (3 saved accounts, passwords locked)
    # -----------------------------------------------------------------------
    with bpsb_tab_signin:
        st.subheader("🔐 Sign In Information — bestpriceskipbins.com.au")
        st.caption("Saved credentials for up to 3 BestPriceSkipBins supplier accounts. Passwords are hidden and protected.")
        st.markdown("---")

        bpsb_accounts = st.session_state["bpsb_accounts"]
        bpsb_unlocked = st.session_state["bpsb_acc_unlocked"]
        _BPSB_ACC_CACHE = os.path.join(_CACHE_DIR, "bpsb_accounts.json")

        for i in range(3):
            acc = bpsb_accounts[i]
            with st.expander(
                f"**{acc['label']}**  —  Postcode: `{acc.get('postcode') or '(not set)'}`  |  Username: `{acc.get('username') or '(not set)'}`",
                expanded=True,
            ):
                v1, v2, v3 = st.columns([1, 1, 1])
                with v1:
                    new_pc = st.text_input(
                        "Postcode",
                        value=acc.get("postcode", ""),
                        key=f"bpsb_postcode_{i}",
                    )
                with v2:
                    new_user = st.text_input(
                        "Username",
                        value=acc.get("username", ""),
                        key=f"bpsb_user_{i}",
                    )
                with v3:
                    masked = "••••••••" if acc.get("password") else "(not set)"
                    st.text_input(
                        "Password",
                        value=masked,
                        disabled=True,
                        key=f"bpsb_pwd_display_{i}",
                    )

                _changed = False
                if new_pc != acc.get("postcode", ""):
                    bpsb_accounts[i]["postcode"] = new_pc
                    _changed = True
                if new_user != acc.get("username", ""):
                    bpsb_accounts[i]["username"] = new_user
                    _changed = True
                if _changed:
                    _save_cache(_BPSB_ACC_CACHE, bpsb_accounts)
                    st.rerun()

                if not bpsb_unlocked[i]:
                    st.markdown("🔒 **Password is locked.** Enter the current password to unlock and change it.")
                    ul1, ul2 = st.columns([2, 1])
                    with ul1:
                        entered = st.text_input(
                            "Enter current password to unlock",
                            type="password",
                            key=f"bpsb_unlock_{i}",
                        )
                    with ul2:
                        st.write("")
                        st.write("")
                        unlock_btn = st.button("🔓 Unlock", key=f"bpsb_unlock_btn_{i}")

                    if unlock_btn:
                        if not acc.get("password") or entered == acc["password"]:
                            bpsb_unlocked[i] = True
                            st.session_state["bpsb_acc_unlocked"] = bpsb_unlocked
                            st.rerun()
                        else:
                            st.error("❌ Incorrect password.")
                else:
                    st.markdown("🔓 **Unlocked.** Set a new password below.")
                    e1, e2, e3 = st.columns([2, 2, 1])
                    with e1:
                        new_pwd = st.text_input("New Password", type="password", key=f"bpsb_newpwd_{i}")
                    with e2:
                        confirm_pwd = st.text_input("Confirm Password", type="password", key=f"bpsb_confirmpwd_{i}")
                    with e3:
                        st.write("")
                        st.write("")
                        save_btn = st.button("💾 Save", key=f"bpsb_savepwd_{i}", type="primary")

                    cancel_btn = st.button("🔒 Cancel & Lock", key=f"bpsb_cancel_{i}")

                    if save_btn:
                        if not new_pwd:
                            st.error("New password cannot be empty.")
                        elif new_pwd != confirm_pwd:
                            st.error("❌ Passwords do not match.")
                        else:
                            bpsb_accounts[i]["password"] = new_pwd
                            _save_cache(_BPSB_ACC_CACHE, bpsb_accounts)
                            bpsb_unlocked[i] = False
                            st.session_state["bpsb_acc_unlocked"] = bpsb_unlocked
                            st.success(f"✅ Password for {acc['label']} saved.")
                            st.rerun()

                    if cancel_btn:
                        bpsb_unlocked[i] = False
                        st.session_state["bpsb_acc_unlocked"] = bpsb_unlocked
                        st.rerun()

        # -----------------------------------------------------------------------
        # Login & Screenshot section
        # -----------------------------------------------------------------------
        st.markdown("---")
        st.subheader("🌐 Login & Screenshot")
        st.caption("Log in to bestpriceskipbins.com.au/supplier/ with a saved account and take a screenshot.")

        _acc_labels = [f"Account {i+1} ({bpsb_accounts[i].get('username') or 'not set'})" for i in range(3)]
        _sel_acc = st.selectbox("Select account to log in with", _acc_labels, key="bpsb_login_acc_sel")
        _sel_idx = int(_sel_acc.split()[1]) - 1  # "Account 1..." → index 0

        _login_delay = st.slider(
            "⏱️ Page load delay after login (seconds)",
            min_value=2, max_value=20, value=6, step=1,
            key="bpsb_login_delay",
            help="Increase if the dashboard hasn't finished loading in the screenshot.",
        )

        login_btn_main = st.button("🔑 Login & Take Screenshot", type="primary", key="bpsb_login_btn")

        if login_btn_main:
            _login_acc = bpsb_accounts[_sel_idx]
            _login_user = _login_acc.get("username", "")
            _login_pwd  = _login_acc.get("password", "")
            if not _login_user or not _login_pwd:
                st.error(f"❌ {_login_acc['label']} has no username or password set. Please fill them in above first.")
            else:
                with st.spinner(f"🔑 Logging in as {_login_acc['label']} ({_login_user})… waiting {_login_delay}s for page to load"):
                    _ok, _msg, _shot = BPSB.login(_login_user, _login_pwd, login_delay=_login_delay)
                if _ok:
                    st.success(f"✅ {_msg}")
                else:
                    st.error(f"❌ {_msg}")
                if _shot:
                    st.image(_shot, caption=f"Screenshot — {_login_acc['label']} ({_login_user})", use_container_width=True)

    # -----------------------------------------------------------------------
    # Sub-tab: Update Rates
    # -----------------------------------------------------------------------
    with bpsb_tab_rates:
        st.subheader("✏️ Update Rates — bestpriceskipbins.com.au")
        st.caption("Log in once and update all bin sizes under 7.5 m³ in a single run.")
        st.markdown("---")

        _rate_acc_labels = [f"Account {i+1} ({bpsb_accounts[i].get('username') or 'not set'})" for i in range(3)]
        _rate_sel = st.selectbox("Account", _rate_acc_labels, key="bpsb_rate_acc_sel")
        _rate_idx = int(_rate_sel.split()[1]) - 1

        _rate_waste_urls = {
            "General Waste":                    "https://bestpriceskipbins.com.au/supplier/rates_manage.php",
            "Mixed Heavy Waste":                "https://bestpriceskipbins.com.au/supplier/rates_manage_mhw.php",
            "Mixed Heavy Waste (No Soil/Dirt)": "https://bestpriceskipbins.com.au/supplier/rates_manage_mhwns.php",
            "Concrete / Bricks":                "https://bestpriceskipbins.com.au/supplier/rates_manage_conc.php",
            "Green Waste":                      "https://bestpriceskipbins.com.au/supplier/rates_manage_green.php",
            "Dirt / Soil":                      "https://bestpriceskipbins.com.au/supplier/rates_manage_dirt.php",
        }
        _rate_waste = st.selectbox("Waste Type", list(_rate_waste_urls.keys()), key="bpsb_rate_waste")

        st.markdown("##### Enter new prices for bin sizes < 7.5 m³")
        st.caption("Leave a field blank to skip that size. Sizes are updated in order, one login session.")

        # (bin size label, sequential row id on the rates page)
        _lt75_sizes = ["2", "3", "4", "5", "6", "7"]
        _price_inputs = {}
        _size_cols = st.columns(3)
        for _ci, sz in enumerate(_lt75_sizes):
            with _size_cols[_ci % 3]:
                _price_inputs[sz] = st.text_input(
                    f"{sz} m³",
                    value="",
                    key=f"bpsb_rate_price_{sz}",
                    placeholder="e.g. 189",
                )

        _rate_r1, _rate_r2 = st.columns(2)
        with _rate_r1:
            _rate_login_delay = st.slider(
                "⏱️ Login delay (s)", min_value=2, max_value=20, value=6, step=1, key="bpsb_rate_login_delay"
            )
        with _rate_r2:
            _rate_edit_delay = st.slider(
                "⏱️ Per-row delay (s)", min_value=1, max_value=10, value=3, step=1, key="bpsb_rate_edit_delay",
                help="Wait time after navigating to each row's edit page before interacting."
            )

        _rate_btn = st.button("✏️ Update All Sizes < 7.5 m³", type="primary", key="bpsb_rate_btn")

        if _rate_btn:
            _racc = bpsb_accounts[_rate_idx]
            _ruser = _racc.get("username", "")
            _rpwd  = _racc.get("password", "")
            if not _ruser or not _rpwd:
                st.error(f"❌ {_racc['label']} has no username or password set. Fill them in the Sign In tab first.")
            else:
                _updates = [
                    (sz, _price_inputs[sz].strip())
                    for sz in _lt75_sizes
                    if _price_inputs[sz].strip()
                ]
                if not _updates:
                    st.warning("⚠️ Enter at least one price before updating.")
                else:
                    _summary_preview = "  |  ".join(
                        f"{sz} m³ → ${_price_inputs[sz].strip()}"
                        for sz in _lt75_sizes if _price_inputs[sz].strip()
                    )
                    with st.spinner(
                        f"✏️ Logging in as {_racc['label']} and updating {len(_updates)} size(s): {_summary_preview}…"
                    ):
                        _rok, _rmsg, _rshot = BPSB.update_multiple_rates(
                            _ruser, _rpwd,
                            updates=_updates,
                            rates_url=_rate_waste_urls[_rate_waste],
                            login_delay=_rate_login_delay,
                            edit_delay=_rate_edit_delay,
                        )
                    if _rok:
                        st.success(f"✅ Done!  {_rmsg}")
                    else:
                        st.error(f"❌ {_rmsg}")
                    if _rshot:
                        st.image(_rshot, caption="Screenshot after all updates", use_container_width=True)

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
