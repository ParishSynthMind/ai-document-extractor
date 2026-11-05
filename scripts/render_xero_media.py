"""Render the Xero README images headless with Playwright, from real run output.

    python scripts/render_xero_media.py   (needs: pip install playwright && playwright install chromium)

1. media/xero-dry-run.png  the real dry run on five saved invoice extractions (no Xero call)
2. media/xero-tests.png    the 55 Xero tests passing against the fake Xero
"""
import html
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "xero"
MEDIA = ROOT / "media"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def colour(line: str) -> str:
    t = html.escape(line)
    if line.startswith("$"):
        return f"<span style='color:#7ee787'>{t}</span>"
    if "DRY RUN" in line or "PASSED" in line or "passed" in line:
        return f"<span style='color:#3fb950'>{t}</span>"
    if "HELD" in line:
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


def dry_run_lines() -> list[str]:
    # The summaries only: the JSON payloads after each one are cut, except the first.
    out, in_json, shown_json = [], False, False
    for line in (RUN / "1-dry-run.txt").read_text().splitlines():
        if line == "{":
            in_json = True
            if not shown_json:
                out.append(line)
            continue
        if in_json:
            if line == "}":
                in_json = False
                if not shown_json:
                    out.append(line)
                    shown_json = True
            elif not shown_json:
                out.append(line)
            continue
        out.append(line)
    return out


def main():
    MEDIA.mkdir(exist_ok=True)
    pages = [
        (page_for(dry_run_lines(), "Real dry run, 5 November 2026: five saved invoice extractions to Xero bill "
                                   "payloads. No Xero call is made in a dry run."), "xero-dry-run.png"),
        (page_for((RUN / "2-mocked-tests.txt").read_text().splitlines(),
                  "pytest tests/test_xero.py -v  (Xero mocked over HTTP, built to Xero's documented shapes)"),
         "xero-tests.png"),
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
