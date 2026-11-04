"""Render the QuickBooks README images headless with Playwright, from real run output.

    python scripts/render_qbo_media.py   (needs: pip install playwright && playwright install chromium)

1. media/qbo-bill-readback.png  the three Bills as read back from the QuickBooks sandbox API
2. media/qbo-test-run.png       the live push, the re-run that hit the duplicate guard, and the held invoices
"""
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "quickbooks"
MEDIA = ROOT / "media"
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def bill_card(b: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(line.get('Description', ''))}</td>"
        f"<td style='color:#555'>{html.escape(line['AccountBasedExpenseLineDetail']['AccountRef']['name'])}</td>"
        f"<td style='text-align:right'>{line['Amount']:,.2f}</td></tr>"
        for line in b["Line"])
    return f"""<div style="border:1px solid #d0d7de;border-radius:10px;padding:18px 22px;margin-bottom:16px;background:#fff">
<div style="display:flex;justify-content:space-between;align-items:baseline">
  <div style="font-size:19px;font-weight:700">{html.escape(b['VendorRef']['name'])}</div>
  <div style="font-size:13px;color:#1a7f37;font-weight:600">Bill Id {b['Id']} &middot; read back from the API</div></div>
<div style="font-size:13px;color:#555;margin:4px 0 10px">Bill no. <b>{html.escape(b['DocNumber'])}</b> &middot;
  dated {b['TxnDate']} &middot; due {b['DueDate']} &middot; vendor Id {b['VendorRef']['value']}</div>
<table style="width:100%;border-collapse:collapse;font-size:14px">
<tr style="text-align:left;color:#57606a;border-bottom:1px solid #d0d7de"><th>Line</th><th>Expense account</th>
<th style="text-align:right">USD</th></tr>{rows}
<tr style="border-top:2px solid #24292f;font-weight:700"><td>Total</td><td></td>
<td style="text-align:right">{b['TotalAmt']:,.2f}</td></tr></table>
<div style="font-size:12px;color:#9a6700;margin-top:8px">Private note: {html.escape(b.get('PrivateNote', ''))}</div></div>"""


def readback_page(bills: dict) -> str:
    cards = "".join(bill_card(b) for b in bills.values())
    return f"""<html><body style="margin:0;background:#f6f8fa;font-family:{FONT}">
<div id="wrap" style="display:inline-block;width:900px;padding:26px 32px">
<div style="font-size:22px;font-weight:700">Bills created in a QuickBooks Online sandbox company</div>
<div style="font-size:14px;color:#555;margin:4px 0 16px">Real run, 4 November 2026. Invoices read by Claude,
checked in code, pushed through the Accounting API v3, then each Bill read back by id.</div>
{cards}</div></body></html>"""


def colour(line: str) -> str:
    t = html.escape(line)
    if "CREATED" in line or "read back OK" in line:
        return f"<span style='color:#3fb950'>{t}</span>"
    if "HELD" in line or "DUPLICATE" in line:
        return f"<span style='color:#d29922'>{t}</span>"
    if line.strip().startswith("!"):
        return f"<span style='color:#f85149'>{t}</span>"
    if line.strip().startswith("-"):
        return f"<span style='color:#8b949e'>{t}</span>"
    return t


def terminal_page() -> str:
    blocks = []
    for name, cmd in [("2-live-push.txt", "qbo-push *.qbo-input.json --store keychain --label \"[TEST RECORD, portfolio demo]\""),
                      ("3-rerun-duplicates.txt", "qbo-push (same three invoices again)")]:
        text = (RUN / name).read_text().rstrip("\n").splitlines()
        body = "\n".join(colour(x) for x in text)
        blocks.append(f"<div style='color:#7ee787'>$ {html.escape(cmd)}</div>{body}\n")
    return f"""<html><body style="margin:0;background:#0d1117">
<div id="wrap" style="display:inline-block;padding:22px 28px;background:#0d1117">
<pre style="margin:0;font-family:{MONO};font-size:13px;line-height:1.45;color:#c9d1d9">{chr(10).join(blocks)}</pre>
</div></body></html>"""


def main():
    MEDIA.mkdir(exist_ok=True)
    bills = json.loads((RUN / "readback-bills.json").read_text())
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=2)
        for doc, out in [(readback_page(bills), "qbo-bill-readback.png"), (terminal_page(), "qbo-test-run.png")]:
            page.set_content(doc)
            page.locator("#wrap").screenshot(path=str(MEDIA / out))
            print("wrote media/" + out)
        b.close()


if __name__ == "__main__":
    main()
