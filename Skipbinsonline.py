"""
Skipbinsonline.py  –  scraper module for skipbinsonline.com.au
Uses requests + BeautifulSoup only (no Selenium required).

Flow
----
1. GET  instantquote API  cmd=s1           → waste type list with cat_ids
2. GET  instantquote API  cmd=check.wastetype → available bin sizes for postcode + cat_id
3. POST binoptions.php    category + binsize + bintype=bestprice → best price page

Public API
----------
WASTE_TYPES  : dict  name -> list of cubic-metre strings (populated at first use)
ALL_SIZES    : list  all cubic-metre strings (union of all waste types)
run_search(postcode, dod, pud, cell_q, status_q, done_event)
    Posts (waste_type_name, size_str, price_or_None) to cell_q.
    Posts progress strings to status_q.
    Sets done_event when finished.
"""

import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup


def _log(msg):
    """Append a line to sbo_debug.log next to the exe / script."""
    try:
        if getattr(sys, 'frozen', False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, 'sbo_debug.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants – static waste type IDs (same for all postcodes on this site)
# ---------------------------------------------------------------------------

# Mapping from our display names to the site's cat_id values
WASTE_TYPE_IDS = {
    "General Waste":        "10",
    "Household":            "14",
    "Mixed Heavy Waste":    "12",
    "Mixed Waste w/ Soil":  "23",
    "Light Building Waste": "22",
    "Excavation Waste":     "16",
    "Mixed Rubble":         "15",
    "Clean Waste":          "13",
    "Green Waste":          "11",
}

# These are populated at first run per-postcode via the API.
# For the UI we pre-define a representative set based on General Waste sizes.
# The full accurate list is fetched dynamically in run_search.
WASTE_TYPES: dict = {}   # filled in at runtime

ALL_SIZES = [
    "1.5","2","2.5","3","3.5","4","5","5.5","6","7",
    "8","9","10","11","12","15","16","20",
]

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

_BASE_URL  = "https://order.skipbinsonline.com.au/order"
_API_URL   = _BASE_URL + "/system/instantquote.req.v3.php"
_ORDER_URL = _BASE_URL + "/customer/binoptions.php"


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":  (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer":            "https://www.skipbinsonline.com.au/",
        "Accept-Language":    "en-AU,en;q=0.9",
    })
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dmy_to_mdy(date_str: str) -> str:
    """Convert D/MM/YYYY → MM/DD/YYYY (the format that binoptions.php accepts)."""
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        return date_str
    day, month, year = parts
    return f"{int(month):02d}/{int(day):02d}/{year}"


def _fetch_waste_type_ids(session: requests.Session, postcode: str) -> dict:
    """Return {display_name: cat_id} from the live API for this postcode."""
    try:
        r = session.get(_API_URL, params={"cmd": "s1", "postcode": postcode, "t": "1"}, timeout=15)
        data = r.json()
        return {wt["title"]: wt["cat_id"] for wt in data.get("wasteTypes", [])}
    except Exception as _e:
        _log(f"[SBO] _fetch_waste_type_ids failed: {_e}\n{traceback.format_exc()}")
        return {}


def _fetch_bin_sizes(session: requests.Session, postcode: str, cat_id: str) -> list:
    """Return list of size strings available for this postcode + category."""
    try:
        r = session.get(
            _API_URL,
            params={"cmd": "check.wastetype", "postcode": postcode, "cat_id": cat_id, "t": "1"},
            timeout=15,
        )
        data = r.json()
        return data.get("bin_sizes", [])
    except Exception as _e:
        _log(f"[SBO] _fetch_bin_sizes failed (cat_id={cat_id}): {_e}")
        return []


