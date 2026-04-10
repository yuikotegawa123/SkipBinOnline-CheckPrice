"""
app.py  –  Streamlit web UI for SkipBin Price Checker
Deploy to Streamlit Community Cloud (free) from GitHub.
"""

import json
import os
import queue
import threading
import time
from typing import Optional

import pandas as pd
import streamlit as st

import Bookabin
import Bestpriceskipbins as BPSB
import Skipbinfinder as SBF
import Skipbinsonline as SBO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Full union of every size across all four scrapers.
# ns-suffixed sizes (e.g. "3ns" = 3 m³ no-soil) sort immediately after their base size.
def _size_sort_key(s):
    base = s[:-2] if s.endswith('ns') else s
    return (float(base), 1 if s.endswith('ns') else 0)

FULL_SIZES = sorted(
    set(Bookabin.ALL_SIZES) | set(BPSB.ALL_SIZES) | set(SBF.ALL_SIZES) | set(SBO.ALL_SIZES),
    key=_size_sort_key,
)


def _fmt_size_col(s: str) -> str:
    """Return a display column name for a size key, e.g. '3ns' → '3 m³ (no soil)'."""
    if s.endswith('ns'):
        return f"{s[:-2]} m³ (no soil)"
    return f"{s} m³"


def _to_df(results: dict, waste_types_map: dict, all_sizes: list) -> pd.DataFrame:
    rows = []
    for wt in waste_types_map:
        row = {"Waste Type": wt}
        for s in all_sizes:
            price = results.get(wt, {}).get(s)
            if isinstance(price, (int, float)):
                row[_fmt_size_col(s)] = f"${price:,.0f}"
            else:
                row[_fmt_size_col(s)] = "N/A"
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


def _dod_to_sbo_date(d_slash: str) -> str:
    """Convert D/MM/YYYY → YY-MM-DD for use in SBO rates URL startDate param."""
    parts = d_slash.strip().split("/")
    if len(parts) != 3:
        return ""
    day, month, year = parts
    return f"{str(year)[-2:]}-{int(month):02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
# Disk cache helpers  (survive F5 / page refresh)
# ---------------------------------------------------------------------------

_CACHE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_BAB_CACHE     = os.path.join(_CACHE_DIR, "bab_results.json")
_BPSB_CACHE    = os.path.join(_CACHE_DIR, "bpsb_results.json")
_SBF_CACHE     = os.path.join(_CACHE_DIR, "sbf_results.json")
_SBO_CACHE     = os.path.join(_CACHE_DIR, "sbo_results.json")

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

_DEFAULT_SBF_ACCOUNTS = [
    {"label": "Account 1", "username": "", "password": "", "postcode": "3173"},
    {"label": "Account 2", "username": "", "password": "", "postcode": "3130"},
    {"label": "Account 3", "username": "", "password": "", "postcode": "3199"},
]

_DEFAULT_SBO_ACCOUNTS = [
    {"label": "Account 1", "username": "", "password": "", "postcode": "3173"},
    {"label": "Account 2", "username": "", "password": "", "postcode": "3130"},
    {"label": "Account 3", "username": "", "password": "", "postcode": "3199"},
]

