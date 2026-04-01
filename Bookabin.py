import re
import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

# Selenium is imported lazily inside make_driver() so that importing this
# module does not block the UI on startup.

# ---------------------------------------------------------------------------
# Waste types, available sizes, and BookABin IDs
# ---------------------------------------------------------------------------
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

DATE_FROM_ID = "ctl00_ctl00_ContentBody_ContentMain_txtDateFrom"
DATE_TO_ID   = "ctl00_ctl00_ContentBody_ContentMain_txtDateTo"
BTN_XPATH    = "//a[contains(@href,'btnGetBestPrice')]"


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

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


def make_driver(retries=3):
    import time
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
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-hang-monitor")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.page_load_strategy = 'eager'

    for attempt in range(retries):
        try:
            service = Service(executable_path=driver_bin) if driver_bin else Service()
            return webdriver.Chrome(service=service, options=opts)
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
    raise RuntimeError("Could not start Chrome after multiple attempts")


def login(username: str, password: str):
    """
    Log into https://www.bookabin.com.au/suppliers.aspx.
    Returns (success: bool, message: str, screenshot: bytes | None).
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # Use a larger window so the screenshot is readable
    driver = make_driver()
    driver.set_window_size(1280, 900)
    try:
        driver.get("https://www.bookabin.com.au/suppliers.aspx")
        wait = WebDriverWait(driver, 15)

        # Wait for the password field to appear
        pwd_input = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )

        # The supplier-id field is the visible text/tel/number input closest to
        # the login section.  Fall back to any visible text input on the page.
        try:
            user_input = driver.find_element(
                By.XPATH,
                "//input[@type='password']/preceding::input[@type='text' or @type='email' or @type='number'][1]",
            )
        except Exception:
            inputs = driver.find_elements(
                By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='number']"
            )
            user_input = inputs[-1] if inputs else None

        if user_input is None:
            shot = driver.get_screenshot_as_png()
            return False, "Could not find the Supplier ID input field on the page.", shot

        user_input.clear()
        user_input.send_keys(username)
        pwd_input.clear()
        pwd_input.send_keys(password)

        # The Log In element is an <a> tag that triggers ASP.NET PostBack
        login_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(@id,'btnLogin') or contains(@href,'btnLogin')]")
            )
        )
        driver.execute_script("arguments[0].click();", login_btn)

        time.sleep(3)

        shot = driver.get_screenshot_as_png()
        src = driver.page_source.lower()

        # Success indicators: the page shows logout / welcome / supplier dashboard
        if any(k in src for k in ("log out", "logout", "sign out", "welcome", "my account")):
            return True, "Logged in successfully!", shot
        # Failure indicators
        if any(k in src for k in ("invalid", "incorrect", "error", "failed")):
            return False, "Login failed: invalid credentials.", shot
        # Unknown — show screenshot so user can judge
        return True, "Login submitted — see screenshot below.", shot

    except Exception as exc:
        try:
            shot = driver.get_screenshot_as_png()
        except Exception:
            shot = None
        return False, f"Error during login: {exc}", shot
    finally:
        driver.quit()


def get_all_size_indices(driver, step3_url):
    """
    Load the step-3 page once and return a dict {size_str -> click_index}
    for all available sizes.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    wait = WebDriverWait(driver, 15)
    driver.get(step3_url)
    wait.until(EC.presence_of_element_located(
        (By.XPATH, "//*[contains(@href,'lvBinSizes')]")
    ))
    src = driver.page_source
    index_map = {}

    # Primary: match ctrl index from href + nearby size text (singular or plural)
    pairs = re.findall(
        r'ctrl(\d+)\$SelectButton.{0,300}?(\d+(?:\.\d+)?)\s*cubic\s*metres?',
        src, re.IGNORECASE
    )
    for ctrl_idx, found_size in pairs:
        if found_size not in index_map:
            index_map[found_size] = int(ctrl_idx)

    # Secondary: scan all lvBinSizes links in DOM order and match size from surrounding text
    if not index_map:
        bin_elems = driver.find_elements(By.XPATH, "//*[contains(@href,'lvBinSizes')]")
        for i, el in enumerate(bin_elems):
            try:
                # Look at the parent row text for a cubic size
                parent_text = el.find_element(By.XPATH, "./ancestor::tr[1]").text
            except Exception:
                parent_text = el.text
            m = re.search(r'(\d+(?:\.\d+)?)\s*cubic', parent_text, re.IGNORECASE)
            if m:
                size = m.group(1)
                if size not in index_map:
                    index_map[size] = i

    # Tertiary: body text line-number fallback
    if not index_map:
        body = driver.find_element(By.TAG_NAME, "body").text
        lines = [l.strip() for l in body.splitlines() if "cubic" in l.lower()]
        for i, line in enumerate(lines):
            m = re.search(r'(\d+(?:\.\d+)?)\s*cubic', line)
            if m and m.group(1) not in index_map:
                index_map[m.group(1)] = i

    return index_map


