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

# XPath for the Edit Row image-button and the Update Row image-button
_EDIT_BTN_XPATH   = ".//input[@alt='Edit Row' or @title='Edit Row' or @value='Edit']"
_UPDATE_BTN_XPATH = ".//input[@alt='Update Row' or @title='Update Row' or @value='Update']"
_CANCEL_BTN_XPATH = ".//input[@alt='Cancel' or @title='Cancel' or @value='Cancel']"


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


def _get_data_rows(driver):
    """
    Return all <tr> elements that have an Edit Row button (i.e. data rows, not header rows).
    """
    from selenium.webdriver.common.by import By
    return driver.find_elements(
        By.XPATH,
        f"//tr[.//input[@alt='Edit Row' or @title='Edit Row' or @value='Edit']]",
    )


def get_rates(supplier_id: str, password: str, from_date: str) -> dict:
    """
    Log in, navigate to rates_manage.aspx, then for each data row click the
    ``input[@alt='Edit Row']`` button, scrape the revealed text inputs (the
    first one being the first-day-column price), then click Cancel to restore
    the row before moving on.

    Returns:
        {
            "ok":      True | False,
            "message": str,
            "rows": [
                {
                    "row_index":   int,   # 0-based position in the table
                    "description": str,   # bin size / waste-type label
                    "first_field_id":  str,   # id of the first-day-column price input
                    "first_field_val": str,   # current value of that field
                    "all_fields": {field_id: value, ...}  # all editable fields
                },
                ...
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
            return {"ok": False, "message": "Login failed — check Supplier ID and password.", "rows": []}

        url = RATES_URL_TPL.format(date=from_date)
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: "rates" in d.page_source.lower() or "fromdate" in d.current_url.lower()
        )

        # Count how many data rows exist before entering edit mode
        data_rows = _get_data_rows(driver)
        if not data_rows:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:2000]
            return {"ok": True, "message": "Logged in but no editable rows found. Raw preview below.", "rows": [], "raw": body_text}

        rows = []
        num_rows = len(data_rows)

        for row_idx in range(num_rows):
            try:
                # Re-fetch rows each iteration — DOM may rebuild after edit/cancel
                data_rows = _get_data_rows(driver)
                if row_idx >= len(data_rows):
                    break
                tr = data_rows[row_idx]

                # Read the description from the first <td> (before entering edit mode)
                cells = tr.find_elements(By.TAG_NAME, "td")
                description = cells[0].text.strip() if cells else f"Row {row_idx}"

                # Click the Edit Row button
                edit_btn = tr.find_element(By.XPATH, _EDIT_BTN_XPATH)
                driver.execute_script("arguments[0].scrollIntoView(true);", edit_btn)
                driver.execute_script("arguments[0].click();", edit_btn)
                time.sleep(1)

                # Re-fetch the same row index (now in edit mode)
                data_rows_after = _get_data_rows(driver)
                # The row in edit mode no longer has the Edit button — look for Update button
                # The edited row is the one with an Update Row button
                edit_tr = None
                try:
                    edit_tr = driver.find_element(
                        By.XPATH,
                        f"//tr[.//input[@alt='Update Row' or @title='Update Row' or @value='Update']]"
                    )
                except Exception:
                    pass

                all_fields = {}
                if edit_tr is not None:
                    inputs = edit_tr.find_elements(By.XPATH, ".//input[@type='text']")
                    for inp in inputs:
                        fid = (inp.get_attribute("id") or
                               inp.get_attribute("name") or
                               f"field_{row_idx}_{len(all_fields)}")
                        val = inp.get_attribute("value") or ""
                        all_fields[fid] = val

                first_fid = next(iter(all_fields), None)
                first_val = all_fields[first_fid] if first_fid else ""

                rows.append({
                    "row_index":       row_idx,
                    "description":     description,
                    "first_field_id":  first_fid,
                    "first_field_val": first_val,
                    "all_fields":      all_fields,
                })

                # Cancel to leave the row unchanged and return to view mode
                try:
                    if edit_tr is not None:
                        cancel_btn = edit_tr.find_element(By.XPATH, _CANCEL_BTN_XPATH)
                        driver.execute_script("arguments[0].click();", cancel_btn)
                        time.sleep(1)
                except Exception:
                    pass

            except Exception:
                continue

        if not rows:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:2000]
            return {"ok": True, "message": "Logged in but no editable rows found. Raw preview below.", "rows": [], "raw": body_text}

        return {"ok": True, "message": f"Loaded {len(rows)} rate rows.", "rows": rows}

    except Exception as exc:
        return {"ok": False, "message": f"Error: {exc}", "rows": []}
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


def update_rates(supplier_id: str, password: str, from_date: str,
                 updates: dict, status_q=None) -> dict:
    """
    Log in, navigate to rates_manage.aspx.
    For each entry in ``updates`` (row_index -> new_price), click the
    ``input[@alt='Edit Row']`` button for that row, overwrite the text input
    in the column that corresponds to ``from_date`` with the new value,
    then click ``input[@alt='Update Row']`` to save that row on the website.

    updates: { row_index (int): new_price (str|float), ... }

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

        # Find the date column index so we target the correct price input
        date_col_idx = _find_date_col_index(driver, from_date)
        input_offset = max(0, date_col_idx - 1) if date_col_idx is not None else 0

        updated_count = 0
        skipped = []

        for row_idx, new_price in updates.items():
            try:
                data_rows = _get_data_rows(driver)
                if row_idx >= len(data_rows):
                    skipped.append(str(row_idx))
                    continue
                tr = data_rows[row_idx]

                cells = tr.find_elements(By.TAG_NAME, "td")
                description = cells[0].text.strip() if cells else f"Row {row_idx}"

                # Click Edit Row
                edit_btn = tr.find_element(By.XPATH, _EDIT_BTN_XPATH)
                driver.execute_script("arguments[0].scrollIntoView(true);", edit_btn)
                driver.execute_script("arguments[0].click();", edit_btn)
                time.sleep(1)
                _status(f"Editing row {row_idx}: {description} → {new_price}")

                edit_tr = driver.find_element(
                    By.XPATH,
                    "//tr[.//input[@alt='Update Row' or @title='Update Row' or @value='Update']]"
                )

                text_inputs = edit_tr.find_elements(By.XPATH, ".//input[@type='text']")
                if not text_inputs:
                    _cancel_edit(driver, edit_tr)
                    skipped.append(str(row_idx))
                    continue

                target_idx = min(input_offset, len(text_inputs) - 1)
                target_input = text_inputs[target_idx]
                driver.execute_script("arguments[0].scrollIntoView(true);", target_input)
                target_input.clear()
                target_input.send_keys(str(new_price))

                update_btn = edit_tr.find_element(By.XPATH, _UPDATE_BTN_XPATH)
                driver.execute_script("arguments[0].scrollIntoView(true);", update_btn)
                driver.execute_script("arguments[0].click();", update_btn)
                # Wait for ASP.NET postback to complete — Edit Row buttons reappear
                try:
                    WebDriverWait(driver, 10).until(
                        lambda d: len(_get_data_rows(d)) > 0
                    )
                except Exception:
                    time.sleep(2)
                updated_count += 1

            except Exception as row_exc:
                _status(f"⚠ Row {row_idx} failed: {row_exc}")
                try:
                    edit_tr_fallback = driver.find_element(
                        By.XPATH,
                        "//tr[.//input[@alt='Update Row' or @title='Update Row' or @value='Update']]"
                    )
                    _cancel_edit(driver, edit_tr_fallback)
                except Exception:
                    pass
                skipped.append(str(row_idx))
                continue

        shot = driver.get_screenshot_as_png()
        if updated_count == 0:
            return {"ok": False, "message": "No rows were updated — check row indices or Edit Row button structure.", "screenshot": shot}

        msg = f"✅ {updated_count} row(s) updated on bookabin.com.au."
        if skipped:
            msg += f"  ⚠ Skipped row(s): {', '.join(skipped)}."
        return {"ok": True, "message": msg, "screenshot": shot}

    except Exception as exc:
        shot = None
        try: shot = driver.get_screenshot_as_png()
        except Exception: pass
        return {"ok": False, "message": f"Error: {exc}", "screenshot": shot}
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


