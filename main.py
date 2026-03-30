"""
Main.py  –  Unified SkipBin Price Checker
Shows BookABin (Selenium) and BestPriceSkipBins (requests) side-by-side.
One postcode + date search fetches both sources simultaneously.
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk

# When running as a frozen EXE, requests cannot find certifi's CA bundle
# automatically. Point it to the bundled copy so HTTPS works correctly.
if getattr(sys, 'frozen', False):
    import certifi
    _ca = certifi.where()
    os.environ.setdefault('REQUESTS_CA_BUNDLE', _ca)
    os.environ.setdefault('SSL_CERT_FILE', _ca)

import Bookabin
import Bestpriceskipbins as BPSB
import Skipbinfinder as SBF
import Skipbinsonline as SBO

# ---------------------------------------------------------------------------
# Shared settings
# ---------------------------------------------------------------------------

FONT       = ("Segoe UI", 10)
FONT_BOLD  = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 8)

BAB_COLOR  = "#1a73e8"   # BookABin blue
BPSB_COLOR = "#e65100"   # BestPrice orange
SBF_COLOR  = "#2e7d32"   # SkipBinFinder green
SBO_COLOR  = "#6a1b9a"   # SkipBinsOnline purple

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_price(price):
    if isinstance(price, (int, float)):
        return f"${price:,.0f}"
    return price if price is not None else "N/A"


# ---------------------------------------------------------------------------
# UI builder
# ---------------------------------------------------------------------------

def build_ui():
    root = tk.Tk()
    root.title("SkipBin Price Checker  –  BookABin  ·  BestPriceSkipBins  ·  SkipBinFinder  ·  SkipBinsOnline")
    root.resizable(True, True)

    # ── Header ──────────────────────────────────────────────────────────────
    hdr = tk.Frame(root, bg="#1a3a5c")
    hdr.grid(row=0, column=0, sticky="ew")
    root.grid_columnconfigure(0, weight=1)

    tk.Label(hdr, text="SkipBin Price Checker",
             font=("Segoe UI", 15, "bold"), fg="white", bg="#1a3a5c",
             pady=10, padx=16).pack(side="left")
    tk.Label(hdr,
             text="BookABin  ·  BestPriceSkipBins  ·  SkipBinFinder  ·  SkipBinsOnline  –  one search, four sources",
             font=("Segoe UI", 9), fg="#aac8f0", bg="#1a3a5c").pack(side="left")

    # ── Input row ────────────────────────────────────────────────────────────
    inp = tk.Frame(root, pady=8, padx=12)
    inp.grid(row=1, column=0, sticky="w")

    tk.Label(inp, text="Postcode:", font=FONT_BOLD).grid(
        row=0, column=0, sticky="e", padx=(0, 4))
    postcode_var = tk.StringVar(value="3173")
    tk.Entry(inp, textvariable=postcode_var, font=FONT, width=10).grid(
        row=0, column=1, padx=(0, 18))

    tk.Label(inp, text="Delivery Date:", font=FONT_BOLD).grid(
        row=0, column=2, sticky="e", padx=(0, 4))
    delivery_var = tk.StringVar(value="1/04/2026")
    tk.Entry(inp, textvariable=delivery_var, font=FONT, width=13).grid(
        row=0, column=3, padx=(0, 18))

    tk.Label(inp, text="Pickup Date:", font=FONT_BOLD).grid(
        row=0, column=4, sticky="e", padx=(0, 4))
    pickup_var = tk.StringVar(value="8/04/2026")
    tk.Entry(inp, textvariable=pickup_var, font=FONT, width=13).grid(
        row=0, column=5, padx=(0, 18))

    tk.Label(inp, text="(D/MM/YYYY)", font=FONT_SMALL, fg="#888").grid(
        row=0, column=6, padx=(0, 18))

    btn = tk.Button(
        inp, text="  Search All  ", font=FONT_BOLD,
        bg="#1a3a5c", fg="white", relief="flat", cursor="hand2", pady=4
    )
    btn.grid(row=0, column=7)

    tk.Label(inp, text="(D/MM/YYYY)", font=FONT_SMALL, fg="#888").grid(
        row=0, column=6, padx=(0, 12))

    # ── Notebook (two tabs) ─────────────────────────────────────────────────
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Treeview",         font=("Consolas", 10), rowheight=26)
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                    background="#2c5f9e", foreground="white")
    style.map("Treeview.Heading", background=[("active", "#1a4a7a")])

    nb = ttk.Notebook(root)
    nb.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
    root.grid_rowconfigure(2, weight=1)

    # ── Tab 1: BookABin ──────────────────────────────────────────────────────
    tab_bab = tk.Frame(nb)
    nb.add(tab_bab, text="  BookABin  ")

    bab_col_ids = ["waste_type"] + Bookabin.ALL_SIZES

    bab_frame = tk.Frame(tab_bab)
    bab_frame.pack(fill="both", expand=True)

    bab_tree = ttk.Treeview(bab_frame, columns=bab_col_ids, show="headings", height=8)
    bab_tree.column("waste_type", width=200, anchor="w", stretch=False)
    for s in Bookabin.ALL_SIZES:
        bab_tree.column(s, width=78, anchor="center", stretch=False)
    bab_tree.heading("waste_type", text="Waste Type")
    for s in Bookabin.ALL_SIZES:
        bab_tree.heading(s, text=f"{s} m³")

    bab_hsb = ttk.Scrollbar(bab_frame, orient="horizontal", command=bab_tree.xview)
    bab_vsb = ttk.Scrollbar(bab_frame, orient="vertical",   command=bab_tree.yview)
    bab_tree.configure(xscrollcommand=bab_hsb.set, yscrollcommand=bab_vsb.set)
    bab_tree.grid(row=0, column=0, sticky="nsew")
    bab_vsb.grid(row=0, column=1, sticky="ns")
    bab_hsb.grid(row=1, column=0, sticky="ew")
    bab_frame.grid_rowconfigure(0, weight=1)
    bab_frame.grid_columnconfigure(0, weight=1)

    bab_tree.tag_configure("even", background="#eef4ff")
    bab_tree.tag_configure("odd",  background="#ffffff")

    tk.Label(tab_bab,
             text="Prices scraped from bookabin.com.au via headless Chrome (Selenium). "
                  "Reflects cheapest available supplier.",
             font=FONT_SMALL, fg="#555"
             ).pack(anchor="w", padx=8, pady=(2, 4))

    bab_row_iids = {}
    for idx, wtype in enumerate(Bookabin.WASTE_TYPES):
        sizes = Bookabin.WASTE_TYPES[wtype]
        vals  = [wtype] + [
            "..." if s in sizes else "-"
            for s in Bookabin.ALL_SIZES
        ]
        iid = bab_tree.insert("", "end", values=vals,
                              tags=("even" if idx % 2 == 0 else "odd",))
        bab_row_iids[wtype] = iid

    # ── Tab 2: BestPriceSkipBins ─────────────────────────────────────────────
    tab_bpsb = tk.Frame(nb)
    nb.add(tab_bpsb, text="  BestPriceSkipBins  ")

    bpsb_col_ids = ["waste_type"] + BPSB.ALL_SIZES

    bpsb_frame = tk.Frame(tab_bpsb)
    bpsb_frame.pack(fill="both", expand=True)

    bpsb_tree = ttk.Treeview(bpsb_frame, columns=bpsb_col_ids, show="headings", height=8)
    bpsb_style = ttk.Style()
    bpsb_style.configure(
        "BPSB.Treeview.Heading",
        font=("Segoe UI", 9, "bold"),
        background="#7b3200", foreground="white"
    )
    bpsb_tree.configure(style="BPSB.Treeview")
    bpsb_style.configure("BPSB.Treeview", font=("Consolas", 10), rowheight=26)
    bpsb_style.map("BPSB.Treeview.Heading", background=[("active", "#5a2400")])

    bpsb_tree.column("waste_type", width=230, anchor="w", stretch=False)
    for s in BPSB.ALL_SIZES:
        bpsb_tree.column(s, width=78, anchor="center", stretch=False)
    bpsb_tree.heading("waste_type", text="Waste Type")
    for s in BPSB.ALL_SIZES:
        bpsb_tree.heading(s, text=f"{s} m³")

    bpsb_hsb = ttk.Scrollbar(bpsb_frame, orient="horizontal", command=bpsb_tree.xview)
    bpsb_vsb = ttk.Scrollbar(bpsb_frame, orient="vertical",   command=bpsb_tree.yview)
    bpsb_tree.configure(xscrollcommand=bpsb_hsb.set, yscrollcommand=bpsb_vsb.set)
    bpsb_tree.grid(row=0, column=0, sticky="nsew")
    bpsb_vsb.grid(row=0, column=1, sticky="ns")
    bpsb_hsb.grid(row=1, column=0, sticky="ew")
    bpsb_frame.grid_rowconfigure(0, weight=1)
    bpsb_frame.grid_columnconfigure(0, weight=1)

    bpsb_tree.tag_configure("even", background="#fff3e0")
    bpsb_tree.tag_configure("odd",  background="#ffffff")

    tk.Label(tab_bpsb,
             text="Prices scraped from bestpriceskipbins.com.au via HTTP requests. "
                  "Reflects cheapest available supplier.",
             font=FONT_SMALL, fg="#555"
             ).pack(anchor="w", padx=8, pady=(2, 4))

    bpsb_row_iids = {}
    for idx, wtype in enumerate(BPSB.WASTE_TYPES):
        sizes = BPSB.WASTE_TYPES[wtype]
        vals  = [wtype] + [
            "..." if s in sizes else "-"
            for s in BPSB.ALL_SIZES
        ]
        iid = bpsb_tree.insert("", "end", values=vals,
                               tags=("even" if idx % 2 == 0 else "odd",))
        bpsb_row_iids[wtype] = iid

    # ── Tab 3: SkipBinFinder ─────────────────────────────────────────────────
    tab_sbf = tk.Frame(nb)
    nb.add(tab_sbf, text="  SkipBinFinder  ")

    sbf_col_ids = ["waste_type"] + SBF.ALL_SIZES

    sbf_frame = tk.Frame(tab_sbf)
    sbf_frame.pack(fill="both", expand=True)

    sbf_tree = ttk.Treeview(sbf_frame, columns=sbf_col_ids, show="headings", height=8)
    sbf_style = ttk.Style()
    sbf_style.configure(
        "SBF.Treeview.Heading",
        font=("Segoe UI", 9, "bold"),
        background="#1b5e20", foreground="white"
    )
    sbf_tree.configure(style="SBF.Treeview")
    sbf_style.configure("SBF.Treeview", font=("Consolas", 10), rowheight=26)
    sbf_style.map("SBF.Treeview.Heading", background=[("active", "#145214")])

    sbf_tree.column("waste_type", width=200, anchor="w", stretch=False)
    for s in SBF.ALL_SIZES:
        sbf_tree.column(s, width=72, anchor="center", stretch=False)
    sbf_tree.heading("waste_type", text="Waste Type")
    for s in SBF.ALL_SIZES:
        sbf_tree.heading(s, text=f"{s} m³")

    sbf_hsb = ttk.Scrollbar(sbf_frame, orient="horizontal", command=sbf_tree.xview)
    sbf_vsb = ttk.Scrollbar(sbf_frame, orient="vertical",   command=sbf_tree.yview)
    sbf_tree.configure(xscrollcommand=sbf_hsb.set, yscrollcommand=sbf_vsb.set)
    sbf_tree.grid(row=0, column=0, sticky="nsew")
    sbf_vsb.grid(row=0, column=1, sticky="ns")
    sbf_hsb.grid(row=1, column=0, sticky="ew")
    sbf_frame.grid_rowconfigure(0, weight=1)
    sbf_frame.grid_columnconfigure(0, weight=1)

    sbf_tree.tag_configure("even", background="#e8f5e9")
    sbf_tree.tag_configure("odd",  background="#ffffff")

    tk.Label(tab_sbf,
             text="Prices scraped from skipbinfinder.com.au via headless Chrome (Selenium). "
                  "Shows cheapest available bin per size.",
             font=FONT_SMALL, fg="#555"
             ).pack(anchor="w", padx=8, pady=(2, 4))

    sbf_row_iids = {}
    for idx, wtype in enumerate(SBF.WASTE_TYPES):
        sizes = SBF.WASTE_TYPES[wtype]
        vals  = [wtype] + [
            "..." if s in sizes else "-"
            for s in SBF.ALL_SIZES
        ]
        iid = sbf_tree.insert("", "end", values=vals,
                              tags=("even" if idx % 2 == 0 else "odd",))
        sbf_row_iids[wtype] = iid

    # ── Tab 4: SkipBinsOnline ────────────────────────────────────────────────
    tab_sbo = tk.Frame(nb)
    nb.add(tab_sbo, text="  SkipBinsOnline  ")

    # SBO sizes are discovered dynamically; start with ALL_SIZES as column headers
    sbo_col_ids = ["waste_type"] + SBO.ALL_SIZES

    sbo_frame = tk.Frame(tab_sbo)
    sbo_frame.pack(fill="both", expand=True)

    sbo_tree = ttk.Treeview(sbo_frame, columns=sbo_col_ids, show="headings", height=12)
    sbo_style = ttk.Style()
    sbo_style.configure(
        "SBO.Treeview.Heading",
        font=("Segoe UI", 9, "bold"),
        background="#4a148c", foreground="white"
    )
    sbo_tree.configure(style="SBO.Treeview")
    sbo_style.configure("SBO.Treeview", font=("Consolas", 10), rowheight=26)
    sbo_style.map("SBO.Treeview.Heading", background=[("active", "#38006b")])

    sbo_tree.column("waste_type", width=200, anchor="w", stretch=False)
    for s in SBO.ALL_SIZES:
        sbo_tree.column(s, width=70, anchor="center", stretch=False)
    sbo_tree.heading("waste_type", text="Waste Type")
    for s in SBO.ALL_SIZES:
        sbo_tree.heading(s, text=f"{s} m³")

    sbo_hsb = ttk.Scrollbar(sbo_frame, orient="horizontal", command=sbo_tree.xview)
    sbo_vsb = ttk.Scrollbar(sbo_frame, orient="vertical",   command=sbo_tree.yview)
    sbo_tree.configure(xscrollcommand=sbo_hsb.set, yscrollcommand=sbo_vsb.set)
    sbo_tree.grid(row=0, column=0, sticky="nsew")
    sbo_vsb.grid(row=0, column=1, sticky="ns")
    sbo_hsb.grid(row=1, column=0, sticky="ew")
    sbo_frame.grid_rowconfigure(0, weight=1)
    sbo_frame.grid_columnconfigure(0, weight=1)

    sbo_tree.tag_configure("even", background="#f3e5f5")
    sbo_tree.tag_configure("odd",  background="#ffffff")

    tk.Label(tab_sbo,
             text="Prices scraped from skipbinsonline.com.au via HTTP requests. "
                  "Bin sizes are fetched live; rows are added as data arrives.",
             font=FONT_SMALL, fg="#555"
             ).pack(anchor="w", padx=8, pady=(2, 4))

    # SBO rows are added dynamically on first search (sizes are per-postcode)
    sbo_row_iids: dict = {}

    # ── Status bar ───────────────────────────────────────────────────────────
    status_var = tk.StringVar(value="Enter postcode and dates, then click  Search All.")
    tk.Label(root, textvariable=status_var, font=FONT_SMALL,
             fg="#444", anchor="w", relief="sunken", padx=8, pady=3
             ).grid(row=3, column=0, sticky="ew")

    # Sub-status (per source)
    sub_bab_var  = tk.StringVar(value="BookABin: idle")
    sub_bpsb_var = tk.StringVar(value="BestPriceSkipBins: idle")
    sub_sbf_var  = tk.StringVar(value="SkipBinFinder: idle")
    sub_sbo_var  = tk.StringVar(value="SkipBinsOnline: idle")

    sub_fr = tk.Frame(root)
    sub_fr.grid(row=4, column=0, sticky="ew", padx=8, pady=(2, 6))
    tk.Label(sub_fr, textvariable=sub_bab_var,  font=FONT_SMALL,
             fg=BAB_COLOR,  width=40, anchor="w").pack(side="left")
    tk.Label(sub_fr, textvariable=sub_bpsb_var, font=FONT_SMALL,
             fg=BPSB_COLOR, width=40, anchor="w").pack(side="left")
    tk.Label(sub_fr, textvariable=sub_sbf_var,  font=FONT_SMALL,
             fg=SBF_COLOR,  width=35, anchor="w").pack(side="left")
    tk.Label(sub_fr, textvariable=sub_sbo_var,  font=FONT_SMALL,
             fg=SBO_COLOR,  anchor="w").pack(side="left")

    # ── Logic ────────────────────────────────────────────────────────────────

    def reset_table_bab():
        for wtype, iid in bab_row_iids.items():
            sizes = Bookabin.WASTE_TYPES[wtype]
            vals  = [wtype] + [
                "..." if s in sizes else "-"
                for s in Bookabin.ALL_SIZES
            ]
            bab_tree.item(iid, values=vals)

    def reset_table_bpsb():
        for wtype, iid in bpsb_row_iids.items():
            sizes = BPSB.WASTE_TYPES[wtype]
            vals  = [wtype] + [
                "..." if s in sizes else "-"
                for s in BPSB.ALL_SIZES
            ]
            bpsb_tree.item(iid, values=vals)

    def reset_table_sbf():
        for wtype, iid in sbf_row_iids.items():
            sizes = SBF.WASTE_TYPES[wtype]
            vals  = [wtype] + [
                "..." if s in sizes else "-"
                for s in SBF.ALL_SIZES
            ]
            sbf_tree.item(iid, values=vals)

    def reset_table_sbo():
        for wtype, iid in sbo_row_iids.items():
            sizes = SBO.WASTE_TYPES.get(wtype, [])
            vals  = [wtype] + [
                "..." if s in sizes else "-"
                for s in SBO.ALL_SIZES
            ]
            sbo_tree.item(iid, values=vals)

    def parse_bpsb_date(d_slash):
        """Convert D/MM/YYYY -> dd-MM-yyyy for BPSB."""
        parts = d_slash.strip().split("/")
        if len(parts) != 3:
            return d_slash
        day, month, year = parts
        return f"{int(day):02d}-{int(month):02d}-{year}"

    def on_search():
        postcode = postcode_var.get().strip()
        dod      = delivery_var.get().strip()
        pud      = pickup_var.get().strip()
        if not postcode or not dod or not pud:
            status_var.set("Please fill in all fields.")
            return

        btn.config(state="disabled")
        reset_table_bab()
        reset_table_bpsb()
        reset_table_sbf()
        reset_table_sbo()
        status_var.set(f"Searching  {postcode}  {dod} → {pud}  on all four sites …")
        sub_bab_var.set("BookABin: starting 5 headless Chrome sessions …")
        sub_bpsb_var.set("BestPriceSkipBins: starting requests …")
        sub_sbf_var.set("SkipBinFinder: starting 5 headless Chrome sessions …")
        sub_sbo_var.set("SkipBinsOnline: starting requests …")

        # Queues / events
        bab_cell_q   = queue.Queue()
        bab_status_q = queue.Queue()
        bab_done     = threading.Event()

        bpsb_cell_q   = queue.Queue()
        bpsb_status_q = queue.Queue()
        bpsb_done     = threading.Event()

        sbf_cell_q   = queue.Queue()
        sbf_status_q = queue.Queue()
        sbf_done     = threading.Event()

        sbo_cell_q   = queue.Queue()
        sbo_status_q = queue.Queue()
        sbo_done     = threading.Event()

        # Convert date for BPSB
        bpsb_dod = parse_bpsb_date(dod)
        bpsb_pud = parse_bpsb_date(pud)

        # Launch both searches in background threads
        threading.Thread(
            target=Bookabin.run_search,
            args=(postcode, dod, pud, bab_cell_q, bab_status_q, bab_done),
            daemon=True
        ).start()

        threading.Thread(
            target=BPSB.run_search,
            args=(postcode, bpsb_dod, bpsb_pud, bpsb_cell_q, bpsb_status_q, bpsb_done),
            daemon=True
        ).start()

        threading.Thread(
            target=SBF.run_search,
            args=(postcode, dod, pud, sbf_cell_q, sbf_status_q, sbf_done),
            daemon=True
        ).start()

        threading.Thread(
            target=SBO.run_search,
            args=(postcode, dod, pud, sbo_cell_q, sbo_status_q, sbo_done),
            daemon=True
        ).start()

        def poll():
            # --- BookABin updates ---
            while not bab_status_q.empty():
                sub_bab_var.set("BookABin: " + bab_status_q.get_nowait())
            while not bab_cell_q.empty():
                wtype, size, price = bab_cell_q.get_nowait()
                iid = bab_row_iids.get(wtype)
                if iid:
                    cols     = list(bab_tree.item(iid, "values"))
                    col_i    = bab_col_ids.index(size)
                    cols[col_i] = fmt_price(price)
                    bab_tree.item(iid, values=cols)

            # --- BestPriceSkipBins updates ---
            while not bpsb_status_q.empty():
                sub_bpsb_var.set("BestPriceSkipBins: " + bpsb_status_q.get_nowait())
            while not bpsb_cell_q.empty():
                wtype, size, price = bpsb_cell_q.get_nowait()
                iid = bpsb_row_iids.get(wtype)
                if iid:
                    cols  = list(bpsb_tree.item(iid, "values"))
                    col_i = bpsb_col_ids.index(size)
                    cols[col_i] = fmt_price(price)
                    bpsb_tree.item(iid, values=cols)

            # --- SkipBinFinder updates ---
            while not sbf_status_q.empty():
                sub_sbf_var.set("SkipBinFinder: " + sbf_status_q.get_nowait())
            while not sbf_cell_q.empty():
                wtype, size, price = sbf_cell_q.get_nowait()
                iid = sbf_row_iids.get(wtype)
                if iid:
                    cols  = list(sbf_tree.item(iid, "values"))
                    if size in sbf_col_ids:
                        col_i = sbf_col_ids.index(size)
                        cols[col_i] = fmt_price(price)
                        sbf_tree.item(iid, values=cols)

            # --- SkipBinsOnline updates ---
            while not sbo_status_q.empty():
                sub_sbo_var.set("SkipBinsOnline: " + sbo_status_q.get_nowait())
            while not sbo_cell_q.empty():
                wtype, size, price = sbo_cell_q.get_nowait()
                if wtype not in sbo_row_iids:
                    idx = len(sbo_row_iids)
                    vals = [wtype] + ["-" for _ in SBO.ALL_SIZES]
                    iid = sbo_tree.insert("", "end", values=vals,
                                          tags=("even" if idx % 2 == 0 else "odd",))
                    sbo_row_iids[wtype] = iid
                iid = sbo_row_iids.get(wtype)
                if iid and size in sbo_col_ids:
                    cols  = list(sbo_tree.item(iid, "values"))
                    col_i = sbo_col_ids.index(size)
                    cols[col_i] = fmt_price(price)
                    sbo_tree.item(iid, values=cols)

            all_done = bab_done.is_set() and bpsb_done.is_set() and sbf_done.is_set() and sbo_done.is_set()

            if bab_done.is_set():
                sub_bab_var.set("BookABin: done ✓")
            if bpsb_done.is_set():
                sub_bpsb_var.set("BestPriceSkipBins: done ✓")
            if sbf_done.is_set():
                sub_sbf_var.set("SkipBinFinder: done ✓")
            if sbo_done.is_set():
                sub_sbo_var.set("SkipBinsOnline: done ✓")

            if all_done:
                status_var.set(
                    f"Done  |  Postcode {postcode}   {dod} → {pud}"
                    f"  |  All four sources complete."
                )
                btn.config(state="normal")
            else:
                root.after(400, poll)

        root.after(400, poll)

    btn.config(command=on_search)
    root.mainloop()


if __name__ == "__main__":
    build_ui()