def get_best_price(driver, step3_url, size_idx, dod, pud, already_on_step3=False):
    """
    Click bin size at size_idx on step-3 page, fill dates, submit, parse price.
    Pass already_on_step3=True to skip reloading the page (saves one navigation).
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    wait = WebDriverWait(driver, 15)
    try:
        if not already_on_step3:
            driver.get(step3_url)
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//*[contains(@href,'lvBinSizes')]")
            ))
        bin_elems = driver.find_elements(By.XPATH, "//*[contains(@href,'lvBinSizes')]")
        if size_idx >= len(bin_elems):
            return None
        driver.execute_script("arguments[0].click();", bin_elems[size_idx])

        # Step 4: date entry
        wait.until(EC.presence_of_element_located((By.ID, DATE_FROM_ID)))
        f_from = driver.find_element(By.ID, DATE_FROM_ID)
        f_to   = driver.find_element(By.ID, DATE_TO_ID)
        f_from.clear(); f_from.send_keys(dod)
        f_to.clear();   f_to.send_keys(pud)

        # Click "GET BEST PRICE" (JS link, not a form submit)
        btn = driver.find_element(By.XPATH, BTN_XPATH)
        driver.execute_script("arguments[0].click();", btn)

        # Step 5: scrape best price
        wait.until(lambda d: (
            "best price"   in d.page_source.lower() or
            "total charge" in d.page_source.lower() or
            "invalid"      in d.page_source.lower() or
            "no suppliers" in d.page_source.lower()
        ))
        text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r"best price[^$]*\$([0-9,.]+)", text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
        m = re.search(r"Total Charge\s*\$([0-9,.]+)", text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Search logic (runs in background thread)
# ---------------------------------------------------------------------------

def run_search(postcode, dod, pud, cell_q, status_q, done_event):
    total    = sum(len(v) for v in WASTE_TYPES.values())
    done_cnt = [0]
    lock     = threading.Lock()

    def fetch_group(waste_type, sizes):
        wtid      = WASTE_WTID[waste_type]
        step3_url = f"https://www.bookabin.com.au/skip_bins_{postcode}_{wtid}.aspx"
        driver    = None
        try:
            driver = make_driver()
        except Exception:
            # Chrome could not start – mark all sizes as N/A
            for size in sizes:
                cell_q.put((waste_type, size, None))
                with lock:
                    done_cnt[0] += 1
                    status_q.put(
                        f"Fetching...  {done_cnt[0]} / {total}  -  {waste_type}  {size} m3  (Chrome failed)"
                    )
            return
        try:
            # Load step-3 once to get all size indices in bulk
            index_map = {}
            try:
                index_map = get_all_size_indices(driver, step3_url)
            except Exception:
                pass
            first_of_group = True
            for size in sizes:
                price_result = None
                idx = index_map.get(size)
                if idx is None:
                    price_result = "N/A"
                else:
                    # First size: page is already loaded from get_all_size_indices
                    # Subsequent sizes: must reload step-3 (after price result page)
                    price = get_best_price(
                        driver, step3_url, idx, dod, pud,
                        already_on_step3=first_of_group
                    )
                    if price is not None:
                        price_result = price
                    else:
                        # One retry with a fresh page load
                        price = get_best_price(driver, step3_url, idx, dod, pud)
                        if price is not None:
                            price_result = price
                first_of_group = False
                cell_q.put((waste_type, size, price_result))
                with lock:
                    done_cnt[0] += 1
                    status_q.put(
                        f"Fetching...  {done_cnt[0]} / {total}  -  {waste_type}  {size} m3"
                    )
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [
            ex.submit(fetch_group, wt, sizes)
            for wt, sizes in WASTE_TYPES.items()
        ]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    done_event.set()


# ---------------------------------------------------------------------------
# Rates management  (supplier portal: rates_manage.aspx)
# ---------------------------------------------------------------------------

RATES_URL_TPL = "https://www.bookabin.com.au/supplier/rates_manage.aspx?fromdate={date}"

def _login_driver(driver, supplier_id: str, password: str) -> bool:
    """
    Log into bookabin.com.au/suppliers.aspx using an existing driver.
    Returns True if login succeeded, False otherwise.
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver.get("https://www.bookabin.com.au/suppliers.aspx")
    wait = WebDriverWait(driver, 15)
    try:
        pwd_input = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        try:
            user_input = driver.find_element(
                By.XPATH,
                "//input[@type='password']/preceding::input[@type='text' or @type='email' or @type='number'][1]",
            )
        except Exception:
            inputs = driver.find_elements(
                By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='number']"
            )
            user_input = inputs[-1] if inputs else None

        if user_input is None:
            return False

        user_input.clear(); user_input.send_keys(supplier_id)
        pwd_input.clear();   pwd_input.send_keys(password)

        login_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//*[contains(@id,'btnLogin') or contains(@href,'btnLogin')]")
            )
        )
        driver.execute_script("arguments[0].click();", login_btn)
        time.sleep(3)

        src = driver.page_source.lower()
        return any(k in src for k in ("log out", "logout", "sign out", "welcome", "my account", "rates_manage", "manage"))
    except Exception:
        return False


