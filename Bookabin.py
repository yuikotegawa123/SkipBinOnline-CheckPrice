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