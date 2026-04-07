"""
Skipbinfinder.py  –  scraper module for skipbinfinder.com.au
Uses Selenium (headless Chrome) to navigate the 4-step booking wizard.

On step 4 (Bin Size page), ALL available sizes and their "Best Price" are shown
at once, so we only need ONE browser navigation per waste type.

Public API
----------
WASTE_TYPES  : dict  name -> list of cubic-metre strings
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
from concurrent.futures import ThreadPoolExecutor, as_completed

# Selenium is imported lazily inside _make_driver() so that importing this
# module does not block the UI on startup.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# waste_type IDs used in skipbinfinder URLs
WASTE_TYPE_IDS = {
    "General Waste":     1,
    "Mixed Heavy Waste": 2,
    "Concrete / Bricks": 3,
    "Green Garden Waste":4,
    "Soil / Dirt":       5,
}

# Available sizes per waste type (matched to what skipbinfinder.com.au actually lists)
WASTE_TYPES = {
    "General Waste":     ["2","2.5","3","3.5","4","5","5.5","6","7","8","9","10","11","12","14","15","16","20","30"],
    "Mixed Heavy Waste": ["2","3","4","5","6","7","8","9","10","11","12","15"],
    "Concrete / Bricks": ["2","3","4","5","6","7","8","9","10","11","12"],
    "Green Garden Waste":["2","3","3.5","4","5","6","7","8","9","10","11","12","14","15","20","25","30"],
    "Soil / Dirt":       ["2","3","4","5","6","7","8","9","10","12"],
}

ALL_SIZES = ["2","2ns","2.5","3","3ns","3.5","4","4ns","5","5ns","5.5","6","6ns","7","7ns","8","8ns","9","9ns","10","10ns","11","11ns","12","12ns","14","15","16","20","25","30"]

# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def _log(msg):
    """Append a line to sbf_debug.log next to the exe / script."""
    try:
        if getattr(sys, 'frozen', False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, 'sbf_debug.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass


def _bundled_chrome_paths():
    """
    Return (chrome_binary, chromedriver_exe) paths, in priority order:
      1. PyInstaller EXE  → bundled chrome-win64 / chromedriver-win64
      2. Linux system     → /usr/bin/chromium-browser (Streamlit Cloud, Ubuntu)
      3. Fallback         → (None, None)  relies on PATH / selenium-manager
    """
    import sys, os
    # 1. Frozen EXE bundle
    if getattr(sys, 'frozen', False):
        base   = sys._MEIPASS
        chrome = os.path.join(base, 'chrome-win64',       'chrome.exe')
        driver = os.path.join(base, 'chromedriver-win64',  'chromedriver.exe')
        if os.path.isfile(chrome) and os.path.isfile(driver):
            return chrome, driver
    # 2. Linux system Chromium (Streamlit Cloud / Ubuntu)
    import platform
    if platform.system() == 'Linux':
        for chrome_candidate in ('/usr/bin/chromium-browser', '/usr/bin/chromium'):
            if os.path.isfile(chrome_candidate):
                for driver_candidate in ('/usr/bin/chromedriver', '/usr/lib/chromium-browser/chromedriver'):
                    if os.path.isfile(driver_candidate):
                        return chrome_candidate, driver_candidate
                return chrome_candidate, None
    return None, None


def _make_driver(retries=3):
    import time
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium_stealth import stealth

    chrome_bin, driver_bin = _bundled_chrome_paths()

    for attempt in range(retries):
        try:
            opts = Options()
            if chrome_bin:
                opts.binary_location = chrome_bin
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--blink-settings=imagesEnabled=false")
            opts.add_argument("--window-size=1280,720")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            service = Service(executable_path=driver_bin) if driver_bin else Service()
            driver = webdriver.Chrome(service=service, options=opts)
            stealth(driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )
            return driver
        except Exception as exc:
            _log(f"[SBF] Chrome launch attempt {attempt+1}/{retries} failed: {exc}")
            if attempt < retries - 1:
                time.sleep(2)
    raise RuntimeError("Could not start Chrome after multiple attempts")


def _set_datepicker(driver, input_id, year, month_0indexed, day):
    """Set a jQuery UI datepicker value via the JS API."""
    driver.execute_script(
        "jQuery('#' + arguments[0]).datepicker('setDate', "
        "new Date(arguments[1], arguments[2], arguments[3]));",
        input_id, year, month_0indexed, day
    )


def _parse_dmy(date_str):
    """
    Parse D/MM/YYYY string to (year, month_0indexed, day).
    E.g. '1/04/2026' -> (2026, 3, 1)
    """
    parts = date_str.strip().split("/")
    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    return year, month - 1, day


def _parse_step4_prices(driver):
    """
    Parse the step-4 bin-size listing and return a dict: {size_str -> min_price}.
    'Trailer Bin' entries are skipped entirely.
    'Soil not accepted in this bin' entries are stored under 'Xns' key.

    Each bin card spans from its size line up to (but not including) the next
    size line, so labels from one card never bleed into an adjacent card.
    """
    from selenium.webdriver.common.by import By
    body_text = driver.find_element(By.TAG_NAME, "body").text

    lines     = [l.strip() for l in body_text.split('\n')]
    size_re   = re.compile(r'^(\d+(?:\.\d+)?)\s+cubic\s+met(?:er|re)s?$', re.IGNORECASE)
    price_re  = re.compile(r'^Best\s+Price$', re.IGNORECASE)
    dollar_re = re.compile(r'^\$([0-9,]+(?:\.\d+)?)$')

    # Collect all size-line indices first so we can define per-card boundaries
    size_positions = []
    for idx, ln in enumerate(lines):
        if size_re.match(ln):
            size_positions.append(idx)

    prices = {}

    for pos_i, i in enumerate(size_positions):
        size = size_re.match(lines[i]).group(1)

        # Card ends just before the next size line (or at most 20 lines ahead)
        next_size = size_positions[pos_i + 1] if pos_i + 1 < len(size_positions) else len(lines)
        card_end  = min(next_size, i + 20)

        card_lines = lines[i:card_end]

        # Skip Trailer Bin cards
        if any(re.search(r'trailer\s*bin', ln, re.IGNORECASE) for ln in card_lines):
            continue

        # Detect no-soil label anywhere within this card
        no_soil = any(re.search(r'soil\s+not\s+accepted', ln, re.IGNORECASE) for ln in card_lines)

        # Find Best Price within this card
        best_price = None
        for j, ln in enumerate(card_lines[1:], start=1):
            if price_re.match(ln):
                inline = re.search(r'\$([0-9,]+(?:\.\d+)?)', ln)
                if inline:
                    best_price = float(inline.group(1).replace(',', ''))
                elif j + 1 < len(card_lines):
                    dm = dollar_re.match(card_lines[j + 1])
                    if dm:
                        best_price = float(dm.group(1).replace(',', ''))
                break
            combined = re.match(r'Best\s+Price\s+\$([0-9,]+(?:\.\d+)?)', ln, re.IGNORECASE)
            if combined:
                best_price = float(combined.group(1).replace(',', ''))
                break

        key = size + 'ns' if no_soil else size

        if best_price is not None:
            if key not in prices or best_price < prices[key]:
                prices[key] = best_price

    _log(f"[SBF] _parse_step4_prices result: {prices}")
    return prices


# ---------------------------------------------------------------------------
# Per-waste-type fetch (one Selenium session per waste type)
# ---------------------------------------------------------------------------

def _fetch_waste_type(postcode, waste_type, dod, pud):
    """
    Navigate the SBF wizard for one waste type and return a dict {size -> price}.
    dod / pud are strings in D/MM/YYYY format.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    wait = None
    driver = None
    try:
        driver = _make_driver()
        wait   = WebDriverWait(driver, 60)

        # ── Step 1: postcode ────────────────────────────────────────────────
        driver.get("https://www.skipbinfinder.com.au/bin-hire/")
        import time as _time2
        # Allow Cloudflare JS challenge to complete before interacting
        _time2.sleep(6)
        try:
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[name='zip_code']")
            ))
        except Exception:
            body = driver.find_element(By.TAG_NAME, "body").text
            _log(f"[SBF] Timed out on postcode input for {waste_type}. Body:\n{body[:2000]}\n---")
            raise
        pcs = driver.find_elements(By.CSS_SELECTOR, "input[name='zip_code']")
        pcs[0].send_keys(postcode)
        next_btns = driver.find_elements(
            By.CSS_SELECTOR, "input[type='submit'][value='NEXT']"
        )
        driver.execute_script("arguments[0].click();", next_btns[0])

        # ── Step 2: waste type ───────────────────────────────────────────────
        wait.until(EC.presence_of_element_located(
            (By.XPATH, f"//span[normalize-space(text())='{waste_type}']")
        ))
        import time as _time
        _time.sleep(0.5)   # let DOM stabilise to avoid stale element references
        # Re-find elements after sleep to avoid stale references
        wt_spans = driver.find_elements(
            By.XPATH, f"//span[normalize-space(text())='{waste_type}']"
        )
        clicked = False
        for sp in wt_spans:
            try:
                if sp.is_displayed():
                    driver.execute_script("arguments[0].click();", sp)
                    clicked = True
                    break
            except Exception:
                # Element went stale, re-find and retry
                wt_spans = driver.find_elements(
                    By.XPATH, f"//span[normalize-space(text())='{waste_type}']"
                )
                for sp2 in wt_spans:
                    try:
                        if sp2.is_displayed():
                            driver.execute_script("arguments[0].click();", sp2)
                            clicked = True
                            break
                    except Exception:
                        continue
                break
        if not clicked:
            _log(f"[SBF] Could not click waste type span for '{waste_type}'")
            return {}

        # ── Step 3: dates ────────────────────────────────────────────────────
        wait.until(EC.presence_of_element_located((By.ID, "delivery_date")))
        yr_d, mo_d, dy_d = _parse_dmy(dod)
        yr_p, mo_p, dy_p = _parse_dmy(pud)
        _set_datepicker(driver, "delivery_date",   yr_d, mo_d, dy_d)
        _set_datepicker(driver, "collection_date", yr_p, mo_p, dy_p)

        # Click the "Next" button (step 3 button is a <button>)
        step3_next = driver.find_elements(
            By.XPATH, "//button[normalize-space(text())='Next']"
        )
        if not step3_next:
            # fallback: any visible button/input that says Next
            step3_next = driver.find_elements(
                By.XPATH,
                "//button[contains(translate(text(),'next','NEXT'),'NEXT')] | "
                "//input[@value='Next' or @value='NEXT']"
            )
        if not step3_next:
            return {}
        driver.execute_script("arguments[0].click();", step3_next[0])

        # ── Step 4: bin-size listing ─────────────────────────────────────────
        # Wait until at least one "Best Price" text appears
        wait.until(lambda d: "Best Price" in d.find_element(By.TAG_NAME, "body").text)

        prices = _parse_step4_prices(driver)
        if not prices:
            body = driver.find_element(By.TAG_NAME, "body").text
            _log(f"[SBF] No prices parsed for {waste_type}. Body snippet:\n{body[:3000]}\n---")
        return prices

    except Exception as exc:
        import traceback
        _log(f"[SBF] Exception for {waste_type}: {exc}\n{traceback.format_exc()}")
        return {}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bulk search (used by main.py)