def _get_header_columns(driver) -> list:
    """
    Read the column header texts from the rates table.
    Returns list of header strings (e.g. ['Description', 'Day 1', 'Day 2', ...]).
    """
    from selenium.webdriver.common.by import By
    headers = []
    try:
        # GridView header row is typically the first <tr> with <th> elements
        th_elems = driver.find_elements(By.XPATH, "//table//th")
        if th_elems:
            return [th.text.strip() for th in th_elems]
        # Fallback: first <tr> with only <td> — treat as header
        first_tr = driver.find_elements(By.XPATH, "//table//tr[1]/td")
        return [td.text.strip() for td in first_tr]
    except Exception:
        return headers


def _click_edit_row(driver, row_idx: int) -> bool:
    """
    Click the Edit link/button for the given row index in the GridView.
    The Edit control is typically the first <a> or <input type=submit> in the row
    whose text or value is "Edit".
    Returns True if clicked successfully.
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        # Find all Edit buttons/links in the table
        edit_btns = driver.find_elements(
            By.XPATH,
            "//table//tr[.//a[normalize-space(text())='Edit'] or "
            ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            "//a[normalize-space(text())='Edit'] | "
            "//table//tr[.//a[normalize-space(text())='Edit'] or "
            ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            "//input[@value='Edit'] | "
            "//table//tr[.//a[normalize-space(text())='Edit'] or "
            ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            "//button[normalize-space(text())='Edit']"
        )
        if row_idx < len(edit_btns):
            driver.execute_script("arguments[0].scrollIntoView(true);", edit_btns[row_idx])
            driver.execute_script("arguments[0].click();", edit_btns[row_idx])
            time.sleep(2)
            return True
    except Exception:
        pass
    return False


def _click_update_row(driver) -> bool:
    """
    Click the Update/Save link/button for the currently-editing row.
    Returns True if clicked successfully.
    """
    import time
    from selenium.webdriver.common.by import By
    try:
        update_btn = driver.find_element(
            By.XPATH,
            "//a[normalize-space(text())='Update'] | //input[@value='Update'] | "
            "//button[normalize-space(text())='Update'] | "
            "//a[normalize-space(text())='Save'] | //input[@value='Save'] | "
            "//button[normalize-space(text())='Save']"
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", update_btn)
        driver.execute_script("arguments[0].click();", update_btn)
        time.sleep(2)
        return True
    except Exception:
        return False


def _click_cancel_row(driver) -> bool:
    """Cancel edit mode for the current row."""
    import time
    from selenium.webdriver.common.by import By
    try:
        cancel_btn = driver.find_element(
            By.XPATH,
            "//a[normalize-space(text())='Cancel'] | //input[@value='Cancel'] | "
            "//button[normalize-space(text())='Cancel']"
        )
        driver.execute_script("arguments[0].click();", cancel_btn)
        time.sleep(1)
        return True
    except Exception:
        return False


def get_rates(supplier_id: str, password: str, from_date: str) -> dict:
    """
    Log in and scrape the current rates table from rates_manage.aspx.
    For each data row, clicks the "Edit" button to reveal the editable text
    inputs, captures all field IDs and current values, then cancels the edit
    before moving to the next row.

    Returns:
        {
            "ok":      True | False,
            "message": str,
            "headers": [str, ...],          # column header labels
            "rows": [
                {
                    "row_index":   int,     # 0-based position in the table
                    "description": str,     # bin size / waste type label (col 0)
                    "fields": {             # input field id -> current value
                        "<field_id>": "<value>", ...
                    },
                    "field_order": [str],   # field IDs in left-to-right column order
                }
            ]
        }
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = None
    try:
        driver = make_driver()
        if not _login_driver(driver, supplier_id, password):
            return {"ok": False, "message": "Login failed — check Supplier ID and password.", "rows": [], "headers": []}

        url = RATES_URL_TPL.format(date=from_date)
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: "rates" in d.page_source.lower() or "fromdate" in d.current_url.lower()
        )

        headers = _get_header_columns(driver)

        # Count data rows (rows that have an Edit button)
        def _count_edit_rows():
            return len(driver.find_elements(
                By.XPATH,
                "//table//tr[.//a[normalize-space(text())='Edit'] or "
                ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            ))

        num_rows = _count_edit_rows()
        if num_rows == 0:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:2000]
            return {
                "ok": True,
                "message": "Logged in but no rows with Edit buttons found. Raw preview below.",
                "headers": headers,
                "rows": [],
                "raw": body_text,
            }

        rows = []
        for row_idx in range(num_rows):
            # Re-count after each postback (row list may be re-rendered)
            current_count = _count_edit_rows()
            if row_idx >= current_count:
                break

            if not _click_edit_row(driver, row_idx):
                continue

            # Now the row is in edit mode — find the row containing inputs
            try:
                editing_tr = driver.find_element(
                    By.XPATH,
                    "//table//tr[.//input[@type='text'] or .//input[@type='number']]"
                )
                cells = editing_tr.find_elements(By.TAG_NAME, "td")
                # Description is usually the first non-input cell
                description = ""
                for cell in cells:
                    cell_text = cell.text.strip()
                    if cell_text and not cell.find_elements(By.XPATH, ".//input"):
                        description = cell_text
                        break
                if not description:
                    description = f"Row {row_idx}"

                inputs = editing_tr.find_elements(
                    By.XPATH, ".//input[@type='text' or @type='number']"
                )
                fields = {}
                field_order = []
                for inp in inputs:
                    fid = (inp.get_attribute("id") or inp.get_attribute("name") or
                           f"field_{row_idx}_{len(fields)}")
                    val = inp.get_attribute("value") or ""
                    fields[fid] = val
                    field_order.append(fid)

                rows.append({
                    "row_index":   row_idx,
                    "description": description,
                    "fields":      fields,
                    "field_order": field_order,
                })
            except Exception:
                pass

            # Cancel to return to read mode before next row
            _click_cancel_row(driver)

        return {"ok": True, "message": f"Loaded {len(rows)} rate rows.", "headers": headers, "rows": rows}

    except Exception as exc:
        return {"ok": False, "message": f"Error: {exc}", "rows": [], "headers": []}
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


