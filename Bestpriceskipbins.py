"""
Bestpriceskipbins.py  –  scraper module for bestpriceskipbins.com.au
Uses requests + BeautifulSoup (no Selenium required).

Public API
----------
WASTE_TYPES  : dict  name -> list of cubic-metre strings
ALL_SIZES    : list  all cubic-metre strings
fetch_price(postcode, waste_type_name, size_str, delivery_date, collection_date)
    -> float price, or None if unavailable
"""

import os
import re
import sys
import threading
import traceback
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup


def _log(msg):
    """Append a line to bpsb_debug.log next to the exe / script."""
    try:
        if getattr(sys, 'frozen', False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(log_dir, 'bpsb_debug.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://bestpriceskipbins.com.au/bin-hire/"

# waste_type IDs on this site
WASTE_TYPE_IDS = {
    "General Waste":                    1,
    "Mixed Heavy Waste":                2,
    "Concrete / Bricks":                3,
    "Green Garden Waste":               4,
    "Soil / Dirt":                      5,
    "Mixed Heavy Waste (No Soil/Dirt)": 6,
}

WASTE_TYPES = {
    "General Waste":                    ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "14", "15", "16", "20", "25", "30"],
    "Mixed Heavy Waste":                ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
    "Concrete / Bricks":                ["2", "3", "4", "5", "6", "7", "8", "9", "10", "12"],
    "Green Garden Waste":               ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "14", "15", "20", "25", "30"],
    "Soil / Dirt":                      ["2", "3", "4", "5", "6", "7", "8", "9", "10", "12"],
    "Mixed Heavy Waste (No Soil/Dirt)": ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "14"],
}

ALL_SIZES = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "14", "15", "16", "20", "25", "30"]

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_price(postcode: str, waste_type_name: str, size_str: str,
                delivery_date: str, collection_date: str):
    """
    Fetch the best price from bestpriceskipbins.com.au.

    Parameters
    ----------
    postcode         : e.g. "3173"
    waste_type_name  : key from WASTE_TYPE_IDS
    size_str         : e.g. "4"  (cubic metres)
    delivery_date    : "dd-MM-yyyy"
    collection_date  : "dd-MM-yyyy"

    Returns
    -------
    float price, or None if unavailable / not serviced
    """
    wt_id = WASTE_TYPE_IDS.get(waste_type_name)
    if wt_id is None:
        return None

    params = {
        "section":         "supplier",
        "zip_code":        postcode,
        "waste_type":      wt_id,
        "cubic_metre":     size_str,
        "delivery_date":   delivery_date,
        "collection_date": collection_date,
        "page_id":         "119",
        "submit":          "submit",
    }

    try:
        resp = _SESSION.get(BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as _e:
        _log(f"[BPSB] Request failed ({waste_type_name} {size_str}): {_e}\n{traceback.format_exc()}")
        # Retry once on failure
        try:
            import time; time.sleep(1)
            resp = _SESSION.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as _e2:
            _log(f"[BPSB] Retry also failed ({waste_type_name} {size_str}): {_e2}")
            return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # --- 1. Step-5 payment page: price in <em> inside .price-title ---
    # e.g.  <h2 class="price-title">Your best price is <em> $432.00</em>…</h2>
    for el in soup.select(".price-title em, .price-head em"):
        m = re.search(r"\$([0-9,]+(?:\.\d+)?)", el.get_text())
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # --- 2. Alternate dates table (site returned slightly different date) ---
    # <td>$600.00</td> inside a table row
    alt_table = soup.find("table", class_="altcls")
    if alt_table:
        for td in alt_table.find_all("td"):
            m = re.match(r"^\s*\$([0-9,]+(?:\.\d+)?)\s*$", td.get_text())
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

    # --- 3. Any plain table cell that is just a dollar amount ---
    for td in soup.find_all("td"):
        m = re.match(r"^\s*\$([0-9,]+(?:\.\d+)?)\s*$", td.get_text())
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    return None


# ---------------------------------------------------------------------------
# Bulk search (used by Main.py)
# ---------------------------------------------------------------------------

def run_search(postcode, delivery_date, collection_date,
               cell_q, status_q, done_event):
    """
    Fetch all waste-type × size combos concurrently.
    Posts results to cell_q as (waste_type_name, size_str, price_or_None).
    Posts progress strings to status_q.
    Sets done_event when finished.
    """
    total    = sum(len(v) for v in WASTE_TYPES.items())
    total    = sum(len(v) for v in WASTE_TYPES.values())
    done_cnt = [0]
    lock     = threading.Lock()

    def fetch_one(waste_name, size):
        price = fetch_price(postcode, waste_name, size, delivery_date, collection_date)
        cell_q.put((waste_name, size, price))
        with lock:
            done_cnt[0] += 1
            status_q.put(
                f"BPSB  {done_cnt[0]}/{total}  –  {waste_name}  {size} m³"
            )

    tasks = [
        (wt, sz)
        for wt, sizes in WASTE_TYPES.items()
        for sz in sizes
    ]

    _log(f"[BPSB] run_search started: postcode={postcode} total={total} tasks")
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(fetch_one, wt, sz) for wt, sz in tasks]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as _e:
                _log(f"[BPSB] Unhandled thread error: {_e}")

    _log(f"[BPSB] run_search done")
    done_event.set()


