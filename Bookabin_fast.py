"""
Fast, browser-free BookABin scraper.

The public quote flow is a classic ASP.NET WebForms wizard:

  step 3  GET  /skip_bins_{pc}_{wtid}.aspx
          -> hidden __VIEWSTATE / __EVENTVALIDATION
          -> one "bin size" link per size, each a __EVENTTARGET postback
  step 4  POST /skip_bins_{pc}_{wtid}.aspx   __EVENTTARGET=lvBinSizes$ctrlN$SelectButton
          -> the date-entry page for that bin size
  step 5  POST /skip_bins_{pc}_{wtid}_{ssid}.aspx
          __EVENTTARGET=btnGetBestPrice + txtDateFrom / txtDateTo
          -> "Your best price is $X including GST"

No JavaScript is involved, so `requests` is enough.  This is roughly 5-10x
faster and far lighter than driving headless Chrome, which matters a lot on
the 1 vCPU / 1 GB Streamlit Community Cloud free tier.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_fast

# Waste types, available sizes, and BookABin IDs
WASTE_TYPES = {
    "General Waste":      ["2", "3", "4", "6", "7.5", "9", "11", "15", "30"],
    "Mixed Heavy Waste":  ["2", "3", "4", "6", "7.5", "9", "15"],
    "Cleanfill/Hardfill": ["2", "3", "4", "6", "7.5", "9"],
    "Green Garden Waste": ["2", "3", "4", "6", "7.5", "9", "11", "15", "30"],
    "Soil / Dirt":        ["2", "3", "4", "4.5", "6", "7.5", "9", "11"],
}

ALL_SIZES = ["2", "3", "4", "4.5", "6", "7.5", "9", "11", "15", "30"]

# Waste type IDs used in BookABin URLs (skip_bins_{pc}_{wtid}.aspx)
WASTE_WTID = {
    "General Waste":      1,
    "Cleanfill/Hardfill": 2,
    "Green Garden Waste": 3,
    "Soil / Dirt":        4,
    "Mixed Heavy Waste":  5,
}

DATE_FROM_NAME = "ctl00$ctl00$ContentBody$ContentMain$txtDateFrom"
DATE_TO_NAME   = "ctl00$ctl00$ContentBody$ContentMain$txtDateTo"
PRICE_BTN      = "ctl00$ctl00$ContentBody$ContentMain$btnGetBestPrice"
BIN_PREFIX     = "ctl00$ctl00$ContentBody$ContentMain$lvBinSizes$"

_SITE = "https://www.bookabin.com.au"

# One-off diagnostic guard
_logged = []


def _log(msg):
    line = f"[BookabinFast] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _logged:
        return
    _logged.append(True)
    try:
        import os
        base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or "."
        d = os.path.join(base, "skipbin_cache")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "bookabin_fast.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sizes_on_page(html):
    """
    Return [(ctrl, size_str)] in DOM order, taken from the bin-size cards.

    The markup is:
        <div class="binTitle" id="2">
            <a id="...lvBinSizes_ctrl0_SelectButton" href="javascript:...$ctrl0$SelectButton...">
            2 cubic metres
    """
    out = []
    for m in re.finditer(
        r'<div class="binTitle" id="([^"]+)".*?'
        r'lvBinSizes\$(ctrl\d+)\$SelectButton',
        html, re.I | re.S,
    ):
        size, ctrl = m.group(1).strip(), m.group(2)
        out.append((ctrl, size))
    return out


def _parse_price(html):
    """
    Extract the best price from the step-5 result page.

    Mirrors the Selenium scraper exactly:
      1. "best price" followed by the first dollar amount
      2. "Total Charge $X"
    so both implementations report identical numbers.
    """
    text = http_fast.html_to_text(html)
    m = re.search(r"best price[^$]*\$([0-9,.]+)", text, re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"Total Charge\s*\$([0-9,.]+)", text, re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _fetch_size(waste_type, wtid, size, ctrl, dod, pud, step3_html, step3_fields):
    """Fetch one waste-type/size price.  Returns a float or None."""
    url3 = f"{_SITE}/skip_bins_{_PC[0]}_{wtid}.aspx"
    s = http_fast.thread_session(referer=url3)

    # ── step 4: postback that selects the bin size ──────────────────────
    data = dict(step3_fields)
    data["__EVENTTARGET"] = f"{BIN_PREFIX}{ctrl}$SelectButton"
    data["__EVENTARGUMENT"] = ""
    data["radioBinSizes"] = ctrl.replace("ctrl", "")
    resp = s.post(url3, data=data, timeout=30)
    resp.raise_for_status()

    # The postback either returns the date page directly or a small
    # "pageRedirect" stub naming the step-4 URL.
    step4_url = resp.url
    body = resp.text
    if "pageRedirect" in body:
        m = re.search(r"%2f(skip_bins_[^|]+?\.aspx)", body, re.I)
        if m:
            step4_url = f"{_SITE}/{m.group(1)}"
            body = s.get(step4_url, timeout=30).text
    elif "txtDateFrom" not in body:
        # Follow the redirect the server asked for.
        body = s.get(step4_url, timeout=30).text

    # ── step 5: post the dates, read the price ──────────────────────────
    d5 = http_fast.form_fields(body)
    d5["__EVENTTARGET"] = PRICE_BTN
    d5["__EVENTARGUMENT"] = ""
    d5[DATE_FROM_NAME] = dod
    d5[DATE_TO_NAME] = pud
    resp5 = s.post(step4_url, data=d5, timeout=30)
    resp5.raise_for_status()
    return _parse_price(resp5.text)


# The postcode is stored per-thread so _fetch_size can build the URL.
_PC = [None]


def fetch_group(postcode, waste_type, wtid, sizes, dod, pud):
    """Return {size: price} for one waste type using plain HTTP."""
    s = http_fast.thread_session(referer=f"{_SITE}/")
    url3 = f"{_SITE}/skip_bins_{postcode}_{wtid}.aspx"

    try:
        r3 = s.get(url3, timeout=30)
        r3.raise_for_status()
    except Exception as exc:
        _log(f"step-3 GET failed for {waste_type}: {exc!r}")
        return {size: None for size in sizes}

    html = r3.text
    cards = _sizes_on_page(html)
    if not cards:
        _log(f"no bin-size cards found for {waste_type} (url={url3})")
        return {size: None for size in sizes}

    fields = http_fast.form_fields(html)
    size_to_ctrl = {size: ctrl for ctrl, size in cards}

    out = {}
    for size in sizes:
        ctrl = size_to_ctrl.get(size)
        if ctrl is None:
            out[size] = None
            continue
        try:
            out[size] = _fetch_size(
                waste_type, wtid, size, ctrl, dod, pud, html, fields)
        except Exception as exc:
            _log(f"{waste_type} {size}m3 failed: {exc!r}")
            out[size] = None
    return out


def run_search(postcode, dod, pud, cell_q, status_q, done_event):
    """
    Fetch all BookABin waste-type x size prices concurrently over plain HTTP.

    Posts (waste_type, size, price_or_None) to cell_q and progress strings
    to status_q; sets done_event when finished.
    """
    total = sum(len(v) for v in WASTE_TYPES.values())
    done_cnt = [0]
    lock = threading.Lock()
    _PC[0] = postcode

    # One HTTP request per size is cheap and needs no browser, so a good
    # number of them can be in flight at once.
    try:
        from runtime_tuning import http_workers
        _workers = http_workers(6)
    except Exception:
        _workers = 6

    _log(f"fast run_search postcode={postcode} {dod}->{pud} workers={_workers}")

    def emit(waste_type, size, price):
        cell_q.put((waste_type, size, price))
        with lock:
            done_cnt[0] += 1
            status_q.put(
                f"Fetching...  {done_cnt[0]} / {total}  -  {waste_type}  {size} m3"
            )

    with ThreadPoolExecutor(max_workers=_workers) as ex:
        futures = {
            ex.submit(fetch_group, postcode, wt, WASTE_WTID[wt], sizes, dod, pud): wt
            for wt, sizes in WASTE_TYPES.items()
        }
        for f in as_completed(futures):
            wt = futures[f]
            try:
                prices = f.result()
            except Exception as exc:
                _log(f"group {wt} crashed: {exc!r}")
                prices = {size: None for size in WASTE_TYPES[wt]}
            for size, price in prices.items():
                emit(wt, size, price)

    done_event.set()