def update_rates(supplier_id: str, password: str, from_date: str,
                 updates: dict, status_q=None) -> dict:
    """
    Log in, navigate to rates_manage.aspx, and update specific price fields.

    For each field_id in `updates`, this function:
      1. Clicks the Edit button on the row that owns that field.
      2. Fills the field with the new value.
      3. Clicks Update to save the row.

    updates: { "<field_id>": "<new_value>", ... }
             (field IDs come from get_rates() → rows[n]["fields"])

    Returns:
        {"ok": True|False, "message": str, "screenshot": bytes|None}
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def _status(msg):
        if status_q:
            status_q.put(msg)

    driver = None
    try:
        driver = make_driver()
        _status("Logging in…")
        if not _login_driver(driver, supplier_id, password):
            shot = driver.get_screenshot_as_png()
            return {"ok": False, "message": "Login failed — check Supplier ID and password.", "screenshot": shot}

        url = RATES_URL_TPL.format(date=from_date)
        _status(f"Loading rates page for {from_date}…")
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: "rates" in d.page_source.lower() or "fromdate" in d.current_url.lower()
        )

        # Group updates by row: find which row contains each field ID
        # by clicking Edit on each row, checking field IDs, then Cancel.
        def _count_edit_rows():
            return len(driver.find_elements(
                By.XPATH,
                "//table//tr[.//a[normalize-space(text())='Edit'] or "
                ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            ))

        # First pass: map field_id → row_index
        num_rows     = _count_edit_rows()
        field_to_row = {}   # field_id -> row_index
        for row_idx in range(num_rows):
            if _count_edit_rows() == 0:
                break
            if not _click_edit_row(driver, row_idx):
                continue
            try:
                tr = driver.find_element(
                    By.XPATH, "//table//tr[.//input[@type='text' or @type='number']]"
                )
                inputs = tr.find_elements(By.XPATH, ".//input[@type='text' or @type='number']")
                for inp in inputs:
                    fid = inp.get_attribute("id") or inp.get_attribute("name")
                    if fid and fid in updates:
                        field_to_row[fid] = row_idx
            except Exception:
                pass
            _click_cancel_row(driver)

        if not field_to_row:
            shot = driver.get_screenshot_as_png()
            return {"ok": False, "message": "Could not locate any of the requested field IDs on the page.", "screenshot": shot}

        # Second pass: group fields by row, click Edit, fill, click Update
        rows_to_update = {}
        for fid, ridx in field_to_row.items():
            rows_to_update.setdefault(ridx, {})[fid] = updates[fid]

        updated_count = 0
        for row_idx, row_fields in sorted(rows_to_update.items()):
            if not _click_edit_row(driver, row_idx):
                _status(f"⚠ Could not click Edit for row {row_idx}")
                continue
            for fid, new_value in row_fields.items():
                try:
                    el = driver.find_element(By.ID, fid)
                    driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    driver.execute_script("arguments[0].value = '';", el)
                    el.clear()
                    el.send_keys(str(new_value))
                    updated_count += 1
                    _status(f"Row {row_idx}: {fid} → {new_value}")
                except Exception:
                    _status(f"⚠ Row {row_idx}: could not fill {fid}")
            if not _click_update_row(driver):
                _status(f"⚠ Row {row_idx}: could not click Update — trying Cancel")
                _click_cancel_row(driver)

        shot = driver.get_screenshot_as_png()
        if updated_count == 0:
            return {"ok": False, "message": "No fields were updated — field IDs may have changed.", "screenshot": shot}
        return {"ok": True, "message": f"✅ {updated_count} field(s) updated across {len(rows_to_update)} row(s).", "screenshot": shot}

    except Exception as exc:
        shot = None
        try: shot = driver.get_screenshot_as_png()
        except Exception: pass
        return {"ok": False, "message": f"Error: {exc}", "screenshot": shot}
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


# ---------------------------------------------------------------------------
# Size / description matching helpers for auto-update
# ---------------------------------------------------------------------------

def _parse_size_from_description(description: str) -> str | None:
    """
    Extract a bin size (m³ value) from a rates-table row description string.
    E.g. "2 Cubic Metres" → "2", "4.5m3 General Waste" → "4.5"
    Returns the size as a string matching keys in ALL_SIZES, or None.
    """
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:cubic\s*m(?:etre)?s?|m3)', description, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: bare number at start of description
    m = re.search(r'^(\d+(?:\.\d+)?)', description.strip())
    if m:
        return m.group(1)
    return None


def _parse_waste_from_description(description: str) -> str | None:
    """
    Try to match a waste type name in a description string.
    Returns the key from WASTE_TYPES that matches best, or None.
    """
    desc_lower = description.lower()
    # Exact keyword checks in priority order
    checks = [
        ("cleanfill",     "Cleanfill/Hardfill"),
        ("hardfill",      "Cleanfill/Hardfill"),
        ("hard fill",     "Cleanfill/Hardfill"),
        ("clean fill",    "Cleanfill/Hardfill"),
        ("green",         "Green Garden Waste"),
        ("garden",        "Green Garden Waste"),
        ("soil",          "Soil / Dirt"),
        ("dirt",          "Soil / Dirt"),
        ("mixed heavy",   "Mixed Heavy Waste"),
        ("heavy",         "Mixed Heavy Waste"),
        ("general",       "General Waste"),
    ]
    for keyword, waste_type in checks:
        if keyword in desc_lower:
            return waste_type
    return None


def _lookup_price_from_results(
    bab_results: dict,
    size: str | None,
    waste_type: str | None,
) -> float | None:
    """
    Look up a price from the bab_results dict (waste_type → size → price).
    Falls back to searching all waste types for the given size if waste_type
    is unknown, returning the lowest numeric price found.
    """
    if size is None:
        return None

    # Direct lookup
    if waste_type and waste_type in bab_results:
        val = bab_results[waste_type].get(size)
        if isinstance(val, (int, float)):
            return float(val)

    # Fallback: scan all waste types for the size, take cheapest
    candidates = []
    for wt_data in bab_results.values():
        val = wt_data.get(size)
        if isinstance(val, (int, float)):
            candidates.append(float(val))
    return min(candidates) if candidates else None


def auto_update_rates(supplier_id: str, password: str, from_date: str,
                      bab_results: dict, status_q=None) -> dict:
    """
    Automatically update the "Day 1" (first) price column on rates_manage.aspx
    for every row, using prices from `bab_results` (the All Available Sizes
    search table) minus 1.

    For each editable row on the rates page:
      1. Parses the bin size and waste type from the row description.
      2. Looks up that size/type in bab_results.
      3. If found, sets the first text input in the row to  (price - 1).
      4. Clicks Update to save.

    bab_results format (same as session_state["bab_results"]):
        { waste_type_str: { size_str: price_float | "N/A" | None, ... }, ... }

    Returns:
        {
            "ok":         True | False,
            "message":    str,
            "updated":    [ {"description": str, "size": str, "old_price": str,
                             "new_price": float, "ok": bool}, ... ],
            "skipped":    [ {"description": str, "reason": str}, ... ],
            "screenshot": bytes | None,
        }
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def _status(msg):
        if status_q:
            status_q.put(msg)

    updated_rows = []
    skipped_rows = []
    driver = None
    try:
        driver = make_driver()
        _status("Logging in…")
        if not _login_driver(driver, supplier_id, password):
            shot = driver.get_screenshot_as_png()
            return {
                "ok": False, "message": "Login failed — check Supplier ID and password.",
                "updated": [], "skipped": [], "screenshot": shot,
            }

        url = RATES_URL_TPL.format(date=from_date)
        _status(f"Loading rates page for {from_date}…")
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: "rates" in d.page_source.lower() or "fromdate" in d.current_url.lower()
        )

        def _count_edit_rows():
            return len(driver.find_elements(
                By.XPATH,
                "//table//tr[.//a[normalize-space(text())='Edit'] or "
                ".//input[@value='Edit'] or .//button[normalize-space(text())='Edit']]"
            ))

        num_rows = _count_edit_rows()
        if num_rows == 0:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:1000]
            return {
                "ok": False,
                "message": "No rows with Edit buttons found on the rates page.",
                "updated": [], "skipped": [], "screenshot": driver.get_screenshot_as_png(),
                "raw": body_text,
            }

        _status(f"Found {num_rows} rows. Processing each row…")

        for row_idx in range(num_rows):
            # Re-check count after each postback
            current_count = _count_edit_rows()
            if row_idx >= current_count:
                _status(f"Row {row_idx}: no longer visible (postback reduced rows), stopping.")
                break

            # Click Edit to enter edit mode
            if not _click_edit_row(driver, row_idx):
                skipped_rows.append({"description": f"Row {row_idx}", "reason": "Could not click Edit button"})
                continue

            # Read the editing row
            description = f"Row {row_idx}"
            first_field_id  = None
            first_field_val = ""
            try:
                editing_tr = driver.find_element(
                    By.XPATH, "//table//tr[.//input[@type='text' or @type='number']]"
                )
                cells = editing_tr.find_elements(By.TAG_NAME, "td")
                # Grab description from the first non-input cell
                for cell in cells:
                    cell_text = cell.text.strip()
                    if cell_text and not cell.find_elements(By.XPATH, ".//input"):
                        description = cell_text
                        break

                # First text/number input = Day 1 price column
                inputs = editing_tr.find_elements(
                    By.XPATH, ".//input[@type='text' or @type='number']"
                )
                if inputs:
                    first_field_id  = inputs[0].get_attribute("id") or inputs[0].get_attribute("name")
                    first_field_val = inputs[0].get_attribute("value") or ""
            except Exception as e:
                skipped_rows.append({"description": description, "reason": f"Could not read editing row: {e}"})
                _click_cancel_row(driver)
                continue

            if not first_field_id:
                skipped_rows.append({"description": description, "reason": "No editable input fields found in row"})
                _click_cancel_row(driver)
                continue

            # Map description → size + waste type → price from bab_results
            size       = _parse_size_from_description(description)
            waste_type = _parse_waste_from_description(description)
            ref_price  = _lookup_price_from_results(bab_results, size, waste_type)

            _status(f"Row {row_idx}: '{description}' → size={size}, waste={waste_type}, ref=${ref_price}")

            if ref_price is None:
                skipped_rows.append({
                    "description": description,
                    "reason": f"No matching price in search results (size={size}, waste={waste_type})",
                })
                _click_cancel_row(driver)
                continue

            new_price = round(ref_price - 1, 2)

            # Fill the first-day-column field
            try:
                el = driver.find_element(By.ID, first_field_id) if first_field_id else None
                if el is None:
                    raise ValueError(f"Field {first_field_id} not found by ID")
                driver.execute_script("arguments[0].scrollIntoView(true);", el)
                driver.execute_script("arguments[0].value = '';", el)
                el.clear()
                el.send_keys(str(int(new_price) if new_price == int(new_price) else new_price))
            except Exception as e:
                skipped_rows.append({"description": description, "reason": f"Could not fill field: {e}"})
                _click_cancel_row(driver)
                continue

            # Click Update to save this row
            if _click_update_row(driver):
                updated_rows.append({
                    "description": description,
                    "size":        size or "?",
                    "old_price":   first_field_val,
                    "new_price":   new_price,
                    "ok":          True,
                })
                _status(f"  ✅ Updated '{description}': {first_field_val} → {new_price}")
            else:
                skipped_rows.append({"description": description, "reason": "Could not click Update button"})
                _click_cancel_row(driver)

        shot = driver.get_screenshot_as_png()
        n_ok   = len(updated_rows)
        n_skip = len(skipped_rows)
        msg = f"✅ {n_ok} row(s) updated, {n_skip} row(s) skipped."
        return {
            "ok":         n_ok > 0,
            "message":    msg,
            "updated":    updated_rows,
            "skipped":    skipped_rows,
            "screenshot": shot,
        }

    except Exception as exc:
        shot = None
        try: shot = driver.get_screenshot_as_png()
        except Exception: pass
        return {
            "ok": False, "message": f"Error: {exc}",
            "updated": updated_rows, "skipped": skipped_rows, "screenshot": shot,
        }
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def build_ui():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("SkipBin Price Checker - BookABin")
    root.resizable(True, True)

    FONT      = ("Segoe UI", 10)
    FONT_BOLD = ("Segoe UI", 10, "bold")

    # Blue header
    hdr = tk.Frame(root, bg="#1a73e8")
    hdr.grid(row=0, column=0, sticky="ew")
    root.grid_columnconfigure(0, weight=1)
    tk.Label(hdr, text="SkipBin Price Checker",
             font=("Segoe UI", 14, "bold"), fg="white", bg="#1a73e8",
             pady=10, padx=16).pack(side="left")
    tk.Label(hdr, text="bookabin.com.au  -  powered by Selenium",
             font=("Segoe UI", 9), fg="#cce0ff", bg="#1a73e8").pack(side="left")

    # Inputs
    inp = tk.Frame(root, pady=8, padx=12)
    inp.grid(row=1, column=0, sticky="w")

    tk.Label(inp, text="Postcode:", font=FONT_BOLD).grid(row=0, column=0, sticky="e", padx=(0, 4))
    postcode_var = tk.StringVar(value="3173")
    tk.Entry(inp, textvariable=postcode_var, font=FONT, width=10).grid(row=0, column=1, padx=(0, 18))

    tk.Label(inp, text="Delivery Date:", font=FONT_BOLD).grid(row=0, column=2, sticky="e", padx=(0, 4))
    delivery_var = tk.StringVar(value="1/04/2026")
    tk.Entry(inp, textvariable=delivery_var, font=FONT, width=13).grid(row=0, column=3, padx=(0, 18))

    tk.Label(inp, text="Pickup Date:", font=FONT_BOLD).grid(row=0, column=4, sticky="e", padx=(0, 4))
    pickup_var = tk.StringVar(value="2/04/2026")
    tk.Entry(inp, textvariable=pickup_var, font=FONT, width=13).grid(row=0, column=5, padx=(0, 18))

    tk.Label(inp, text="(D/MM/YYYY)", font=("Segoe UI", 8), fg="#aaa").grid(row=0, column=6, padx=(0, 18))

    btn = tk.Button(
        inp, text="  Get Prices  ", font=FONT_BOLD,
        bg="#1a73e8", fg="white", relief="flat", cursor="hand2", pady=4
    )
    btn.grid(row=0, column=7)

    # Table
    col_ids = ["waste_type"] + ALL_SIZES

    tbl = tk.Frame(root)
    tbl.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 0))
    root.grid_rowconfigure(2, weight=1)

    tree = ttk.Treeview(tbl, columns=col_ids, show="headings", height=7)
    tree.column("waste_type", width=165, anchor="w",      stretch=False)
    for s in ALL_SIZES:
        tree.column(s, width=78, anchor="center", stretch=False)

    tree.heading("waste_type", text="Waste Type")
    for s in ALL_SIZES:
        tree.heading(s, text=f"{s} m3")

    hsb = ttk.Scrollbar(tbl, orient="horizontal", command=tree.xview)
    vsb = ttk.Scrollbar(tbl, orient="vertical",   command=tree.yview)
    tree.configure(xscrollcommand=hsb.set, yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    tbl.grid_rowconfigure(0, weight=1)
    tbl.grid_columnconfigure(0, weight=1)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Treeview",         font=("Consolas", 10), rowheight=26)
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                    background="#2c5f9e", foreground="white")
    style.map("Treeview.Heading", background=[("active", "#1a4a7a")])
    tree.tag_configure("even", background="#eef4ff")
    tree.tag_configure("odd",  background="#ffffff")

    # Insert placeholder rows
    row_iids = {}
    for idx, wtype in enumerate(WASTE_TYPES):
        sizes = WASTE_TYPES[wtype]
        vals  = [wtype] + [
            "..." if s in sizes else "-"
            for s in ALL_SIZES
        ]
        iid = tree.insert("", "end", values=vals,
                          tags=("even" if idx % 2 == 0 else "odd",))
        row_iids[wtype] = iid

    # Status bar
    status_var = tk.StringVar(value="Enter postcode and dates, then click  Get Prices.")
    tk.Label(root, textvariable=status_var, font=("Segoe UI", 8),
             fg="#555", anchor="w", relief="sunken", padx=8, pady=3
             ).grid(row=3, column=0, sticky="ew", pady=(6, 0))
    tk.Label(root,
             text="Prices are BookABin best price (cheapest available supplier for your area).",
             font=("Segoe UI", 8), fg="#777"
             ).grid(row=4, column=0, sticky="w", padx=12, pady=(2, 10))

    def reset_table():
        for wtype, iid in row_iids.items():
            sizes = WASTE_TYPES[wtype]
            vals  = [wtype] + [
                "..." if s in sizes else "-"
                for s in ALL_SIZES
            ]
            tree.item(iid, values=vals)

    def on_search():
        postcode = postcode_var.get().strip()
        dod      = delivery_var.get().strip()
        pud      = pickup_var.get().strip()
        if not postcode or not dod or not pud:
            status_var.set("Please fill in all fields.")
            return

        btn.config(state="disabled")
        reset_table()
        status_var.set("Starting 5 browser sessions (headless Chrome)...")

        cell_q   = queue.Queue()
        status_q = queue.Queue()
        done_ev  = threading.Event()

        threading.Thread(
            target=run_search,
            args=(postcode, dod, pud, cell_q, status_q, done_ev),
            daemon=True
        ).start()

        def poll():
            while not status_q.empty():
                status_var.set(status_q.get_nowait())

            while not cell_q.empty():
                waste_type, size, price = cell_q.get_nowait()
                iid  = row_iids.get(waste_type)
                if iid:
                    cols     = tree.item(iid, "values")
                    col_i    = col_ids.index(size)
                    new_vals = list(cols)
                    new_vals[col_i] = (
                        f"${price:,.0f}" if isinstance(price, float) else (price if price is not None else "N/A")
                    )
                    tree.item(iid, values=new_vals)

            if done_ev.is_set():
                status_var.set(
                    f"Done  |  Postcode {postcode}   {dod} -> {pud}"
                    f"  |  Prices are BookABin best available."
                )
                btn.config(state="normal")
            else:
                root.after(200, poll)

        root.after(200, poll)

    btn.config(command=on_search)
    root.mainloop()


if __name__ == "__main__":
    build_ui()