# ---------------------------------------------------------------------------
# Supplier login  (Selenium)
# ---------------------------------------------------------------------------

def login(supplier_id: str, password: str, login_delay: float = 5.0):
    """
    Log in to https://bestpriceskipbins.com.au/supplier/ using Selenium.
    login_delay : seconds to wait after clicking Login before taking the screenshot.
    Returns (success: bool, message: str, screenshot: bytes | None).
    """
    driver = _make_screenshot_driver()
    try:
        ok, msg = _do_login(driver, supplier_id, password, login_delay)
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


def _do_login(driver, supplier_id: str, password: str, login_delay: float):
    """Perform login on the driver. Returns (success, message)."""
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get("https://bestpriceskipbins.com.au/supplier/")
    wait = WebDriverWait(driver, 15)
    pwd_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))

    try:
        user_input = driver.find_element(
            By.XPATH,
            "//input[@type='password']/preceding::input[@type='text' or @type='number' or @type='tel'][1]",
        )
    except Exception:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='number'], input[type='tel']")
        user_input = inputs[-1] if inputs else None

    if user_input is None:
        return False, "Could not find Supplier ID input field."

    user_input.clear()
    user_input.send_keys(supplier_id)
    pwd_input.clear()
    pwd_input.send_keys(password)

    try:
        login_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH,
             "//input[@type='submit'] | //button[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LOGIN')]")
        ))
    except Exception:
        login_btn = driver.find_element(By.XPATH, "//input[@type='submit']")

    driver.execute_script("arguments[0].click();", login_btn)
    time.sleep(login_delay)

    src = driver.page_source.lower()
    if any(k in src for k in ("invalid", "incorrect", "error", "failed", "wrong password")):
        return False, "Login failed — invalid credentials."
    return True, "Logged in."


def _get_row_id_map(driver, rates_url: str) -> dict:
    """
    Load the rates page and return a dict mapping bin size string to row id string.
    e.g. {"2": "1", "3": "2", "5": "7", ...}
    Row IDs are read from the edit pencil links (?action=edit&id=N).
    """
    import re
    import time

    driver.get(rates_url)
    time.sleep(2)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    id_map = {}
    current_size = None

    for row in soup.find_all("tr"):
        # Bin-size header cell has rowspan > 1, text like "2 cubic metres"
        for td in row.find_all("td"):
            try:
                if int(td.get("rowspan", 1)) > 1:
                    m = re.match(r"(\d+(?:\.\d+)?)\s*cubic", td.get_text(strip=True).lower())
                    if m:
                        current_size = m.group(1)
            except (ValueError, TypeError):
                pass

        if current_size and current_size not in id_map:
            for tag in row.find_all(True):
                for attr in ("href", "onclick"):
                    val = tag.get(attr, "")
                    m = re.search(
                        r"action=edit[^&]*&(?:amp;)?id=(\d+)|[?&]id=(\d+)[^&]*&(?:amp;)?action=edit",
                        val,
                    )
                    if m:
                        id_map[current_size] = m.group(1) or m.group(2)
                        break
                else:
                    continue
                break

    return id_map