def _save_cache(path: str, data: dict) -> None:
    """Persist arbitrary dict to a JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

def _load_cache(path: str) -> Optional[dict]:
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

_GIST_FILENAME      = "bab_accounts.json"
_GIST_BPSB_FILENAME = "bpsb_accounts.json"
_GIST_SBF_FILENAME  = "sbf_accounts.json"
_GIST_SBO_FILENAME  = "sbo_accounts.json"

def _gist_token() -> Optional[str]:
    """Return GitHub token from st.secrets, or None if not configured."""
    try:
        return st.secrets["gist"]["token"]
    except Exception:
        return None

def _gist_id() -> Optional[str]:
    """Return Gist ID from st.secrets, or None if not configured."""
    try:
        return st.secrets["gist"]["gist_id"]
    except Exception:
        return None

def _gist_load() -> Optional[list]:
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

def _gist_load_bpsb() -> Optional[list]:
    """Fetch BPSB account list from the Gist (bpsb_accounts.json file)."""
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
        files = resp.json().get("files", {})
        if _GIST_BPSB_FILENAME not in files:
            return None
        content = files[_GIST_BPSB_FILENAME]["content"]
        data = json.loads(content)
        return data if isinstance(data, list) else None
    except Exception:
        return None

def _gist_save_bpsb(accounts: list) -> bool:
    """Write BPSB account list to the Gist (bpsb_accounts.json file)."""
    token = _gist_token()
    gid   = _gist_id()
    if not token or not gid:
        return False
    try:
        resp = _requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"files": {_GIST_BPSB_FILENAME: {"content": json.dumps(accounts, indent=2)}}},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False

def _gist_load_sbf() -> Optional[list]:
    """Fetch SBF account list from the Gist (sbf_accounts.json file)."""
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
        files = resp.json().get("files", {})
        if _GIST_SBF_FILENAME not in files:
            return None
        content = files[_GIST_SBF_FILENAME]["content"]
        data = json.loads(content)
        return data if isinstance(data, list) else None
    except Exception:
        return None

def _gist_save_sbf(accounts: list) -> bool:
    """Write SBF account list to the Gist (sbf_accounts.json file)."""
    token = _gist_token()
    gid   = _gist_id()
    if not token or not gid:
        return False
    try:
        resp = _requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"files": {_GIST_SBF_FILENAME: {"content": json.dumps(accounts, indent=2)}}},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except Exception:
        return False

def _gist_load_sbo() -> Optional[list]:
    """Fetch SBO account list from the Gist (sbo_accounts.json file)."""
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
        files = resp.json().get("files", {})
        if _GIST_SBO_FILENAME not in files:
            return None
        content = files[_GIST_SBO_FILENAME]["content"]
        data = json.loads(content)
        return data if isinstance(data, list) else None
    except Exception:
        return None

def _gist_save_sbo(accounts: list) -> bool:
    """Write SBO account list to the Gist (sbo_accounts.json file)."""
    token = _gist_token()
    gid   = _gist_id()
    if not token or not gid:
        return False
    try:
        resp = _requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json={"files": {_GIST_SBO_FILENAME: {"content": json.dumps(accounts, indent=2)}}},
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

# BPSB accounts (Gist primary, local cache fallback)
if "bpsb_accounts" not in st.session_state:
    _bpsb_acc = _gist_load_bpsb()
    if not isinstance(_bpsb_acc, list):
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

# SBF accounts
if "sbf_accounts" not in st.session_state:
    _sbf_acc = _gist_load_sbf()
    if not isinstance(_sbf_acc, list):
        _sbf_acc = _load_cache(os.path.join(_CACHE_DIR, "sbf_accounts.json"))
    if not isinstance(_sbf_acc, list):
        _sbf_acc = []
    _sbf_acc = [a if isinstance(a, dict) else {} for a in _sbf_acc]
    while len(_sbf_acc) < 3:
        _sbf_acc.append({})
    for _idx, _def in enumerate(_DEFAULT_SBF_ACCOUNTS):
        for _k, _v in _def.items():
            _sbf_acc[_idx].setdefault(_k, _v)
    st.session_state["sbf_accounts"] = _sbf_acc[:3]

if "sbf_acc_unlocked" not in st.session_state:
    st.session_state["sbf_acc_unlocked"] = [False, False, False]

# SBF results cache
if "sbf_results" not in st.session_state:
    _cached_sbf = _load_cache(_SBF_CACHE)
    if _cached_sbf:
        st.session_state["sbf_results"]    = _cached_sbf.get("results", {})
        st.session_state["sbf_search_pc"]  = _cached_sbf.get("pc", "")
        st.session_state["sbf_search_dod"] = _cached_sbf.get("dod", "")
        st.session_state["sbf_search_pud"] = _cached_sbf.get("pud", "")

# SBO accounts
if "sbo_accounts" not in st.session_state:
    _sbo_acc = _gist_load_sbo()
    if not isinstance(_sbo_acc, list):
        _sbo_acc = _load_cache(os.path.join(_CACHE_DIR, "sbo_accounts.json"))
    if not isinstance(_sbo_acc, list):
        _sbo_acc = []
    _sbo_acc = [a if isinstance(a, dict) else {} for a in _sbo_acc]
    while len(_sbo_acc) < 3:
        _sbo_acc.append({})
    for _idx, _def in enumerate(_DEFAULT_SBO_ACCOUNTS):
        for _k, _v in _def.items():
            _sbo_acc[_idx].setdefault(_k, _v)
    st.session_state["sbo_accounts"] = _sbo_acc[:3]

if "sbo_acc_unlocked" not in st.session_state:
    st.session_state["sbo_acc_unlocked"] = [False, False, False]

# SBO results cache
if "sbo_results" not in st.session_state:
    _cached_sbo = _load_cache(_SBO_CACHE)
    if _cached_sbo:
        st.session_state["sbo_results"]    = _cached_sbo.get("results", {})
        st.session_state["sbo_search_pc"]  = _cached_sbo.get("pc", "")
        st.session_state["sbo_search_dod"] = _cached_sbo.get("dod", "")
        st.session_state["sbo_search_pud"] = _cached_sbo.get("pud", "")

def _go_home():
    st.session_state.page = "Home"
    st.session_state.supplier_nav = None

with st.sidebar:
    st.title("🗑️ SkipBin Tool")
    st.markdown("---")

    st.button("🏠  Home", width='stretch', on_click=_go_home)

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

    HOME_POSTCODES = ["3173", "3130", "3199"]
    _SUPPLIER_KEYS  = ["bab", "bpsb", "sbf", "sbo"]
    _SUPPLIER_LABELS = {
        "bab":  "📦 BookABin",
        "bpsb": "💰 BestPriceSkipBins",
        "sbf":  "🔍 SkipBinFinder",
        "sbo":  "🌐 SkipBinsOnline",
    }

    col1, col2, col3 = st.columns([1.4, 1.4, 1])
    with col1:
        dod = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026")
    with col2:
        pud = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026")
    with col3:
        st.write("")
        st.write("")
        search = st.button("🔍 Search All", width='stretch', type="primary")

    if search:
        if not dod or not pud:
            st.error("Please fill in all fields.")
            st.stop()

        _bpsb_dod = _parse_bpsb_date(dod)
        _bpsb_pud = _parse_bpsb_date(pud)

        # all_results[pc][supplier_key] = {wt: {size: price}}
        all_results = {pc: {} for pc in HOME_POSTCODES}
        total_tasks = len(HOME_POSTCODES) * len(_SUPPLIER_KEYS)
        n_done_total = 0

        def _run_supplier(pc, key, run_fn, *args):
            cell_q   = queue.Queue()
            status_q = queue.Queue()
            done     = threading.Event()
            threading.Thread(target=run_fn, args=(*args, cell_q, status_q, done), daemon=True).start()
            done.wait()
            out = {}
            while not cell_q.empty():
                wt, size, price = cell_q.get_nowait()
                out.setdefault(wt, {})[size] = price
            all_results[pc][key] = out

        prog = st.progress(0, text="Searching postcodes …")

        # Process one postcode at a time → max 2 Chrome instances at once (BAB + SBF)
        for _pc_i, _pc in enumerate(HOME_POSTCODES):
            prog.progress(n_done_total / total_tasks,
                          text=f"Postcode {_pc} ({_pc_i+1}/{len(HOME_POSTCODES)}) …")
            _pc_threads = [
                threading.Thread(target=_run_supplier, args=(_pc, "bab",  Bookabin.run_search, _pc, dod, pud),              daemon=True),
                threading.Thread(target=_run_supplier, args=(_pc, "bpsb", BPSB.run_search,     _pc, _bpsb_dod, _bpsb_pud),  daemon=True),
                threading.Thread(target=_run_supplier, args=(_pc, "sbf",  SBF.run_search,      _pc, dod, pud),              daemon=True),
                threading.Thread(target=_run_supplier, args=(_pc, "sbo",  SBO.run_search,      _pc, dod, pud),              daemon=True),
            ]
            for t in _pc_threads:
                t.start()
            while any(t.is_alive() for t in _pc_threads):
                _done_pc = sum(1 for k in _SUPPLIER_KEYS if k in all_results[_pc])
                prog.progress((n_done_total + _done_pc) / total_tasks,
                              text=f"Postcode {_pc}: {_done_pc}/4 suppliers done …")
                time.sleep(1)
            for t in _pc_threads:
                t.join()
            n_done_total += len(_SUPPLIER_KEYS)

        prog.progress(1.0, text="All done!")

        st.success(f"✅ Done — Postcodes {', '.join(HOME_POSTCODES)}  |  {dod} → {pud}")

        pc_tabs = st.tabs([f"📍 {pc}" for pc in HOME_POSTCODES])
        for pc, pc_tab in zip(HOME_POSTCODES, pc_tabs):
            with pc_tab:
                pc_res = all_results[pc]

                bab_res  = pc_res.get("bab",  {})
                bpsb_res = pc_res.get("bpsb", {})
                sbf_res  = pc_res.get("sbf",  {})
                sbo_res  = pc_res.get("sbo",  {})

                st.subheader("📦 BookABin")
                if bab_res:
                    st.dataframe(_to_df(bab_res, Bookabin.WASTE_TYPES, Bookabin.ALL_SIZES), width='stretch')
                else:
                    st.warning("No data returned from BookABin.")

                st.subheader("💰 BestPriceSkipBins")
                if bpsb_res:
                    st.dataframe(_to_df(bpsb_res, BPSB.WASTE_TYPES, BPSB.ALL_SIZES), width='stretch')
                else:
                    st.warning("No data returned from BestPriceSkipBins.")

                st.subheader("🔍 SkipBinFinder")
                if sbf_res:
                    st.dataframe(_to_df(sbf_res, SBF.WASTE_TYPES, SBF.ALL_SIZES), width='stretch')
                else:
                    st.warning("No data returned from SkipBinFinder.")

                st.subheader("🌐 SkipBinsOnline")
                if sbo_res:
                    sbo_waste = {wt: list(sizes.keys()) for wt, sizes in sbo_res.items()}
                    st.dataframe(_to_df(sbo_res, sbo_waste, SBO.ALL_SIZES), width='stretch')
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
            bab_search = st.button("🔍 Search BookABin", width='stretch', type="primary", key="bab_search")

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
            _bab_lt75_sizes = [s for s in Bookabin.ALL_SIZES if float(s) <= 7.5]
            st.dataframe(
                _to_df(bab_results, Bookabin.WASTE_TYPES, _bab_lt75_sizes),
                width='stretch',
            )

            st.markdown("---")

            # ---------------------------------------------------------------
            # Update Price section
            # ---------------------------------------------------------------
            st.subheader("💲 Update Price")

            # Only these two waste types get price-1
            _MINUS1_WTS    = {'General Waste', 'Green Garden Waste'}
            _EXTRA_SZFS    = {15.0, 16.0, 20.0, 30.0}

            # Build price_map: (waste_type, size) → will_set_to
            _price_map = {}   # (wt, sz) → will_set_to
            for _wt, _sizes in bab_results.items():
                for _sz, _pr in _sizes.items():
                    if isinstance(_pr, (int, float)):
                        try:
                            _sz_f = float(_sz)
                        except (ValueError, TypeError):
                            _sz_f = 0.0
                        if _wt in _MINUS1_WTS and (_sz_f <= 12.0 or _sz_f in _EXTRA_SZFS):
                            _adj = int(_pr) - 1
                        else:
                            _adj = int(_pr)
                        _key = (_wt, _sz)
                        if _key not in _price_map or _adj < _price_map[_key]:
                            _price_map[_key] = _adj

            if _price_map:
                # Ordered waste types and sizes matching All Available Sizes table
                _update_wts  = [wt for wt in Bookabin.WASTE_TYPES if any(k[0] == wt for k in _price_map)]
                _update_szs  = [sz for sz in Bookabin.ALL_SIZES if float(sz) <= 7.5 and any(k[1] == sz for k in _price_map)]
                _preview_rows = []
                for _wt in _update_wts:
                    _row = {"Waste Type": _wt}
                    for _sz in _update_szs:
                        _val = _price_map.get((_wt, _sz))
                        _row[f"{_sz} m³"] = f"${_val:,.0f}" if _val is not None else "N/A"
                    _preview_rows.append(_row)
                _df_preview = pd.DataFrame(_preview_rows).set_index("Waste Type")
                st.dataframe(_df_preview, width='stretch')

                # Copy / Download buttons
                _copy_col, _dl_col = st.columns([1, 1])
                with _copy_col:
                    _csv_text = _df_preview.to_csv(index=True)
                    st.download_button(
                        "📋 Download as CSV",
                        data=_csv_text,
                        file_name="update_price.csv",
                        mime="text/csv",
                        width='stretch',
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

    bpsb_tab_prices, bpsb_tab_signin = st.tabs(["🔍 Check Price", "🔐 Sign In Information"])

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
            bpsb_search = st.button("🔍 Search BestPriceSkipBins", width='stretch', type="primary", key="bpsb_search")

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
                # Compute edited prices — store all sizes available from the scraper
                _ep = {}
                for _wt in BPSB.WASTE_TYPES:
                    _ep[_wt] = {}
                    for _sz in BPSB.ALL_SIZES:
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
                width='stretch',
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
            # Read orig_prices from session state — only present after a fresh search
            _orig_prices = st.session_state.get("bpsb_edited_prices", {})

            # Only show/edit sizes < 7.5 m³ (i.e. 2, 3, 4, 5, 6, 7)
            _BPSB_SMALL_SIZES = [s for s in BPSB.ALL_SIZES if float(s) <= 7.5]
            _bpsb_display_sizes = _BPSB_SMALL_SIZES

            def _edit_sizes_for(wt):
                """Return sizes to edit (only < 7.5 m³) that have a price for this waste type."""
                return [s for s in _BPSB_SMALL_SIZES if s in _orig_prices.get(wt, {})]

            if not _orig_prices:
                st.info("Run a price search above to populate the Edited Price table.")
            else:
                _min_date = _dod_to_min_date(saved_dod)
                _edit_acc = _matched[0] if _matched else None

                # Queue stored as {wt_index: "edit" | "undo"}
                if "bpsb_wt_queue" not in st.session_state:
                    st.session_state["bpsb_wt_queue"] = {}
                _queue = st.session_state["bpsb_wt_queue"]

                # One st.columns row per data row — same widths for header and data ensure alignment
                _col_widths = [2] + [1] * len(_bpsb_display_sizes) + [1, 1]

                # Header row (button columns intentionally empty)
                _hdr = st.columns(_col_widths)
                _hdr[0].markdown("**Waste Type**")
                for _hi, _sz in enumerate(_bpsb_display_sizes):
                    _hdr[1 + _hi].markdown(f"**{_sz} m³**")
                st.divider()

                # Data rows with inline Edit / Undo buttons
                _edit_btns = []
                _undo_btns = []
                for _wt_i, _wt in enumerate(BPSB.WASTE_TYPES):
                    _row = st.columns(_col_widths)
                    # Show queue indicator next to name
                    _queued = _queue.get(_wt_i)
                    _name_label = f"{'⏳ ' if _queued else ''}{_wt}{'  *(undo)*' if _queued == 'undo' else '  *(edit)*' if _queued == 'edit' else ''}"
                    _row[0].markdown(_name_label)
                    _wt_edit_szs = set(_edit_sizes_for(_wt))
                    for _ci, _sz in enumerate(_bpsb_display_sizes):
                        _pr = _orig_prices.get(_wt, {}).get(_sz)
                        if _pr is not None and _sz in _wt_edit_szs:
                            _row[1 + _ci].write(f"${_pr - 1:,.0f}")
                        elif _pr is not None:
                            _row[1 + _ci].write(f"${_pr:,.0f}")
                        else:
                            _row[1 + _ci].write("N/A")
                    _edit_btns.append(_row[-2].button(
                        "✏️ Edit", key=f"bpsb_wt_edit_{_wt_i}",
                        disabled=_edit_acc is None, width='stretch',
                    ))
                    _undo_btns.append(_row[-1].button(
                        "↩️ Undo", key=f"bpsb_wt_undo_{_wt_i}",
                        disabled=_edit_acc is None, width='stretch',
                    ))

                # Queue / dequeue on button click (no Selenium yet)
                for _wt_i, _wt in enumerate(BPSB.WASTE_TYPES):
                    if _edit_btns[_wt_i] and _edit_acc:
                        if _queue.get(_wt_i) == "edit":
                            del _queue[_wt_i]   # toggle off
                        else:
                            _queue[_wt_i] = "edit"
                        st.rerun()
                    if _undo_btns[_wt_i] and _edit_acc:
                        if _queue.get(_wt_i) == "undo":
                            del _queue[_wt_i]   # toggle off
                        else:
                            _queue[_wt_i] = "undo"
                        st.rerun()

                # ── Queue table ──────────────────────────────────────────
                st.markdown("#### 🗂 Queue")
                if not _queue:
                    st.caption("No actions queued. Click ✏️ Edit or ↩️ Undo above to add.")
                else:
                    _wt_keys = list(BPSB.WASTE_TYPES.keys())
                    _q_hdr = st.columns([3, 1, 1])
                    _q_hdr[0].markdown("**Waste Type**")
                    _q_hdr[1].markdown("**Action**")
                    _q_hdr[2].markdown("**Remove**")
                    for _qi, (_q_idx, _q_action) in enumerate(list(_queue.items())):
                        _q_wt = _wt_keys[_q_idx]
                        _qc = st.columns([3, 1, 1])
                        _qc[0].write(_q_wt)
                        _qc[1].write("✏️ Edit" if _q_action == "edit" else "↩️ Undo")
                        if _qc[2].button("🗑️", key=f"bpsb_q_del_{_qi}_{_q_idx}", width='stretch'):
                            del _queue[_q_idx]
                            st.rerun()

                # ── Last run summary banner ───────────────────────────────
                _last_run = st.session_state.get("bpsb_last_run_summary", [])
                if _last_run:
                    st.markdown("#### 📋 Last Run Results")
                    _ok_items  = [r for r in _last_run if r["ok"]]
                    _err_items = [r for r in _last_run if not r["ok"]]
                    if _ok_items:
                        st.success("**Done:**\n" + "\n".join(f"- {r['msg']}" for r in _ok_items))
                    if _err_items:
                        st.error("**Failed:**\n" + "\n".join(f"- {r['msg']}" for r in _err_items))
                    if st.button("✖ Clear results", key="bpsb_clear_results"):
                        st.session_state["bpsb_last_run_summary"] = []
                        st.rerun()
                    st.markdown("---")

                for _wt_i, _wt in enumerate(BPSB.WASTE_TYPES):
                    _prev = st.session_state.get(f"bpsb_wt_result_{_wt_i}")
                    if _prev:
                        if _prev["ok"]:
                            st.success(f"**{_wt}** — {_prev['msg']}")
                        else:
                            st.error(f"**{_wt}** — {_prev['msg']}")

                def _run_wt_edit(wt, undo=False):
                    """Run a single waste-type edit; stores result in session state."""
                    idx = list(BPSB.WASTE_TYPES.keys()).index(wt)
                    wt_sizes = _edit_sizes_for(wt)
                    if undo:
                        wt_updates = [(s, str(int(_orig_prices[wt][s]))) for s in wt_sizes]
                    else:
                        wt_updates = [(s, str(int(_orig_prices[wt][s]) - 1)) for s in wt_sizes]
                    if not wt_updates:
                        result = {"ok": False, "msg": f"{wt}: No priced sizes < 12 m³ found."}
                        st.session_state[f"bpsb_wt_result_{idx}"] = result
                        st.session_state.setdefault("bpsb_last_run_summary", []).append(result)
                        return
                    ok, msg = BPSB.update_waste_type_rates(
                        _edit_acc["username"], _edit_acc["password"],
                        waste_type=wt, updates=wt_updates,
                        min_date=_min_date if _min_date else None,
                        login_delay=6, edit_delay=3,
                    )
                    result = {"ok": ok, "msg": f"{wt} — {msg}"}
                    st.session_state[f"bpsb_wt_result_{idx}"] = result
                    st.session_state.setdefault("bpsb_last_run_summary", []).append(result)

                if _edit_acc:
                    import concurrent.futures
                    _btn_row = st.columns([1, 1, 1, 5])

                    # Run Queue button — runs all queued items in parallel
                    _run_queue_btn = _btn_row[0].button(
                        f"▶ Run Queue ({len(_queue)})" if _queue else "▶ Run Queue",
                        key="bpsb_run_queue",
                        disabled=not _queue,
                        width='stretch',
                    )
                    _edit_all = _btn_row[1].button("✏️ Edit All", key="bpsb_edit_all", width='stretch')
                    _undo_all = _btn_row[2].button("↩️ Undo All", key="bpsb_undo_all", width='stretch')

                    if _run_queue_btn and _queue:
                        _items = list(_queue.items())
                        _wts_to_run = [(list(BPSB.WASTE_TYPES.keys())[i], v == "undo") for i, v in _items]
                        st.session_state["bpsb_last_run_summary"] = []
                        with st.spinner(f"Running {len(_wts_to_run)} queued item(s) in parallel…"):
                            with concurrent.futures.ThreadPoolExecutor(max_workers=len(_wts_to_run)) as _pool:
                                _futures = [_pool.submit(_run_wt_edit, wt, undo) for wt, undo in _wts_to_run]
                                concurrent.futures.wait(_futures)
                        st.session_state["bpsb_wt_queue"] = {}
                        st.rerun()

                    if _edit_all or _undo_all:
                        _is_undo_all = bool(_undo_all)
                        _all_wts = [wt for wt in BPSB.WASTE_TYPES if _edit_sizes_for(wt)]
                        st.session_state["bpsb_last_run_summary"] = []
                        with st.spinner(f"{'Undo' if _is_undo_all else 'Edit'} All: {len(_all_wts)} waste types in parallel…"):
                            with concurrent.futures.ThreadPoolExecutor(max_workers=len(_all_wts)) as _pool:
                                _futures = [_pool.submit(_run_wt_edit, wt, _is_undo_all) for wt in _all_wts]
                                concurrent.futures.wait(_futures)
                        st.session_state["bpsb_wt_queue"] = {}
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
                    _gist_save_bpsb(bpsb_accounts)
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
                            _gist_save_bpsb(bpsb_accounts)
                            bpsb_unlocked[i] = False
                            st.session_state["bpsb_acc_unlocked"] = bpsb_unlocked
                            st.success(f"✅ Password for {acc['label']} saved.")
                            st.rerun()

                    if cancel_btn:
                        bpsb_unlocked[i] = False
                        st.session_state["bpsb_acc_unlocked"] = bpsb_unlocked
                        st.rerun()



# ===========================================================================
# PAGE: SkipBinFinder
# ===========================================================================

elif page == "SkipBinFinder":
    st.title("🔍 SkipBinFinder")
    st.markdown("---")

    sbf_tab_prices, sbf_tab_signin, sbf_tab_test = st.tabs(["🔍 Check Price", "🔐 Sign In Information", "🧪 Test Action"])

    # -----------------------------------------------------------------------
    # Sub-tab: Check Price
    # -----------------------------------------------------------------------
    with sbf_tab_prices:
        st.subheader("Check Prices — skipbinfinder.com.au")
        st.caption("Search live prices from SkipBinFinder for your postcode and hire period.")

        sbf_col1, sbf_col2, sbf_col3, sbf_col4 = st.columns([1, 1.4, 1.4, 1])
        with sbf_col1:
            sbf_postcode = st.text_input("Postcode", value="3173", key="sbf_pc")
        with sbf_col2:
            sbf_dod = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026", key="sbf_dod")
        with sbf_col3:
            sbf_pud = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026", key="sbf_pud")
        with sbf_col4:
            st.write("")
            st.write("")
            sbf_search = st.button("🔍 Search SkipBinFinder", width='stretch', type="primary", key="sbf_search")

        if sbf_search:
            if not sbf_postcode or not sbf_dod or not sbf_pud:
                st.error("Please fill in all fields.")
                st.stop()

            def _run_sbf(run_fn, *args):
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

            with st.spinner("🔍 Fetching prices from SkipBinFinder… (this may take a few minutes)"):
                _fetched = _run_sbf(SBF.run_search, sbf_postcode, sbf_dod, sbf_pud)

            if not _fetched:
                st.warning("No data returned from SkipBinFinder. Check the postcode and dates, then try again.")
                st.session_state.pop("sbf_results", None)
            else:
                st.session_state["sbf_results"]    = _fetched
                st.session_state["sbf_search_pc"]  = sbf_postcode
                st.session_state["sbf_search_dod"] = sbf_dod
                st.session_state["sbf_search_pud"] = sbf_pud
                _save_cache(_SBF_CACHE, {
                    "results": _fetched,
                    "pc":      sbf_postcode,
                    "dod":     sbf_dod,
                    "pud":     sbf_pud,
                })

        # ── Render from session_state (persists across tab switches) ──
        if "sbf_results" in st.session_state:
            sbf_results  = st.session_state["sbf_results"]
            saved_pc  = st.session_state.get("sbf_search_pc", "")
            saved_dod = st.session_state.get("sbf_search_dod", "")
            saved_pud = st.session_state.get("sbf_search_pud", "")

            st.success(
                f"✅ Done — Postcode **{saved_pc}**  |  {saved_dod} → {saved_pud}  |  SkipBinFinder prices loaded."
            )

            # --- Cheapest price highlight ---
            st.subheader("💰 Cheapest Available Price")
            cheapest_price = None
            cheapest_wt    = None
            cheapest_size  = None
            for wt, sizes in sbf_results.items():
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
            # Non-ns sizes <= 7.5 become columns; ns sizes become a new row
            _sbf_col_sizes = [s for s in SBF.ALL_SIZES if not s.endswith('ns') and float(s) <= 7.5]
            _sbf_rows = []
            for _wt in SBF.WASTE_TYPES:
                _row = {"Waste Type": _wt}
                for _s in _sbf_col_sizes:
                    _pr = sbf_results.get(_wt, {}).get(_s)
                    _row[f"{_s} m\u00b3"] = f"${_pr:,.0f}" if isinstance(_pr, (int, float)) else "N/A"
                _sbf_rows.append(_row)
            # Extra row for ns prices (Mixed Heavy Waste no-solid)
            _ns_row = {"Waste Type": "Mixed Heavy Waste (With no Soild & Dirt)"}
            for _s in _sbf_col_sizes:
                _ns_key = _s + "ns"
                _ns_pr = None
                for _wt2 in SBF.WASTE_TYPES:
                    _v = sbf_results.get(_wt2, {}).get(_ns_key)
                    if isinstance(_v, (int, float)) and (_ns_pr is None or _v < _ns_pr):
                        _ns_pr = _v
                _ns_row[f"{_s} m\u00b3"] = f"${_ns_pr:,.0f}" if _ns_pr is not None else "N/A"
            if any(v != "N/A" for k, v in _ns_row.items() if k != "Waste Type"):
                _sbf_rows.append(_ns_row)
            _df_sbf_avail = pd.DataFrame(_sbf_rows).set_index("Waste Type")
            st.dataframe(_df_sbf_avail, width='stretch')

            st.markdown("---")

            # ---------------------------------------------------------------
            # Update Price section
            # ---------------------------------------------------------------
            st.subheader("💲 Update Price")

            _sbf_price_map = {}
            for _wt, _sizes in sbf_results.items():
                for _sz, _pr in _sizes.items():
                    if isinstance(_pr, (int, float)):
                        _adj = int(_pr) - 1
                        _key = (_wt, _sz)
                        if _key not in _sbf_price_map or _adj < _sbf_price_map[_key]:
                            _sbf_price_map[_key] = _adj

            if _sbf_price_map:
                _sbf_update_wts = [wt for wt in SBF.WASTE_TYPES if any(k[0] == wt for k in _sbf_price_map)]
                # Non-ns sizes <= 7.5 as columns; ns mapped to new row
                _sbf_update_szs = [sz for sz in SBF.ALL_SIZES if not sz.endswith('ns') and float(sz) <= 7.5 and any(k[1] == sz for k in _sbf_price_map)]
                _sbf_preview_rows = []
                for _wt in _sbf_update_wts:
                    _row = {"Waste Type": _wt}
                    for _sz in _sbf_update_szs:
                        _val = _sbf_price_map.get((_wt, _sz))
                        _row[f"{_sz} m\u00b3"] = f"${_val:,.0f}" if _val is not None else "N/A"
                    _sbf_preview_rows.append(_row)
                # ns row
                _sbf_ns_row = {"Waste Type": "Mixed Heavy Waste (With no Soild & Dirt)"}
                for _sz in _sbf_update_szs:
                    _ns_key = _sz + "ns"
                    _ns_val = None
                    for _wt3 in _sbf_update_wts:
                        _v3 = _sbf_price_map.get((_wt3, _ns_key))
                        if _v3 is not None and (_ns_val is None or _v3 < _ns_val):
                            _ns_val = _v3
                    _sbf_ns_row[f"{_sz} m\u00b3"] = f"${_ns_val:,.0f}" if _ns_val is not None else "N/A"
                if any(v != "N/A" for k, v in _sbf_ns_row.items() if k != "Waste Type"):
                    _sbf_preview_rows.append(_sbf_ns_row)
                _df_sbf_preview = pd.DataFrame(_sbf_preview_rows).set_index("Waste Type")
                st.dataframe(_df_sbf_preview, width='stretch')

                _sbf_copy_col, _sbf_dl_col = st.columns([1, 1])
                with _sbf_copy_col:
                    _sbf_csv_text = _df_sbf_preview.to_csv(index=True)
                    st.download_button(
                        "📋 Download as CSV",
                        data=_sbf_csv_text,
                        file_name="sbf_update_price.csv",
                        mime="text/csv",
                        width='stretch',
                        key="sbf_dl_csv",
                    )
                with _sbf_dl_col:
                    import json as _json2
                    _SBF_WASTE_URLS = {
                        'General Waste':       'https://www.skipbinfinder.com.au/supplier/rates_manage.php',
                        'Mixed Heavy Waste':   'https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavy.php',
                        'Mixed Heavy Waste (With no Soild & Dirt)': 'https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavynosoildirt.php',
                        'Concrete / Bricks':   'https://www.skipbinfinder.com.au/supplier/rates_manage_clean.php',
                        'Green Garden Waste':  'https://www.skipbinfinder.com.au/supplier/rates_manage_green.php',
                        'Soil / Dirt':         'https://www.skipbinfinder.com.au/supplier/rates_manage_dirt.php',
                    }
                    _sbf_min_date = _dod_to_min_date(saved_dod)
                    _sbf_json_data = {}
                    for _wt2 in _sbf_update_wts:
                        _sbf_json_data[_wt2] = {}
                        _base_url2 = _SBF_WASTE_URLS.get(_wt2, '')
                        if _base_url2 and _sbf_min_date:
                            _sbf_json_data[_wt2]['_url'] = _base_url2 + '?min_date=' + _sbf_min_date
                        for _sz2 in _sbf_update_szs:
                            _val2 = _sbf_price_map.get((_wt2, _sz2))
                            if _val2 is not None:
                                _sbf_json_data[_wt2][_sz2] = _val2
                    # Include ns row in JSON export
                    _sbf_ns_export = {}
                    _ns_base_url = _SBF_WASTE_URLS.get('Mixed Heavy Waste (With no Soild & Dirt)', '')
                    if _ns_base_url and _sbf_min_date:
                        _sbf_ns_export['_url'] = _ns_base_url + '?min_date=' + _sbf_min_date
                    for _sz2 in _sbf_update_szs:
                        _ns_key2 = _sz2 + "ns"
                        _ns_val2 = None
                        for _wt3b in _sbf_update_wts:
                            _v3b = _sbf_price_map.get((_wt3b, _ns_key2))
                            if _v3b is not None and (_ns_val2 is None or _v3b < _ns_val2):
                                _ns_val2 = _v3b
                        if _ns_val2 is not None:
                            _sbf_ns_export[_sz2] = _ns_val2
                    if any(k != '_url' for k in _sbf_ns_export):
                        _sbf_json_data["Mixed Heavy Waste (With no Soild & Dirt)"] = _sbf_ns_export
                    _sbf_json_text = _json2.dumps(_sbf_json_data, indent=2)
                    _sbf_copy_js = f"""
                    <textarea id="_sbf_cp_buf" style="position:absolute;left:-9999px">{_sbf_json_text}</textarea>
                    <button onclick="var t=document.getElementById('_sbf_cp_buf');t.select();document.execCommand('copy');this.innerText='✅ Copied!';"
                        style="width:100%;padding:0.4rem 0.8rem;background:#ff4b4b;color:white;border:none;border-radius:4px;cursor:pointer;font-size:0.9rem;">
                        📋 Copy as JSON
                    </button>"""
                    st.components.v1.html(_sbf_copy_js, height=42)
            else:
                st.info("Run a search first to populate the price map.")

    # -----------------------------------------------------------------------
    # Sub-tab: Sign In Information  (3 saved accounts, passwords locked)
    # -----------------------------------------------------------------------
    with sbf_tab_signin:
        st.subheader("🔐 Sign In Information — skipbinfinder.com.au")
        st.caption("Saved credentials for up to 3 SkipBinFinder supplier accounts. Passwords are hidden and protected.")
        st.markdown("---")

        sbf_accounts = st.session_state["sbf_accounts"]
        sbf_unlocked = st.session_state["sbf_acc_unlocked"]
        _SBF_ACC_CACHE = os.path.join(_CACHE_DIR, "sbf_accounts.json")

        for i in range(3):
            acc = sbf_accounts[i]
            with st.expander(
                f"**{acc['label']}**  —  Postcode: `{acc.get('postcode') or '(not set)'}`  |  Username: `{acc.get('username') or '(not set)'}`",
                expanded=True,
            ):
                v1, v2, v3 = st.columns([1, 1, 1])
                with v1:
                    new_pc = st.text_input(
                        "Postcode",
                        value=acc.get("postcode", ""),
                        key=f"sbf_postcode_{i}",
                    )
                with v2:
                    new_user = st.text_input(
                        "Username",
                        value=acc.get("username", ""),
                        key=f"sbf_user_{i}",
                    )
                with v3:
                    masked = "••••••••" if acc.get("password") else "(not set)"
                    st.text_input(
                        "Password",
                        value=masked,
                        disabled=True,
                        key=f"sbf_pwd_display_{i}",
                    )

                _changed = False
                if new_pc != acc.get("postcode", ""):
                    sbf_accounts[i]["postcode"] = new_pc
                    _changed = True
                if new_user != acc.get("username", ""):
                    sbf_accounts[i]["username"] = new_user
                    _changed = True
                if _changed:
                    _save_cache(_SBF_ACC_CACHE, sbf_accounts)
                    _gist_save_sbf(sbf_accounts)
                    st.rerun()

                if not sbf_unlocked[i]:
                    st.markdown("🔒 **Password is locked.** Enter the current password to unlock and change it.")
                    ul1, ul2 = st.columns([2, 1])
                    with ul1:
                        entered = st.text_input(
                            "Enter current password to unlock",
                            type="password",
                            key=f"sbf_unlock_{i}",
                        )
                    with ul2:
                        st.write("")
                        st.write("")
                        unlock_btn = st.button("🔓 Unlock", key=f"sbf_unlock_btn_{i}")

                    if unlock_btn:
                        if not acc.get("password") or entered == acc["password"]:
                            sbf_unlocked[i] = True
                            st.session_state["sbf_acc_unlocked"] = sbf_unlocked
                            st.rerun()
                        else:
                            st.error("❌ Incorrect password.")
                else:
                    st.markdown("🔓 **Unlocked.** Set a new password below.")
                    e1, e2, e3 = st.columns([2, 2, 1])
                    with e1:
                        new_pwd = st.text_input("New Password", type="password", key=f"sbf_newpwd_{i}")
                    with e2:
                        confirm_pwd = st.text_input("Confirm Password", type="password", key=f"sbf_confirmpwd_{i}")
                    with e3:
                        st.write("")
                        st.write("")
                        save_btn = st.button("💾 Save", key=f"sbf_savepwd_{i}", type="primary")

                    cancel_btn = st.button("🔒 Cancel & Lock", key=f"sbf_cancel_{i}")

                    if save_btn:
                        if not new_pwd:
                            st.error("New password cannot be empty.")
                        elif new_pwd != confirm_pwd:
                            st.error("❌ Passwords do not match.")
                        else:
                            sbf_accounts[i]["password"] = new_pwd
                            _save_cache(_SBF_ACC_CACHE, sbf_accounts)
                            _gist_save_sbf(sbf_accounts)
                            sbf_unlocked[i] = False
                            st.session_state["sbf_acc_unlocked"] = sbf_unlocked
                            st.success(f"✅ Password for {acc['label']} saved.")
                            st.rerun()

                    if cancel_btn:
                        sbf_unlocked[i] = False
                        st.session_state["sbf_acc_unlocked"] = sbf_unlocked
                        st.rerun()

    # -----------------------------------------------------------------------
    # Sub-tab: Test Action  (login & screenshot)
    # -----------------------------------------------------------------------
    with sbf_tab_test:
        st.subheader("🧪 Test Action — skipbinfinder.com.au")
        st.caption("Log in to the SkipBinFinder supplier portal and take a screenshot to verify credentials.")
        st.markdown("---")

        sbf_ta_accounts = st.session_state.get("sbf_accounts", [])

        # Build account labels
        _sbf_ta_labels = []
        for _a in sbf_ta_accounts:
            _lbl = _a.get("label", "Account")
            _uid = _a.get("username", "") or "(not set)"
            _sbf_ta_labels.append(f"{_lbl}  —  Username: {_uid}")

        sbf_ta_acc_idx = st.selectbox(
            "Account",
            options=list(range(len(_sbf_ta_labels))),
            format_func=lambda i: _sbf_ta_labels[i],
            key="sbf_ta_account",
        )

        _sbf_ta_sel = sbf_ta_accounts[sbf_ta_acc_idx] if sbf_ta_accounts else {}
        _sbf_ta_uid = _sbf_ta_sel.get("username", "")
        _sbf_ta_pwd = _sbf_ta_sel.get("password", "")

        _ta_col1, _ta_col2, _ta_col3 = st.columns(3)
        _ta_col1.metric("Supplier", "SkipBinFinder")
        _ta_col2.metric("Username", _sbf_ta_uid or "(not set)")
        _ta_col3.metric("Password", "••••••••" if _sbf_ta_pwd else "(not set)")

        st.markdown("")
        sbf_ta_btn = st.button(
            "🔐 Log In & Take Screenshot",
            type="primary",
            disabled=not _sbf_ta_uid or not _sbf_ta_pwd,
            key="sbf_ta_login_btn",
        )

        if not _sbf_ta_uid or not _sbf_ta_pwd:
            st.warning("⚠️ Selected account has no credentials set. Go to the Sign In Information tab to configure.")

        if sbf_ta_btn:
            with st.spinner("🔐 Logging in to SkipBinFinder…"):
                _ok, _msg, _shot = SBF.login(_sbf_ta_uid, _sbf_ta_pwd)

            if _ok:
                st.success(f"✅ {_msg}")
            else:
                st.error(f"❌ {_msg}")

            if _shot:
                st.image(_shot, caption="SkipBinFinder — Login screenshot", use_container_width=True)
            else:
                st.info("No screenshot captured.")

            st.session_state["sbf_ta_last"] = {
                "message": _msg,
                "screenshot": _shot,
            }

        # Show previous result if stored (persists across reruns within the same session)
        elif "sbf_ta_last" in st.session_state:
            _prev = st.session_state["sbf_ta_last"]
            st.markdown("---")
            st.subheader("📸 Last Screenshot")
            st.caption(_prev["message"])
            if _prev.get("screenshot"):
                st.image(_prev["screenshot"], caption="Previous login screenshot", use_container_width=True)

# ===========================================================================
# PAGE: SkipBinsOnline Sign In
# ===========================================================================

elif page == "SkipBinsOnline":
    st.title("🌐 SkipBinsOnline")
    st.markdown("---")

    sbo_tab_prices, sbo_tab_signin = st.tabs(["🔍 Check Price", "🔐 Sign In Information"])

    # -----------------------------------------------------------------------
    # Sub-tab: Check Price
    # -----------------------------------------------------------------------
    with sbo_tab_prices:
        st.subheader("Check Prices — skipbinsonline.com.au")
        st.caption("Search live prices from SkipBinsOnline for your postcode and hire period.")

        sbo_col1, sbo_col2, sbo_col3, sbo_col4 = st.columns([1, 1.4, 1.4, 1])
        with sbo_col1:
            sbo_postcode = st.text_input("Postcode", value="3173", key="sbo_pc")
        with sbo_col2:
            sbo_dod = st.text_input("Delivery Date (D/MM/YYYY)", value="1/04/2026", key="sbo_dod")
        with sbo_col3:
            sbo_pud = st.text_input("Pickup Date (D/MM/YYYY)", value="8/04/2026", key="sbo_pud")
        with sbo_col4:
            st.write("")
            st.write("")
            sbo_search = st.button("🔍 Search SkipBinsOnline", width='stretch', type="primary", key="sbo_search")

        if sbo_search:
            if not sbo_postcode or not sbo_dod or not sbo_pud:
                st.error("Please fill in all fields.")
                st.stop()

            def _run_sbo(run_fn, *args):
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

            with st.spinner("🔍 Fetching prices from SkipBinsOnline… (this may take a minute)"):
                _fetched = _run_sbo(SBO.run_search, sbo_postcode, sbo_dod, sbo_pud)

            if not _fetched:
                st.warning("No data returned from SkipBinsOnline. Check the postcode and dates, then try again.")
                st.session_state.pop("sbo_results", None)
            else:
                st.session_state["sbo_results"]    = _fetched
                st.session_state["sbo_search_pc"]  = sbo_postcode
                st.session_state["sbo_search_dod"] = sbo_dod
                st.session_state["sbo_search_pud"] = sbo_pud
                _ep_sbo = {}
                for _wt in _fetched:
                    _ep_sbo[_wt] = {}
                    for _sz, _pr in _fetched[_wt].items():
                        if isinstance(_pr, (int, float)):
                            _ep_sbo[_wt][_sz] = _pr
                st.session_state["sbo_edited_prices"] = _ep_sbo
                _save_cache(_SBO_CACHE, {
                    "results": _fetched,
                    "pc":      sbo_postcode,
                    "dod":     sbo_dod,
                    "pud":     sbo_pud,
                })

        if "sbo_results" in st.session_state:
            sbo_results  = st.session_state["sbo_results"]
            saved_pc  = st.session_state.get("sbo_search_pc", "")
            saved_dod = st.session_state.get("sbo_search_dod", "")
            saved_pud = st.session_state.get("sbo_search_pud", "")

            st.success(
                f"✅ Done — Postcode **{saved_pc}**  |  {saved_dod} → {saved_pud}  |  SkipBinsOnline prices loaded."
            )

            st.subheader("💰 Cheapest Available Price")
            cheapest_price = None
            cheapest_wt    = None
            cheapest_size  = None
            for wt, sizes in sbo_results.items():
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

            st.subheader("📋 All Available Sizes")
            _sbo_waste = {wt: list(sizes.keys()) for wt, sizes in sbo_results.items()}
            _sbo_lt75_all = [s for s in SBO.ALL_SIZES if float(s) <= 7.5]
            st.dataframe(_to_df(sbo_results, _sbo_waste, _sbo_lt75_all), width='stretch')

            st.markdown("---")

            # ---------------------------------------------------------------
            # Edited Price section (price − 1 for General Waste / Green Waste ≤ 12 m³)
            # ---------------------------------------------------------------
            st.subheader("✏️ Edited Price (< 7.5 m³,  price − 1)")

            _sbo_accs = st.session_state.get("sbo_accounts", [])
            _sbo_matched = [
                a for a in _sbo_accs
                if a.get("postcode", "").strip() == saved_pc.strip() and a.get("username", "").strip()
            ]
            _sbo_unmatched = [
                a for a in _sbo_accs
                if a.get("postcode", "").strip() != saved_pc.strip() and a.get("username", "").strip()
            ]
            if _sbo_matched:
                st.success(
                    "**Accounts linked to postcode " + saved_pc + ":** "
                    + "  |  ".join(f"✅ {a['label']}: `{a['username']}`" for a in _sbo_matched)
                )
            else:
                st.warning(f"⚠️ No saved account has postcode **{saved_pc}**.")
            if _sbo_unmatched:
                st.caption(
                    "Other accounts: "
                    + "  |  ".join(f"{a['label']}: `{a['username']}` (postcode {a.get('postcode') or 'not set'})" for a in _sbo_unmatched)
                )

            _sbo_lt75 = [s for s in SBO.ALL_SIZES if float(s) <= 7.5]
            _sbo_orig  = st.session_state.get("sbo_edited_prices", {})

            def _sbo_edit_sizes_for(wt):
                return [s for s in _sbo_lt75 if s in _sbo_orig.get(wt, {})]

            if not _sbo_orig:
                st.info("Run a price search above to populate the Edited Price table.")
            else:
                _sbo_edit_acc = _sbo_matched[0] if _sbo_matched else None
                _sbo_wt_list  = [wt for wt in sbo_results if _sbo_edit_sizes_for(wt)]
                _sbo_col_widths = [2] + [1] * len(_sbo_lt75) + [1, 1]

                if "sbo_wt_queue" not in st.session_state:
                    st.session_state["sbo_wt_queue"] = {}
                _sbo_queue = st.session_state["sbo_wt_queue"]

                _sbo_hdr = st.columns(_sbo_col_widths)
                _sbo_hdr[0].markdown("**Waste Type**")
                for _hi, _sz in enumerate(_sbo_lt75):
                    _sbo_hdr[1 + _hi].markdown(f"**{_sz} m³**")
                st.divider()

                _sbo_edit_btns = []
                _sbo_undo_btns = []
                for _wt_i, _wt in enumerate(_sbo_wt_list):
                    _row = st.columns(_sbo_col_widths)
                    _queued = _sbo_queue.get(_wt_i)
                    _name_label = f"{'⏳ ' if _queued else ''}{_wt}{'  *(undo)*' if _queued == 'undo' else '  *(edit)*' if _queued == 'edit' else ''}"
                    _row[0].markdown(_name_label)
                    for _ci, _sz in enumerate(_sbo_lt75):
                        _pr = _sbo_orig.get(_wt, {}).get(_sz)
                        if _pr is not None:
                            _row[1 + _ci].write(f"${_pr - 1:,.0f}")
                        else:
                            _row[1 + _ci].write("N/A")
                    _sbo_edit_btns.append(_row[-2].button(
                        "✏️ Edit", key=f"sbo_wt_edit_{_wt_i}",
                        disabled=_sbo_edit_acc is None, width='stretch',
                    ))
                    _sbo_undo_btns.append(_row[-1].button(
                        "↩️ Undo", key=f"sbo_wt_undo_{_wt_i}",
                        disabled=_sbo_edit_acc is None, width='stretch',
                    ))

                for _wt_i, _wt in enumerate(_sbo_wt_list):
                    if _sbo_edit_btns[_wt_i] and _sbo_edit_acc:
                        if _sbo_queue.get(_wt_i) == "edit":
                            del _sbo_queue[_wt_i]
                        else:
                            _sbo_queue[_wt_i] = "edit"
                        st.rerun()
                    if _sbo_undo_btns[_wt_i] and _sbo_edit_acc:
                        if _sbo_queue.get(_wt_i) == "undo":
                            del _sbo_queue[_wt_i]
                        else:
                            _sbo_queue[_wt_i] = "undo"
                        st.rerun()

                st.markdown("#### 🗂 Queue")
                if not _sbo_queue:
                    st.caption("No actions queued. Click ✏️ Edit or ↩️ Undo above to add.")
                else:
                    _q_hdr = st.columns([3, 1, 1])
                    _q_hdr[0].markdown("**Waste Type**")
                    _q_hdr[1].markdown("**Action**")
                    _q_hdr[2].markdown("**Remove**")
                    for _qi, (_q_idx, _q_action) in enumerate(list(_sbo_queue.items())):
                        _q_wt = _sbo_wt_list[_q_idx]
                        _qc = st.columns([3, 1, 1])
                        _qc[0].write(_q_wt)
                        _qc[1].write("✏️ Edit" if _q_action == "edit" else "↩️ Undo")
                        if _qc[2].button("🗑️", key=f"sbo_q_del_{_qi}_{_q_idx}", width='stretch'):
                            del _sbo_queue[_q_idx]
                            st.rerun()

                _sbo_last_run = st.session_state.get("sbo_last_run_summary", [])
                if _sbo_last_run:
                    st.markdown("#### 📋 Last Run Results")
                    _ok_items  = [r for r in _sbo_last_run if r["ok"]]
                    _err_items = [r for r in _sbo_last_run if not r["ok"]]
                    if _ok_items:
                        st.success("**Done:**\n" + "\n".join(f"- {r['msg']}" for r in _ok_items))
                    if _err_items:
                        st.error("**Failed:**\n" + "\n".join(f"- {r['msg']}" for r in _err_items))
                    for _rr in _sbo_last_run:
                        for _si, _shot in enumerate(_rr.get("screenshots") or []):
                            st.image(_shot, caption=f"Final result — {_rr['msg'][:60]}", width='stretch')
                    if st.button("✖ Clear results", key="sbo_clear_results"):
                        st.session_state["sbo_last_run_summary"] = []
                        st.rerun()
                    st.markdown("---")

                def _run_sbo_wt_edit(wt, undo=False):
                    idx      = _sbo_wt_list.index(wt)
                    wt_sizes = _sbo_edit_sizes_for(wt)
                    if undo:
                        wt_updates = [(s, str(int(_sbo_orig[wt][s]))) for s in wt_sizes]
                    else:
                        wt_updates = [(s, str(int(_sbo_orig[wt][s]) - 1)) for s in wt_sizes]
                    if not wt_updates:
                        result = {"ok": False, "msg": f"{wt}: No priced sizes < 7.5 m³ found."}
                        st.session_state[f"sbo_wt_result_{idx}"] = result
                        st.session_state.setdefault("sbo_last_run_summary", []).append(result)
                        return
                    ok, msg, shots = SBO.update_waste_type_rates(
                        _sbo_edit_acc["username"], _sbo_edit_acc["password"],
                        waste_type=wt, updates=wt_updates,
                        login_delay=6, edit_delay=3,
                        start_date=_dod_to_sbo_date(saved_dod) or None,
                    )
                    result = {"ok": ok, "msg": f"{wt} — {msg}", "screenshots": shots}
                    st.session_state[f"sbo_wt_result_{idx}"] = result
                    st.session_state.setdefault("sbo_last_run_summary", []).append(result)

                if _sbo_edit_acc:
                    import concurrent.futures as _cf
                    _sbo_btn_row = st.columns([1, 1, 1, 5])
                    _sbo_run_queue_btn = _sbo_btn_row[0].button(
                        f"▶ Run Queue ({len(_sbo_queue)})" if _sbo_queue else "▶ Run Queue",
                        key="sbo_run_queue",
                        disabled=not _sbo_queue,
                        width='stretch',
                    )
                    _sbo_edit_all = _sbo_btn_row[1].button("✏️ Edit All", key="sbo_edit_all", width='stretch')
                    _sbo_undo_all = _sbo_btn_row[2].button("↩️ Undo All", key="sbo_undo_all", width='stretch')

                    if _sbo_run_queue_btn and _sbo_queue:
                        _items = list(_sbo_queue.items())
                        _wts_to_run = [(_sbo_wt_list[i], v == "undo") for i, v in _items]
                        st.session_state["sbo_last_run_summary"] = []
                        with st.spinner(f"Running {len(_wts_to_run)} queued item(s) in parallel…"):
                            with _cf.ThreadPoolExecutor(max_workers=len(_wts_to_run)) as _pool:
                                _futures = [_pool.submit(_run_sbo_wt_edit, wt, undo) for wt, undo in _wts_to_run]
                                _cf.wait(_futures)
                        st.session_state["sbo_wt_queue"] = {}
                        st.rerun()

                    if _sbo_edit_all or _sbo_undo_all:
                        _is_undo = bool(_sbo_undo_all)
                        st.session_state["sbo_last_run_summary"] = []
                        with st.spinner(f"{'Undo' if _is_undo else 'Edit'} All: {len(_sbo_wt_list)} waste types in parallel…"):
                            with _cf.ThreadPoolExecutor(max_workers=len(_sbo_wt_list)) as _pool:
                                _futures = [_pool.submit(_run_sbo_wt_edit, wt, _is_undo) for wt in _sbo_wt_list]
                                _cf.wait(_futures)
                        st.session_state["sbo_wt_queue"] = {}
                        st.rerun()

    # -----------------------------------------------------------------------
    # Sub-tab: Sign In Information
    # -----------------------------------------------------------------------
    with sbo_tab_signin:
        st.subheader("🔐 Sign In Information — skipbinsonline.com.au")
        st.caption("Saved credentials for up to 3 SkipBinsOnline supplier accounts. Passwords are hidden and protected.")
        st.markdown("---")

        sbo_accounts = st.session_state["sbo_accounts"]
        sbo_unlocked = st.session_state["sbo_acc_unlocked"]
        _SBO_ACC_CACHE = os.path.join(_CACHE_DIR, "sbo_accounts.json")

        for i in range(3):
            acc = sbo_accounts[i]
            with st.expander(
                f"**{acc['label']}**  —  Postcode: `{acc.get('postcode') or '(not set)'}`  |  Username: `{acc.get('username') or '(not set)'}`",
                expanded=True,
            ):
                v1, v2, v3 = st.columns([1, 1, 1])
                with v1:
                    new_pc = st.text_input(
                        "Postcode",
                        value=acc.get("postcode", ""),
                        key=f"sbo_postcode_{i}",
                    )
                with v2:
                    new_user = st.text_input(
                        "Username",
                        value=acc.get("username", ""),
                        key=f"sbo_user_{i}",
                    )
                with v3:
                    masked = "••••••••" if acc.get("password") else "(not set)"
                    st.text_input(
                        "Password",
                        value=masked,
                        disabled=True,
                        key=f"sbo_pwd_display_{i}",
                    )

                _changed = False
                if new_pc != acc.get("postcode", ""):
                    sbo_accounts[i]["postcode"] = new_pc
                    _changed = True
                if new_user != acc.get("username", ""):
                    sbo_accounts[i]["username"] = new_user
                    _changed = True
                if _changed:
                    _save_cache(_SBO_ACC_CACHE, sbo_accounts)
                    _gist_save_sbo(sbo_accounts)
                    st.rerun()

                if not sbo_unlocked[i]:
                    st.markdown("🔒 **Password is locked.** Enter the current password to unlock and change it.")
                    ul1, ul2 = st.columns([2, 1])
                    with ul1:
                        entered = st.text_input(
                            "Enter current password to unlock",
                            type="password",
                            key=f"sbo_unlock_{i}",
                        )
                    with ul2:
                        st.write("")
                        st.write("")
                        unlock_btn = st.button("🔓 Unlock", key=f"sbo_unlock_btn_{i}")

                    if unlock_btn:
                        if not acc.get("password") or entered == acc["password"]:
                            sbo_unlocked[i] = True
                            st.session_state["sbo_acc_unlocked"] = sbo_unlocked
                            st.rerun()
                        else:
                            st.error("❌ Incorrect password.")
                else:
                    st.markdown("🔓 **Unlocked.** Set a new password below.")
                    e1, e2, e3 = st.columns([2, 2, 1])
                    with e1:
                        new_pwd = st.text_input("New Password", type="password", key=f"sbo_newpwd_{i}")
                    with e2:
                        confirm_pwd = st.text_input("Confirm Password", type="password", key=f"sbo_confirmpwd_{i}")
                    with e3:
                        st.write("")
                        st.write("")
                        save_btn = st.button("💾 Save", key=f"sbo_savepwd_{i}", type="primary")

                    cancel_btn = st.button("🔒 Cancel & Lock", key=f"sbo_cancel_{i}")

                    if save_btn:
                        if not new_pwd:
                            st.error("New password cannot be empty.")
                        elif new_pwd != confirm_pwd:
                            st.error("❌ Passwords do not match.")
                        else:
                            sbo_accounts[i]["password"] = new_pwd
                            _save_cache(_SBO_ACC_CACHE, sbo_accounts)
                            _gist_save_sbo(sbo_accounts)
                            sbo_unlocked[i] = False
                            st.session_state["sbo_acc_unlocked"] = sbo_unlocked
                            st.success(f"✅ Password for {acc['label']} saved.")
                            st.rerun()

                    if cancel_btn:
                        sbo_unlocked[i] = False
                        st.session_state["sbo_acc_unlocked"] = sbo_unlocked
                        st.rerun()