# ---------------------------------------------------------------------------

def run_search(postcode, dod, pud, cell_q, status_q, done_event):
    """
    Fetch all waste-type × size combos by navigating to step-4 once per waste type.
    Posts results to cell_q as (waste_type_name, size_str, price_or_None).
    Posts progress strings to status_q.
    Sets done_event when finished.
    """
    total    = sum(len(v) for v in WASTE_TYPES.values())
    done_cnt = [0]
    lock     = threading.Lock()

    def fetch_one_waste_type(waste_name):
        sizes  = WASTE_TYPES[waste_name]
        # Try up to 2 times if first attempt returns nothing
        prices = _fetch_waste_type(postcode, waste_name, dod, pud)
        if not prices:
            import time as _t; _t.sleep(2)
            _log(f"[SBF] Retrying {waste_name} (first attempt returned empty)")
            prices = _fetch_waste_type(postcode, waste_name, dod, pud)

        for size in sizes:
            price = prices.get(size, None)
            cell_q.put((waste_name, size, price))
            with lock:
                done_cnt[0] += 1
                status_q.put(
                    f"SBF  {done_cnt[0]}/{total}  –  {waste_name}  {size} m³"
                )

        # Emit any no-soil variants found (keyed as "Xns")
        for key, price in prices.items():
            if key.endswith('ns'):
                cell_q.put((waste_name, key, price))
                with lock:
                    status_q.put(
                        f"SBF  –  {waste_name}  {key[:-2]} m³ (no soil)"
                    )

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [
            ex.submit(fetch_one_waste_type, wt)
            for wt in WASTE_TYPES
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as exc:
                _log(f"[SBF] Thread exception: {exc}")

    done_event.set()
