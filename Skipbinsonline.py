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

_MODULE_VERSION = "3-tuple-returns"  # update_multiple_rates / update_waste_type_rates return (ok, msg, screenshot)


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


# ---------------------------------------------------------------------------
# Supplier rates management URLs per waste type
# ---------------------------------------------------------------------------

# Rates management uses a single page with cat_id query param (same cat_ids as the booking API)
WASTE_TYPE_RATES_URLS = {
    "General Waste":        "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=10",
    "Household":            "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=14",
    "Mixed Heavy Waste":    "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=12",
    "Mixed Waste w/ Soil":  "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=23",
    "Light Building Waste": "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=22",
    "Excavation Waste":     "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=16",
    "Mixed Rubble":         "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=15",
    "Clean Waste":          "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=13",
    "Green Waste":          "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php?cat_id=11",
}

_SUPPLIER_LOGIN_URL = "https://order.skipbinsonline.com.au/order/supplier/login.php"


# ---------------------------------------------------------------------------
# Supplier login  (Selenium)
# ---------------------------------------------------------------------------

def _make_screenshot_driver():
    """Build a Chrome driver with images enabled (for login/screenshot use)."""
    from Bookabin import _bundled_chrome_paths
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    chrome_bin, driver_bin = _bundled_chrome_paths()
    opts = Options()
    if chrome_bin:
        opts.binary_location = chrome_bin
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.page_load_strategy = 'normal'
    service = Service(executable_path=driver_bin) if driver_bin else Service()
    return webdriver.Chrome(service=service, options=opts)


def _do_login(driver, username: str, password: str, login_delay: float):
    """Perform login on the driver. Returns (success, message)."""
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get(_SUPPLIER_LOGIN_URL)
    wait = WebDriverWait(driver, 15)
    pwd_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))

    try:
        user_input = driver.find_element(
            By.XPATH,
            "//input[@type='password']/preceding::input[@type='text' or @type='email' or @type='number' or @type='tel'][1]",
        )
    except Exception:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='number'], input[type='tel']")
        user_input = inputs[-1] if inputs else None

    if user_input is None:
        return False, "Could not find username input field."

    user_input.clear()
    user_input.send_keys(username)
    pwd_input.clear()
    pwd_input.send_keys(password)

    try:
        login_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH,
             "//input[@type='submit'] | //button[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LOGIN') or contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'SIGN IN')]")
        ))
    except Exception:
        login_btn = driver.find_element(By.XPATH, "//input[@type='submit']")

    driver.execute_script("arguments[0].click();", login_btn)
    time.sleep(login_delay)

    src = driver.page_source.lower()
    if any(k in src for k in ("invalid", "incorrect", "error", "failed", "wrong password")):
        return False, "Login failed — invalid credentials."
    return True, "Logged in."


def login(username: str, password: str, login_delay: float = 5.0):
    """
    Log in to the SkipBinsOnline supplier portal using Selenium.
    login_delay : seconds to wait after clicking Login before taking the screenshot.
    Returns (success: bool, message: str, screenshot: bytes | None).
    """
    driver = _make_screenshot_driver()
    try:
        ok, msg = _do_login(driver, username, password, login_delay)
        shot = driver.get_screenshot_as_png()
        if not ok:
            return False, msg, shot
        src = driver.page_source.lower()
        if any(k in src for k in ("log out", "logout", "sign out", "welcome", "dashboard", "my account", "supplier dashboard")):
            return True, "Logged in successfully!", shot
        if any(k in src for k in ("invalid", "incorrect", "error", "failed", "wrong password")):
            return False, "Login failed — invalid credentials.", shot
        return True, "Login submitted — see screenshot.", shot
    except Exception as exc:
        try:
            shot = driver.get_screenshot_as_png()
        except Exception:
            shot = None
        return False, f"Error during login: {exc}", shot
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Supplier rate price updater  (Selenium)
# ---------------------------------------------------------------------------

