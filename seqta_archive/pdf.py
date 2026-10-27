"""
HTML -> PDF.

1. WeasyPrint (Linux / Docker: best output, page footers).
2. A Chromium-based browser in headless mode (Windows: Edge is always installed).
3. None - callers fall back to an HTML report.
"""

import os
import shutil
import subprocess
import tempfile

from . import config

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

_engine = None


def _weasy():
    try:
        from weasyprint import HTML  # noqa: F401
        return HTML
    except Exception:
        return None


def _browser():
    if config.PDF_BROWSER and os.path.exists(config.PDF_BROWSER):
        return config.PDF_BROWSER
    for name in ("msedge", "chromium", "chromium-browser", "google-chrome", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for p in _BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def engine():
    """'weasyprint', 'browser:<path>' or '' - resolved once."""
    global _engine
    if _engine is None:
        if _weasy():
            _engine = "weasyprint"
        else:
            b = _browser()
            _engine = f"browser:{b}" if b else ""
    return _engine


def render(html):
    """Return PDF bytes, or None if no PDF engine is available or rendering failed."""
    eng = engine()
    if eng == "weasyprint":
        return _weasy()(string=html, base_url=None).write_pdf()
    if eng.startswith("browser:"):
        return _render_browser(eng.split(":", 1)[1], html)
    return None


def _render_browser(exe, html):
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "report.html")
        out = os.path.join(d, "report.pdf")
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        url = "file:///" + src.replace("\\", "/").lstrip("/")
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
               "--no-default-browser-check", "--disable-extensions", "--disable-sync",
               "--disable-background-networking", "--disable-component-update",
               "--disable-default-apps", "--no-pdf-header-footer",
               f"--user-data-dir={os.path.join(d, 'profile')}",
               f"--print-to-pdf={out}", url]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"PDF via browser failed: {e}", flush=True)
            return None
        if os.path.exists(out) and os.path.getsize(out) > 0:
            with open(out, "rb") as f:
                return f.read()
        print("PDF via browser produced no output", flush=True)
        return None
