"""Render the Xero README images headless with Playwright, from real run output.

    python scripts/render_xero_media.py   (needs: pip install playwright && playwright install chromium)

1. media/xero-bill-readback.png  the three draft bills read back from Xero's Demo Company by id
2. media/xero-live-rerun.png     the live re-run: duplicates skipped, held invoices, token refresh

Bill ids are shortened in the images; the full output is in examples/xero/live-run/.
"""
import html
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "xero" / "live-run"
MEDIA = ROOT / "media"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def colour(line: str) -> str:
    t = html.escape(line)
    if line.startswith("$"):
        return f"<span style='color:#7ee787'>{t}</span>"
    if "DRAFT" in line or "refreshed" in line:
        return f"<span style='color:#3fb950'>{t}</span>"
    if "HELD" in line or "DUPLICATE" in line:
        return f"<span style='color:#d29922'>{t}</span>"
    if line.strip().startswith("!"):
        return f"<span style='color:#f85149'>{t}</span>"
    if line.strip().startswith("-"):
        return f"<span style='color:#8b949e'>{t}</span>"
    return t


def page_for(lines: list[str], title: str) -> str:
    body = "\n".join(colour(x) for x in lines)
    return f"""<html><body style="margin:0;background:#0d1117">
<div id="wrap" style="display:inline-block;padding:22px 28px;background:#0d1117">
<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#8b949e;font-size:13px;margin-bottom:10px">
{html.escape(title)}</div>
<pre style="margin:0;font-family:{MONO};font-size:13px;line-height:1.45;color:#c9d1d9">{body}</pre>
</div></body></html>"""


def run_lines(name: str) -> list[str]:
    # Shorten Xero bill ids (8 hex characters, then an ellipsis).
    text = (RUN / name).read_text()
    text = re.sub(r"\b([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", r"\1...", text)
    return text.splitlines()


def main():
    MEDIA.mkdir(exist_ok=True)
    pages = [
        (page_for(run_lines("3-independent-readback.txt"),
                  "Live, 5 November 2026: the three draft bills read back from Xero's Demo Company by id"),
         "xero-bill-readback.png"),
        (page_for(run_lines("2-rerun-duplicates.txt"),
                  "Live, 5 November 2026: the same five invoices pushed again to Xero's Demo Company"),
         "xero-live-rerun.png"),
    ]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=2)
        for doc, out in pages:
            page.set_content(doc)
            page.locator("#wrap").screenshot(path=str(MEDIA / out))
            print("wrote media/" + out)
        b.close()


if __name__ == "__main__":
    main()
