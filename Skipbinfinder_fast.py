"""
Fast, browser-free SkipBinFinder scraper.

The quote wizard is a set of plain GET forms — no JavaScript required:

  step 1  GET  /bin-hire/?section=supplier&zip_code={pc}
          -> one radio per waste type; the radio's `value` attribute already
             contains the complete next-step URL
  step 2  GET  {that url}
          -> the date form
  step 3  GET  /bin-hire/?section=supplier&zip_code={pc}&waste_type={id}
                &cubic_metre=0&delivery_date=YYYY-MM-DD&collection_date=YYYY-MM-DD
          -> the step-4 listing with "N cubic meters  Best Price $X"

One HTTP request per waste type replaces a full Chrome session, which makes
the supplier roughly 8x faster and light enough for the 1 vCPU cloud tier.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_fast

WASTE_TYPES = {
    "General Waste":     ["2","2.5","3","3.5","4","5","5.5","6","7","8","9","10","11","12","14","15","16","20","30"],
    "Mixed Heavy Waste": ["2","3","4","5","6","7","8","9","10","11","12","15"],
    "Concrete / Bricks": ["2","3","4","5","6","7","8","9","10","11","12"],
    "Green Garden Waste":["2","3","3.5","4","5","6","7","8","9","10","11","12","14","15","20","25","30"],
    "Soil / Dirt":       ["2","3","4","5","6","7","8","9","10","12"],
}

ALL_SIZES = ["2","2ns","2.5","3","3ns","3.5","4","4ns","5","5ns","5.5","6","6ns","7","7ns","8","8ns","9","9ns","10","10ns","11","11ns","12","12ns","14","15","16","20","25","30"]

# Site waste_type ids — read from the radio labels on the postcode page:
#   1 General Waste   2 Mixed Heavy Waste   3 Concrete / Bricks
#   4 Green Garden Waste   5 Soil / Dirt
WASTE_TYPE_IDS = {
    "General Waste":      1,
    "Mixed Heavy Waste":  2,
    "Concrete / Bricks":  3,
    "Green Garden Waste": 4,
    "Soil / Dirt":        5,
}

_BASE = "https://www.skipbinfinder.com.au/bin-hire/"

_logged = []


def _log(msg):
    line = f"[SBFFast] {msg}"
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
        with open(os.path.join(d, "sbf_fast.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _to_iso(dmy):
    """D/MM/YYYY -> YYYY-MM-DD."""
    parts = str(dmy).strip().split("/")
    if len(parts) != 3:
        return ""
    day, month, year = parts
    try:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return ""


def _parse_prices(html):
    """
    Parse the step-4 bin-size listing into {size_str: min_price}.

    'Trailer Bin' entries are skipped, and bins that do not accept soil are
    additionally reported under a '<size>ns' key.
    """
    text = http_fast.html_to_lines(html)
    lines = text

    size_re   = re.compile(r"^(\d+(?:\.\d+)?)\s+cubic\s+met(?:er|re)s?$", re.I)
    price_re  = re.compile(r"^Best\s+Price$", re.I)
    dollar_re = re.compile(r"^\$([0-9,]+(?:\.\d+)?)$")

    positions = [i for i, ln in enumerate(lines) if size_re.match(ln)]
    prices = {}

    for pos_i, i in enumerate(positions):
        size = size_re.match(lines[i]).group(1)
        next_size = positions[pos_i + 1] if pos_i + 1 < len(positions) else len(lines)
        card = lines[i:min(next_size, i + 20)]

        if any(re.search(r"trailer\s*bin", ln, re.I) for ln in card):
            continue
        no_soil = any(re.search(r"soil\s+not\s+accepted", ln, re.I) for ln in card)

        best = None
        for j, ln in enumerate(card[1:], start=1):
            inline = re.search(r"Best\s+Price\s+\$([0-9,]+(?:\.\d+)?)", ln, re.I)
            if inline:
                best = float(inline.group(1).replace(",", ""))
                break
            if price_re.match(ln):
                m = re.search(r"\$([0-9,]+(?:\.\d+)?)", ln)
                if m:
                    best = float(m.group(1).replace(",", ""))
                elif j + 1 < len(card):
                    dm = dollar_re.match(card[j + 1])
                    if dm:
                        best = float(dm.group(1).replace(",", ""))
                break

        if best is None:
            continue
        key = size + "ns" if no_soil else size
        if key not in prices or best < prices[key]:
            prices[key] = best

    return prices


def _fetch_waste_type(postcode, waste_type, dod_iso, pud_iso):
    """One HTTP round-trip per waste type.  Returns {size: price}."""
    s = http_fast.thread_session(referer=_BASE)

    url = (f"{_BASE}?section=supplier&zip_code={postcode}"
           f"&waste_type={WASTE_TYPE_IDS[waste_type]}&cubic_metre=0"
           f"&delivery_date={dod_iso}&collection_date={pud_iso}&page_id=51")
    r = s.get(url, timeout=45)
    r.raise_for_status()

    text = http_fast.html_to_text(r.text)
    if "Best Price" not in text:
        raise RuntimeError("step-4 listing did not contain any prices")
    return _parse_prices(r.text)


def run_search(postcode, dod, pud, cell_q, status_q, done_event):
    """
    Fetch all SkipBinFinder waste-type x size prices concurrently over plain HTTP.

    Posts (waste_type, size, price_or_None) to cell_q and progress strings
    to status_q; sets done_event when finished.
    """
    total = sum(len(v) for v in WASTE_TYPES.values())
    done_cnt = [0]
    lock = threading.Lock()

    dod_iso = _to_iso(dod)
    pud_iso = _to_iso(pud)
    if not dod_iso or not pud_iso:
        # Fall back to the raw strings (the site accepts a few formats).
        dod_iso = dod
        pud_iso = pud

    # One request per waste type — no browser involved, so several can run
    # concurrently even on the 1 vCPU cloud tier.
    try:
        from runtime_tuning import http_workers
        _workers = http_workers(5)
    except Exception:
        _workers = 5

    _log(f"fast run_search postcode={postcode} {dod_iso}->{pud_iso} workers={_workers}")

    def emit(waste_type, size, price):
        cell_q.put((waste_type, size, price))
        with lock:
            done_cnt[0] += 1
            status_q.put(f"SBF  {done_cnt[0]}/{total}  –  {waste_type}  {size} m³")

    def one(waste_type):
        prices, err = http_fast.retry(
            lambda: _fetch_waste_type(postcode, waste_type, dod_iso, pud_iso),
            attempts=2, delay=2.0,
            what=f"SBF {waste_type}",
        )
        if err:
            _log(err)
        return waste_type, (prices or {})

    with ThreadPoolExecutor(max_workers=_workers) as ex:
        futures = [ex.submit(one, wt) for wt in WASTE_TYPES]
        for f in as_completed(futures):
            try:
                waste_type, prices = f.result()
            except Exception as exc:
                _log(f"worker crashed: {exc!r}")
                continue
            for size in WASTE_TYPES[waste_type]:
                emit(waste_type, size, prices.get(size))
            for key, price in prices.items():
                if key.endswith("ns"):
                    emit(waste_type, key, price)

    done_event.set()