def _normalise_date_variants(date_str: str) -> list:
    """
    Given a D/MM/YYYY date string, return a list of alternative representations
    that might appear as column headers on rates_manage.aspx, e.g.:
        "2/04/2026"  →  ["2/04/2026", "02/04/2026", "2/4/2026",
                         "02-04-2026", "2-Apr-2026", "02-Apr-2026",
                         "Apr 2", "Apr 02", "2 Apr 2026", "02 Apr 2026"]
    """
    import calendar
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        return [date_str]
    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    mon_abbr = calendar.month_abbr[month]   # "Apr"
    variants = [
        f"{day}/{month:02d}/{year}",
        f"{day:02d}/{month:02d}/{year}",
        f"{day}/{month}/{year}",
        f"{day:02d}-{month:02d}-{year}",
        f"{day}-{mon_abbr}-{year}",
        f"{day:02d}-{mon_abbr}-{year}",
        f"{mon_abbr} {day}",
        f"{mon_abbr} {day:02d}",
        f"{day} {mon_abbr} {year}",
        f"{day:02d} {mon_abbr} {year}",
        f"{day:02d}/{month:02d}",
        f"{day}/{month:02d}",
    ]
    return variants


def _find_date_col_index(driver, from_date: str):
    """
    Scan the table header row(s) for a cell whose text contains the from_date.
    Returns the 0-based <th>/<td> column index of that cell, or None if not found.
    The first column (description) is index 0; price columns start at index 1.
    """
    from selenium.webdriver.common.by import By
    variants = [v.lower() for v in _normalise_date_variants(from_date)]
    header_rows = driver.find_elements(By.XPATH, "//tr[.//th]")
    for hr in header_rows:
        cells = hr.find_elements(By.XPATH, ".//th | .//td")
        for col_idx, cell in enumerate(cells):
            cell_text = cell.text.strip().lower()
            if any(v in cell_text for v in variants):
                return col_idx
    return None


