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


# ---------------------------------------------------------------------------
# Supplier login  (Selenium)
# ---------------------------------------------------------------------------

def _make_screenshot_driver():
    """Build a Chrome driver with images enabled (for login/screenshot use)."""
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


def login(username: str, password: str, login_delay: float = 5.0):
    """
    Log in to https://www.skipbinfinder.com.au/supplier/ using Selenium.
    login_delay : seconds to wait after clicking Login before taking the screenshot.
    Returns (success: bool, message: str, screenshot: bytes | None).
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = _make_screenshot_driver()
    try:
        driver.get("https://www.skipbinfinder.com.au/supplier/")
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
            shot = driver.get_screenshot_as_png()
            return False, "Could not find username input field.", shot

        user_input.clear()
        user_input.send_keys(username)
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

        shot = driver.get_screenshot_as_png()
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
# Supplier login helper (reusable on an existing driver)
# ---------------------------------------------------------------------------

def _do_login(driver, username: str, password: str, login_delay: float):
    """Perform login on an already-opened driver. Returns (success, message)."""
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get("https://www.skipbinfinder.com.au/supplier/")
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


# ---------------------------------------------------------------------------
# Supplier rate price updater  (Selenium)
# ---------------------------------------------------------------------------

WASTE_TYPE_RATES_URLS = {
    "General Waste":       "https://www.skipbinfinder.com.au/supplier/rates_manage.php",
    "Mixed Heavy Waste":   "https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavy.php",
    "Concrete / Bricks":   "https://www.skipbinfinder.com.au/supplier/rates_manage_clean.php",
    "Green Garden Waste":  "https://www.skipbinfinder.com.au/supplier/rates_manage_green.php",
    "Soil / Dirt":         "https://www.skipbinfinder.com.au/supplier/rates_manage_dirt.php",
    "Mixed Heavy Waste (With no Soild & Dirt)": "https://www.skipbinfinder.com.au/supplier/rates_manage_mixedheavynosoildirt.php",
}


def _get_row_id_map(driver, rates_url: str, min_date: str = None) -> dict:
    """
    Load the rates page and return a dict mapping bin size string to row id string.
    e.g. {"2": "1", "3": "2", "5": "7", ...}
    Row IDs are read from the edit pencil links (?action=edit&id=N).
    """
    import time
    from bs4 import BeautifulSoup

    nav_url = rates_url + (f"?min_date={min_date}" if min_date else "")
    driver.get(nav_url)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    id_map = {}
    current_size = None

    for row in soup.find_all("tr"):
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
                    if "action=edit" in val:
                        m = re.search(r"[?&](?:amp;)?id=(\d+)", val)
                        if m:
                            id_map[current_size] = m.group(1)
                            break
                else:
                    continue
                break

    # Fallback: scan ALL links on the page for action=edit + id=N
    if not id_map:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "action=edit" not in href:
                continue
            m = re.search(r"[?&](?:amp;)?id=(\d+)", href)
            if not m:
                continue
            row_id = m.group(1)
            tr = a.find_parent("tr")
            if not tr:
                continue
            row_text = tr.get_text(" ", strip=True)
            sm = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:cubic|m³|m3)", row_text, re.IGNORECASE)
            if sm and sm.group(1) not in id_map:
                id_map[sm.group(1)] = row_id

    return id_map


def _update_single_row(driver, row_id: str, new_price: str, rates_url: str, edit_delay: float, min_date: str = None):
    """
    Navigate to the edit URL for row_id, fill all Price row inputs with new_price,
    click the confirm button. Reuses an already-logged-in driver.
    Returns (success: bool, message: str).
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    edit_url = f"{rates_url}?action=edit&id={row_id}"
    if min_date:
        edit_url += f"&min_date={min_date}"
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

    for inp in visible_inputs[:2]:
        inp.click()
        inp.send_keys(Keys.CONTROL + "a")
        inp.send_keys(new_price)

    last_input = visible_inputs[min(1, len(visible_inputs) - 1)]
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


def update_multiple_rates(username: str, password: str,
                          updates: list,
                          rates_url: str = "https://www.skipbinfinder.com.au/supplier/rates_manage.php",
                          login_delay: float = 5.0, edit_delay: float = 3.0,
                          min_date: str = None):
    """
    Log in ONCE then update multiple rows sequentially.
    updates : list of (size_str, new_price_str) e.g. [("2", "189"), ("3", "308")].
    Returns (success, message, screenshots).
    """
    driver = _make_screenshot_driver()
    results = []
    screenshots = []
    try:
        ok, msg = _do_login(driver, username, password, login_delay)
        if not ok:
            try:
                screenshots.append(driver.get_screenshot_as_png())
            except Exception:
                pass
            return False, msg, screenshots

        id_map = _get_row_id_map(driver, rates_url, min_date=min_date)

        if not id_map:
            try:
                screenshots.append(driver.get_screenshot_as_png())
            except Exception:
                pass
            return False, (
                f"Could not detect any row IDs on the rates page. "
                f"Page title: '{driver.title}' | URL: {driver.current_url}"
            ), screenshots

        for size_str, new_price in updates:
            row_id = id_map.get(size_str)
            if row_id is None:
                results.append(f"{size_str} m³: ❌ row not found (detected sizes: {list(id_map.keys())})")
                continue
            ok, msg = _update_single_row(driver, row_id, new_price, rates_url, edit_delay, min_date=min_date)
            if ok:
                results.append(f"{size_str} m³ → ${new_price} ✓")
            else:
                results.append(f"{size_str} m³: ❌ {msg}")

        try:
            screenshots.append(driver.get_screenshot_as_png())
        except Exception:
            pass

        all_ok = all("❌" not in r for r in results)
        summary = "  |  ".join(results)
        return all_ok, summary, screenshots

    except Exception as exc:
        try:
            screenshots.append(driver.get_screenshot_as_png())
        except Exception:
            pass
        summary = ("  |  ".join(results) + f"  |  ERROR: {exc}").lstrip("  |  ")
        return False, summary, screenshots
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def update_waste_type_rates(username: str, password: str,
                            waste_type: str,
                            updates: list,
                            min_date: str = None,
                            login_delay: float = 5.0,
                            edit_delay: float = 3.0):
    """
    Log in and update prices for a specific waste type.
    waste_type : key in WASTE_TYPE_RATES_URLS
    updates    : list of (size_str, new_price_str)
    min_date   : "YYYY-MM-DD" to target specific date column
    Returns (success, message, screenshots).
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
        min_date=min_date,
    )
