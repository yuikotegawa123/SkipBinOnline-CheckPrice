# -*- mode: python ; coding: utf-8 -*-
import os, sys, importlib.util

# Locate selenium_stealth JS files
_stealth_spec = importlib.util.find_spec('selenium_stealth')
_stealth_pkg  = os.path.dirname(_stealth_spec.origin)
_stealth_js   = os.path.join(_stealth_pkg, 'js')

# Portable Chrome for Testing + matching ChromeDriver (downloaded by download_chrome.py)
_here        = os.path.dirname(os.path.abspath(SPEC))
_chrome_dir  = os.path.join(_here, 'chrome-win64')
_driver_dir  = os.path.join(_here, 'chromedriver-win64')

if not os.path.isdir(_chrome_dir):
    raise RuntimeError(
        "chrome-win64/ not found.\n"
        "Run:  python download_chrome.py\n"
        "then rebuild."
    )
if not os.path.isdir(_driver_dir):
    raise RuntimeError(
        "chromedriver-win64/ not found.\n"
        "Run:  python download_chrome.py\n"
        "then rebuild."
    )

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        (_stealth_js,   'selenium_stealth/js'),
        (_chrome_dir,   'chrome-win64'),
        (_driver_dir,   'chromedriver-win64'),
    ],
    hiddenimports=[
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.chrome.options',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.common.by',
        'selenium.webdriver.support.ui',
        'selenium.webdriver.support.expected_conditions',
        'selenium.webdriver.remote.webelement',
        'selenium.webdriver.remote.command',
        'selenium_stealth',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Single-file mode: everything packed into one EXE.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SkipBinTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
)