def _fetch_price(session: requests.Session, postcode: str, cat_id: str,
                 size: str, delivery_mdy: str, collection_mdy: str):
    """
    POST to binoptions.php with bestprice bin type and parse "The best price found".
    Returns float price or None.
    """
    try:
        resp = session.post(_ORDER_URL, data={
            "newquote":        "1",
            "findmeabin":      "submit",
            "mattresses":      "0",
            "cartyres":        "0",
            "trucktyres":      "0",
            "postcode":        postcode,
            "category":        cat_id,
            "binsize":         size,
            "bintype":         "bestprice",
            "delivery_date":   delivery_mdy,
            "collection_date": collection_mdy,
        }, timeout=30)
        resp.raise_for_status()
    except Exception as _e:
        _log(f"[SBO] POST failed ({cat_id} {size}): {_e}")
        # Retry once on failure
        try:
            import time as _t; _t.sleep(1)
            resp = session.post(_ORDER_URL, data={
                "newquote":        "1",
                "findmeabin":      "submit",
                "mattresses":      "0",
                "cartyres":        "0",
                "trucktyres":      "0",
                "postcode":        postcode,
                "category":        cat_id,
                "binsize":         size,
                "bintype":         "bestprice",
                "delivery_date":   delivery_mdy,
                "collection_date": collection_mdy,
            }, timeout=30)
            resp.raise_for_status()
        except Exception as _e2:
            _log(f"[SBO] Retry also failed ({cat_id} {size}): {_e2}")
            return None

    body = BeautifulSoup(resp.text, "html.parser").get_text()

    # Primary: "The best price found:  $NNN.NN inc. of GST"
    m = re.search(
        r"[Tt]he\s+best\s+price\s+found\s*:.*?\$\s*([\d,]+(?:\.\d+)?)",
        body, re.DOTALL
    )
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Fallback: "Bin Total  $NNN"
    m2 = re.search(r"Bin\s+Total\s*\$\s*([\d,]+(?:\.\d+)?)", body)
    if m2:
        try:
            return float(m2.group(1).replace(",", ""))
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Bulk search (used by main.py)
# ---------------------------------------------------------------------------

def run_search(postcode: str, dod: str, pud: str,
               cell_q, status_q, done_event):
    """
    Fetch all waste-type × size combos concurrently.
    Posts results to cell_q as (waste_type_name, size_str, price_or_None).
    Posts progress strings to status_q.
    Sets done_event when finished.

    dod / pud must be in D/MM/YYYY format (same as the other modules).
    """
    global WASTE_TYPES

    session   = _make_session()
    del_mdy   = _parse_dmy_to_mdy(dod)
    col_mdy   = _parse_dmy_to_mdy(pud)

    # ── Step 1: get live waste-type IDs for this postcode ──────────────────
    live_ids = _fetch_waste_type_ids(session, postcode)
    # Build a cat_id map: use live data where available, else fall back to static
    cat_id_map = {}
    for display_name, static_id in WASTE_TYPE_IDS.items():
        # The site name might differ slightly (e.g. "Mixed Heavy Waste" vs "Mixed Heavy")
        # Try exact match first, then partial
        matched = None
        for live_name, live_id in live_ids.items():
            if live_name.lower() == display_name.lower():
                matched = live_id
                break
        if matched is None:
            for live_name, live_id in live_ids.items():
                if live_name.lower() in display_name.lower() or display_name.lower() in live_name.lower():
                    matched = live_id
                    break
        cat_id_map[display_name] = matched or static_id

    # ── Step 2: get available bin sizes per waste type ─────────────────────
    wt_sizes: dict = {}
    for display_name, cat_id in cat_id_map.items():
        sizes = _fetch_bin_sizes(session, postcode, cat_id)
        wt_sizes[display_name] = sizes if sizes else []

    # Update module-level WASTE_TYPES so the UI can reflect discovered sizes
    WASTE_TYPES = {k: v for k, v in wt_sizes.items() if v}

    total    = sum(len(v) for v in wt_sizes.values())
    done_cnt = [0]
    lock     = threading.Lock()

    def fetch_one(display_name, cat_id, size):
        task_session = _make_session()
        price = _fetch_price(task_session, postcode, cat_id, size, del_mdy, col_mdy)
        cell_q.put((display_name, size, price))
        with lock:
            done_cnt[0] += 1
            status_q.put(
                f"SBO  {done_cnt[0]}/{total}  –  {display_name}  {size} m³"
            )

    tasks = [
        (name, cat_id_map[name], sz)
        for name, sz_list in wt_sizes.items()
        for sz in sz_list
    ]

    _log(f"[SBO] run_search started: postcode={postcode} total={total} tasks")
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = [ex.submit(fetch_one, nm, cid, sz) for nm, cid, sz in tasks]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as _e:
                _log(f"[SBO] Unhandled thread error: {_e}")

    _log(f"[SBO] run_search done")
    done_event.set()