def _update_single_row(driver, size: str, new_price: str, rates_url: str, edit_delay: float,
                       start_date: str = None):
    """
    Navigate to the edit form for a bin size, fill the price textarea/input and
    click the confirm button. Reuses an already-logged-in driver.
    URL format: waste_schedules.php?cat_id=XX&edit={size}&startDate=YY-MM-DD
    Returns (success: bool, message: str).
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    date_part = f"&startDate={start_date}" if start_date else ""
    edit_url = f"{rates_url}&edit={size}{date_part}"
    driver.get(edit_url)
    time.sleep(edit_delay)

    EDITABLE_CSS = (
        "textarea, "
        "input[type='text'], input[type='number'], "
        "input:not([type='button']):not([type='submit']):not([type='reset'])"
        ":not([type='image']):not([type='checkbox']):not([type='radio'])"
        ":not([type='hidden'])"
    )

    def _get_editable(ctx):
        els = ctx.find_elements(By.CSS_SELECTOR, EDITABLE_CSS)
        return [e for e in els if e.is_displayed() and e.is_enabled()
                and e.get_attribute("readonly") is None]

    # Wait up to 8 s for inputs to appear in main frame first
    editable = []
    frame_used = None
    for _ in range(8):
        editable = _get_editable(driver)
        if editable:
            break
        time.sleep(1)

    # If not in main frame, try every iframe
    if not editable:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in iframes:
            try:
                driver.switch_to.frame(frame)
                for _ in range(6):
                    editable = _get_editable(driver)
                    if editable:
                        frame_used = frame
                        break
                    time.sleep(1)
                if editable:
                    break
                driver.switch_to.default_content()
            except Exception:
                driver.switch_to.default_content()

    if not editable:
        driver.switch_to.default_content()
        return False, (
            f"{size} m³: no editable inputs/textareas found even inside iframes. "
            f"URL={driver.current_url}"
        )

    for el in editable:
        try:
            driver.execute_script("arguments[0].value = '';", el)
            el.click()
            el.send_keys(new_price)
        except Exception:
            pass

    # Find and click the save/confirm button (within whichever context we're in)
    confirm_btn = None
    for by, sel in [
        (By.CSS_SELECTOR, "input[type='image']"),
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.CSS_SELECTOR, "input[type='button']"),
        (By.XPATH, "//button[@type='submit']"),
        (By.XPATH, "//button"),
    ]:
        for el in driver.find_elements(by, sel):
            if el.is_displayed():
                confirm_btn = el
                break
        if confirm_btn:
            break

    if confirm_btn is None:
        driver.switch_to.default_content()
        return False, f"{size} m³: filled inputs but could not find confirm button."

    driver.execute_script("arguments[0].click();", confirm_btn)
    time.sleep(2)
    driver.switch_to.default_content()
    return True, f"{size} m³ → ${new_price} ✓"


def update_multiple_rates(username: str, password: str,
                          updates: list,
                          rates_url: str = None,
                          login_delay: float = 5.0,
                          edit_delay: float = 3.0,
                          start_date: str = None,
                          waste_type: str = None):
    """
    Log in ONCE then update multiple rows sequentially.
    Returns (success, message, screenshots_list).
    screenshots_list is a list of PNG bytes, one per navigation step.
    """
    import time
    import re as _re
    if rates_url is None:
        rates_url = WASTE_TYPE_RATES_URLS.get("General Waste", _SUPPLIER_LOGIN_URL)

    # Derive waste_type label from cat_id in rates_url if not supplied
    if waste_type is None:
        _m = _re.search(r"cat_id=(\d+)", rates_url)
        if _m:
            _cat = _m.group(1)
            waste_type = next((k for k, v in WASTE_TYPE_IDS.items() if v == _cat), None)

    _BASE_WS = "https://order.skipbinsonline.com.au/order/supplier/waste_schedules.php"

    driver = _make_screenshot_driver()
    results = []
    screenshots = []

    def _snap():
        try:
            screenshots.append(driver.get_screenshot_as_png())
        except Exception:
            pass

    def _click_btn(label):
        """Click an input[type=button] or button whose value/text contains label (case-insensitive)."""
        lo = label.lower()
        xpath = (
            f"//input[@type='button' and contains("
            f"translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{lo}')]"
            f" | //button[contains("
            f"translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{lo}')]"
            f" | //a[contains("
            f"translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{lo}')]"
        )
        from selenium.webdriver.common.by import By
        els = driver.find_elements(By.XPATH, xpath)
        for el in els:
            if el.is_displayed():
                driver.execute_script("arguments[0].click();", el)
                return True
        return False

    try:
        ok, msg = _do_login(driver, username, password, login_delay)
        if not ok:
            _snap()
            return False, msg, screenshots

        # ── Step 1: Go to base Waste Schedules page ──────────────────────────
        driver.get(_BASE_WS)
        import time
        time.sleep(2)
        _snap()   # screenshot 0: base page (should show Marrel button)

        # ── Step 2: Click Marrel ──────────────────────────────────────────────
        _click_btn("marrel")
        time.sleep(2)
        _snap()   # screenshot 1: after clicking Marrel (should show waste-type tabs)

        # ── Step 3: Click the correct waste type tab ──────────────────────────
        if waste_type:
            _click_btn(waste_type)
            time.sleep(2)
        _snap()   # screenshot 2: after clicking waste type (should show price table)

        # ── Step 4: Navigate to &save=true to confirm price-table view ────────
        date_part = f"&startDate={start_date}" if start_date else ""
        save_url = rates_url + date_part + "&save=true"
        driver.get(save_url)
        time.sleep(3)
        _snap()   # screenshot 3: price table full view

        # ── Step 5: Edit each size ─────────────────────────────────────────────
        for size_str, new_price in updates:
            ok, msg = _update_single_row(driver, size_str, new_price, rates_url, edit_delay,
                                         start_date=start_date)
            if ok:
                results.append(msg)
            else:
                results.append(f"{size_str} m\u00b3: \u274c {msg}")

        all_ok = all("\u274c" not in r for r in results)
        summary = "  |  ".join(results)
        return all_ok, summary, screenshots

    except Exception as exc:
        summary = ("  |  ".join(results) + f"  |  ERROR: {exc}").lstrip("  |  ")
        _snap()
        return False, summary, screenshots
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def update_waste_type_rates(username: str, password: str,
                            waste_type: str,
                            updates: list,
                            login_delay: float = 5.0,
                            edit_delay: float = 3.0,
                            start_date: str = None):
    """
    Log in and update prices for a specific waste type.
    Returns (success, message, screenshots_list).
    """
    rates_url = WASTE_TYPE_RATES_URLS.get(waste_type)
    if not rates_url:
        return False, f"No rates URL configured for waste type: {waste_type}", []

    return update_multiple_rates(
        username, password,
        updates=updates,
        rates_url=rates_url,
        login_delay=login_delay,
        edit_delay=edit_delay,
        start_date=start_date,
        waste_type=waste_type,
    )
