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

def login(supplier_id: str, password: str):
    """
    Log in to https://bestpriceskipbins.com.au/supplier/ using Selenium.
    Returns (success: bool, message: str, screenshot: bytes | None).
    """
    import time
    from Bookabin import make_driver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = make_driver()
    driver.set_window_size(1280, 900)
    try:
        driver.get("https://bestpriceskipbins.com.au/supplier/")
        wait = WebDriverWait(driver, 15)

        # Wait for the password field
        pwd_input = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )

        # Supplier ID input — find the text/number input immediately before the password field
        try:
            user_input = driver.find_element(
                By.XPATH,
                "//input[@type='password']/preceding::input[@type='text' or @type='number' or @type='tel'][1]",
            )
        except Exception:
            inputs = driver.find_elements(
                By.CSS_SELECTOR, "input[type='text'], input[type='number'], input[type='tel']"
            )
            user_input = inputs[-1] if inputs else None

        if user_input is None:
            shot = driver.get_screenshot_as_png()
            return False, "Could not find the Supplier ID input field on the page.", shot

        user_input.clear()
        user_input.send_keys(supplier_id)
        pwd_input.clear()
        pwd_input.send_keys(password)

        # Click "Login Now!" — try submit input first, then any login button
        try:
            login_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//input[@type='submit'] | //button[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LOGIN')]")
                )
            )
        except Exception:
            # fallback: any clickable submit-ish element
            login_btn = driver.find_element(By.XPATH, "//input[@type='submit']")

        driver.execute_script("arguments[0].click();", login_btn)
        time.sleep(3)

        shot = driver.get_screenshot_as_png()
        src  = driver.page_source.lower()

        if any(k in src for k in ("log out", "logout", "sign out", "welcome", "dashboard", "my account", "supplier dashboard")):
            return True, "Logged in successfully!", shot
        if any(k in src for k in ("invalid", "incorrect", "error", "failed", "wrong password")):
            return False, "Login failed — invalid credentials.", shot
        # Unknown result — return screenshot for manual inspection
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
