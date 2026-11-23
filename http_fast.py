"""
Shared HTTP helpers for the fast (browser-free) supplier scrapers.

Both BookABin and SkipBinFinder are plain server-rendered forms:
  * BookABin          – ASP.NET WebForms (POST __EVENTTARGET + __VIEWSTATE)
  * SkipBinFinder     – plain GET forms (query-string parameters)

Neither needs JavaScript, so a `requests.Session` is ~5-10x faster than
driving headless Chrome and uses a fraction of the memory.  On the
Streamlit Community Cloud free tier (1 vCPU / 1 GB RAM) that is the
difference between a ~4 minute search and a ~20 second one.

The Selenium implementations are kept as a fallback (`use_browser=True`) so a
layout change on either site degrades gracefully instead of returning N/A.
"""

import re
import time
import threading

try:
    import requests
except Exception:                                     # pragma: no cover
    requests = None

# A desktop-Chrome UA.  Both sites serve the same HTML to it, and it avoids
# the stripped-down markup some bots get.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
}

# One session per thread: requests.Session is not guaranteed thread-safe.
_local = threading.local()


def new_session(referer=None):
    """Return a requests.Session with browser-like headers."""
    s = requests.Session()
    s.headers.update(_HEADERS)
    if referer:
        s.headers["Referer"] = referer
    return s


def thread_session(referer=None):
    """Return this thread's session, creating it on first use."""
    s = getattr(_local, "session", None)
    if s is None:
        s = new_session(referer)
        _local.session = s
    return s


def html_to_text(html):
    """
    Strip scripts/styles/tags and collapse whitespace.

    Tags are replaced with a space (not a newline) so that phrases which are
    split across inline elements stay together — the same rendering a browser
    produces.  Use `html_to_lines()` when per-line structure is needed.
    """
    p = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    p = re.sub(r"<style.*?</style>", " ", p, flags=re.S | re.I)
    p = re.sub(r"<!--.*?-->", " ", p, flags=re.S)
    p = re.sub(r"<[^>]+>", " ", p)
    p = p.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    p = p.replace("&quot;", '"').replace("&pound;", "£")
    return re.sub(r"\s+", " ", p)


def html_to_lines(html):
    """Same as html_to_text() but keeps one element per line."""
    p = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    p = re.sub(r"<style.*?</style>", " ", p, flags=re.S | re.I)
    p = re.sub(r"<!--.*?-->", " ", p, flags=re.S)
    p = re.sub(r"<[^>]+>", "\n", p)
    p = p.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    p = p.replace("&quot;", '"').replace("&pound;", "£")
    lines = [ln.strip() for ln in p.split("\n")]
    return [ln for ln in lines if ln]


def form_fields(html):
    """
    Return the hidden form fields of an ASP.NET WebForms page as a dict.
    Handles both attribute orders (`name` before / after `value`).
    """
    out = {}
    for m in re.finditer(r'<input[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"', html, re.I):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input[^>]*?value="([^"]*)"[^>]*?name="([^"]+)"', html, re.I):
        out.setdefault(m.group(2), m.group(1))
    return out


def retry(fn, attempts=3, delay=1.5, what=""):
    """Call fn(), retrying on exception.  Returns (result, error_text)."""
    last = None
    for attempt in range(attempts):
        try:
            return fn(), None
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay * (attempt + 1))
    return None, f"{what or 'request'} failed after {attempts} attempts: {last!r}"