def _update_single_row(driver, row_id: str, new_price: str, rates_url: str, edit_delay: float):
    """
    Navigate to the edit URL for row_id, fill all Price row inputs with new_price,
    click the confirm button. Reuses an already-logged-in driver.
    Returns (success: bool, message: str).
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    edit_url = f"{rates_url}?action=edit&id={row_id}"
    driver.get(edit_url)
    time.sleep(edit_delay)

    # Only inputs in the Price: row (not Stock)
    price_row_inputs = driver.find_elements(
        By.XPATH,
        "//td[normalize-space(text())='Price:']/ancestor::tr[1]//input[@type='text']"
    )
    visible_inputs = [inp for inp in price_row_inputs if inp.is_displayed()]

    if not visible_inputs:
        table_inputs = driver.find_elements(By.CSS_SELECTOR, "table input[type='text']")
        visible_inputs = [inp for inp in table_inputs if inp.is_displayed()]

    if not visible_inputs:
        return False, f"Row {row_id}: could not find price inputs."

    for inp in visible_inputs:
        inp.click()
        inp.send_keys(Keys.CONTROL + "a")
        inp.send_keys(new_price)

    last_input = visible_inputs[-1]
    confirm_btn = None
    for by, sel in [
        (By.XPATH, ".//following::input[@type='image'][1]"),
        (By.XPATH, ".//following::input[@type='submit'][1]"),
        (By.XPATH, ".//following::button[1]"),
    ]:
        try:
            el = last_input.find_element(by, sel)
            if el.is_displayed():
                confirm_btn = el
                break
        except Exception:
            continue

    if confirm_btn is None:
        for by, sel in [
            (By.CSS_SELECTOR, "table input[type='image']"),
            (By.CSS_SELECTOR, "table input[type='submit']"),
        ]:
            els = driver.find_elements(by, sel)
            for el in els:
                if el.is_displayed():
                    confirm_btn = el
                    break
            if confirm_btn:
                break

    if confirm_btn is None:
        return False, f"Row {row_id}: could not find confirm (✓) button."

    driver.execute_script("arguments[0].click();", confirm_btn)
    time.sleep(2)
    return True, f"Row {row_id} → ${new_price} ✓"


def update_rate_price(supplier_id: str, password: str, row_id: str, new_price: str,
                      rates_url: str = "https://bestpriceskipbins.com.au/supplier/rates_manage.php",
                      login_delay: float = 5.0, edit_delay: float = 5.0):
    """
    Log in and update a single row. Returns (success, message, screenshot).
    """
    driver = _make_screenshot_driver()
    try:
        ok, msg = _do_login(driver, supplier_id, password, login_delay)
        if not ok:
            return False, msg, driver.get_screenshot_as_png()

        ok, msg = _update_single_row(driver, row_id, new_price, rates_url, edit_delay)
        shot = driver.get_screenshot_as_png()
        return ok, msg, shot

    except Exception as exc:
        try:
            shot = driver.get_screenshot_as_png()
        except Exception:
            shot = None
        return False, f"Error during price update: {exc}", shot
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def update_multiple_rates(supplier_id: str, password: str,
                          updates: list,
                          rates_url: str = "https://bestpriceskipbins.com.au/supplier/rates_manage.php",
                          login_delay: float = 5.0, edit_delay: float = 3.0):
    """
    Log in ONCE then update multiple rows sequentially.
    updates : list of (size_str, new_price_str) e.g. [("2", "189"), ("3", "308")].
    Row IDs are auto-detected from the rates page — no hardcoding needed.
    Returns (success, message, screenshot).
    """
    driver = _make_screenshot_driver()
    results = []
    try:
        ok, msg = _do_login(driver, supplier_id, password, login_delay)
        if not ok:
            return False, msg, driver.get_screenshot_as_png()

        # Auto-detect size → row_id mapping from the live page
        id_map = _get_row_id_map(driver, rates_url)

        for size_str, new_price in updates:
            row_id = id_map.get(size_str)
            if row_id is None:
                results.append(f"{size_str}m³: ❌ id not found (page has: {list(id_map.keys())})")
                continue
            ok, msg = _update_single_row(driver, row_id, new_price, rates_url, edit_delay)
            results.append(msg)

        shot = driver.get_screenshot_as_png()
        all_ok = all("❌" not in r for r in results)
        summary = "  |  ".join(results)
        return all_ok, summary, shot

    except Exception as exc:
        try:
            shot = driver.get_screenshot_as_png()
        except Exception:
            shot = None
        summary = ("  |  ".join(results) + f"  |  ERROR: {exc}").lstrip("  |  ")
        return False, summary, shot
    finally:
        try:
            driver.quit()
        except Exception:
            pass