def _match_size(description: str, price_map: dict):
    """
    Try to match a row description string against the keys in price_map.
    Keys are size strings like "3", "6", "7.5".
    Returns the matched price value or None.
    """
    desc_lower = description.lower()
    # Sort keys longest-first so "7.5" is tried before "7"
    for key in sorted(price_map.keys(), key=lambda k: len(str(k)), reverse=True):
        key_str = str(key).strip()
        # Patterns: "3m", "3 m", "3cubic", "3 cubic", "3.0", standalone digit boundary
        size_patterns = [
            f"{key_str}m",
            f"{key_str} m",
            f"{key_str}cubic",
            f"{key_str} cubic",
            f"{key_str}.0 ",
            f"{key_str}.0m",
        ]
        if any(p in desc_lower for p in size_patterns):
            return price_map[key]
        # Word-boundary fallback: key surrounded by non-digit chars
        import re as _re
        if _re.search(rf'(?<!\d){_re.escape(key_str)}(?!\d)', desc_lower):
            return price_map[key]
    return None


def auto_update_rates(supplier_id: str, password: str, from_date: str,
                      price_map: dict, status_q=None) -> dict:
    """
    Log into bookabin.com.au, open rates_manage.aspx?fromdate=<from_date>,
    and for every data row whose description matches a size key in ``price_map``:

      1. Find the column header that matches ``from_date``  →  ``date_col_idx``
      2. Click ``input[@alt='Edit Row']`` on that row
      3. In the revealed edit row, pick the text input at position
         ``date_col_idx - 1`` (the first column is the description, not an input)
      4. Set its value to ``price_map[size]``  (caller already subtracted 1)
      5. Click ``input[@alt='Update Row']`` to save

    price_map: { "3" -> 299, "6" -> 349, ... }

    Returns:
        {"ok": True|False, "message": str, "screenshot": bytes|None,
         "matched": {row_idx: {"description": str, "new_price": value}}}
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
            return {"ok": False, "message": "Login failed — check Supplier ID and password.",
                    "screenshot": shot, "matched": {}}

        url = RATES_URL_TPL.format(date=from_date)
        _status(f"Loading rates page for {from_date}…")
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: "rates" in d.page_source.lower() or "fromdate" in d.current_url.lower()
        )

        # ── Find which column index corresponds to from_date ──────────────
        date_col_idx = _find_date_col_index(driver, from_date)
        if date_col_idx is None:
            # from_date column not found in header → fall back to first price input (index 0)
            _status("⚠ Date column header not found — will use first text input in each edit row.")
            input_offset = 0
        else:
            # Column 0 is description (no text input), price inputs start at 0 inside edit row
            # So the Nth column header (1-based price) → input index (date_col_idx - 1)
            input_offset = max(0, date_col_idx - 1)
            _status(f"Date column index = {date_col_idx} → edit-row input offset = {input_offset}")

        # ── Scan all data rows for matching sizes ─────────────────────────
        data_rows = _get_data_rows(driver)
        if not data_rows:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:2000]
            return {"ok": False,
                    "message": "Logged in but no editable rows found.",
                    "screenshot": driver.get_screenshot_as_png(),
                    "matched": {},
                    "raw": body_text}

        row_updates  = {}   # row_idx (int) -> new_price
        matched_info = {}   # row_idx -> {description, new_price}
        num_rows = len(data_rows)

        for row_idx in range(num_rows):
            data_rows_cur = _get_data_rows(driver)
            if row_idx >= len(data_rows_cur):
                break
            tr = data_rows_cur[row_idx]
            # Read all cell text joined for a richer description
            cells = tr.find_elements(By.TAG_NAME, "td")
            description = " ".join(c.text.strip() for c in cells if c.text.strip())
            if not description:
                description = f"Row {row_idx}"

            matched_price = _match_size(description, price_map)
            if matched_price is not None:
                row_updates[row_idx] = matched_price
                matched_info[row_idx] = {"description": description, "new_price": matched_price}

        if not row_updates:
            shot = driver.get_screenshot_as_png()
            return {"ok": False,
                    "message": "No rows matched the price map — check size labels on the rates page.",
                    "screenshot": shot,
                    "matched": matched_info}

        _status(f"Matched {len(row_updates)} rows — updating prices on bookabin.com.au…")

        # ── Edit → fill correct date column → Update Row ──────────────────
        updated_count = 0
        skipped = []

        for row_idx, new_price in row_updates.items():
            desc = matched_info[row_idx]["description"]
            try:
                data_rows_cur = _get_data_rows(driver)
                if row_idx >= len(data_rows_cur):
                    skipped.append(str(row_idx))
                    continue
                tr = data_rows_cur[row_idx]

                # Click Edit Row
                edit_btn = tr.find_element(By.XPATH, _EDIT_BTN_XPATH)
                driver.execute_script("arguments[0].scrollIntoView(true);", edit_btn)
                driver.execute_script("arguments[0].click();", edit_btn)
                time.sleep(1)
                _status(f"  Row {row_idx} [{desc}] → {new_price}")

                # Row is now in edit mode — find it by Update Row button
                edit_tr = driver.find_element(
                    By.XPATH,
                    "//tr[.//input[@alt='Update Row' or @title='Update Row' or @value='Update']]"
                )

                # All text inputs inside the edit row — pick the one at input_offset
                text_inputs = edit_tr.find_elements(By.XPATH, ".//input[@type='text']")
                if not text_inputs:
                    _cancel_edit(driver, edit_tr)
                    skipped.append(str(row_idx))
                    continue

                # Clamp offset to available inputs
                target_idx = min(input_offset, len(text_inputs) - 1)
                target_input = text_inputs[target_idx]
                driver.execute_script("arguments[0].scrollIntoView(true);", target_input)
                target_input.clear()
                target_input.send_keys(str(new_price))

                # Click Update Row to save this row on the website
                update_btn = edit_tr.find_element(By.XPATH, _UPDATE_BTN_XPATH)
                driver.execute_script("arguments[0].scrollIntoView(true);", update_btn)
                driver.execute_script("arguments[0].click();", update_btn)
                # Wait for ASP.NET postback to complete — Edit Row buttons reappear
                try:
                    WebDriverWait(driver, 10).until(
                        lambda d: len(_get_data_rows(d)) > 0
                    )
                except Exception:
                    time.sleep(2)
                updated_count += 1

            except Exception as row_exc:
                _status(f"  ⚠ Row {row_idx} failed: {row_exc}")
                try:
                    edit_tr_fb = driver.find_element(
                        By.XPATH,
                        "//tr[.//input[@alt='Update Row' or @title='Update Row' or @value='Update']]"
                    )
                    _cancel_edit(driver, edit_tr_fb)
                except Exception:
                    pass
                skipped.append(str(row_idx))
                continue

        shot = driver.get_screenshot_as_png()
        if updated_count == 0:
            return {"ok": False,
                    "message": "Matched rows but no updates succeeded — Edit/Update buttons may differ.",
                    "screenshot": shot, "matched": matched_info}

        msg = f"✅ {updated_count} row(s) updated on bookabin.com.au."
        if skipped:
            msg += f"  ⚠ Skipped row(s): {', '.join(skipped)}."
        return {"ok": True, "message": msg, "screenshot": shot, "matched": matched_info}

    except Exception as exc:
        shot = None
        try: shot = driver.get_screenshot_as_png()
        except Exception: pass
        return {"ok": False, "message": f"Error: {exc}", "screenshot": shot, "matched": {}}
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass


def _cancel_edit(driver, edit_tr):
    """Click the Cancel button inside an open edit row, ignoring errors."""
    try:
        from selenium.webdriver.common.by import By
        cancel_btn = edit_tr.find_element(By.XPATH, _CANCEL_BTN_XPATH)
        driver.execute_script("arguments[0].click();", cancel_btn)
        import time; time.sleep(0.5)
    except Exception:
        pass


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