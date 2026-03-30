"""
download_chrome.py
------------------
Downloads the latest stable portable Chrome for Testing + matching ChromeDriver
(Windows x64) into the workspace folder before building the EXE.

Run once before pyinstaller:
    python download_chrome.py
"""

import io
import json
import os
import urllib.request
import zipfile

DEST = os.path.dirname(os.path.abspath(__file__))

VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)


def download_zip(url: str, dest_dir: str, label: str):
    print(f"  Downloading {label} ...")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    print(f"  Extracting {label} ...")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest_dir)
    print(f"  Done: {dest_dir}")


def main():
    print("Fetching Chrome for Testing version manifest ...")
    with urllib.request.urlopen(VERSIONS_URL) as resp:
        manifest = json.loads(resp.read())

    stable = manifest["channels"]["Stable"]["downloads"]

    # Find Windows x64 chrome URL
    chrome_url = next(
        d["url"] for d in stable["chrome"]
        if d["platform"] == "win64"
    )
    driver_url = next(
        d["url"] for d in stable["chromedriver"]
        if d["platform"] == "win64"
    )

    chrome_dest = os.path.join(DEST, "chrome-win64")
    driver_dest = os.path.join(DEST, "chromedriver-win64")

    if os.path.isdir(chrome_dest):
        print(f"chrome-win64 already exists, skipping download.")
    else:
        download_zip(chrome_url, DEST, "Chrome for Testing (win64)")

    if os.path.isdir(driver_dest):
        print(f"chromedriver-win64 already exists, skipping download.")
    else:
        download_zip(driver_url, DEST, "ChromeDriver (win64)")

    print("\nAll done. You can now run: pyinstaller SkipBinTool.spec")


if __name__ == "__main__":
    main()
