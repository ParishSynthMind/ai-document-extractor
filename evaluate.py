"""Score extraction results against the hand-written answers in samples/expected/.

    python evaluate.py results/          # prints a markdown accuracy table

A field counts as correct when it matches the expected value (numbers to the
penny; text ignoring case, spaces and punctuation; "not in the document" must
come back as null). The table also shows how many wrong fields the review
list caught, because a wrong value that was flagged for review is a very
different thing from a wrong value that slipped through.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXPECTED = Path(__file__).resolve().parent / "samples" / "expected"


def same(expected, got) -> bool:
    if expected is None or got is None:
        return expected is None and got is None
    if isinstance(expected, bool) or isinstance(got, bool):
        return expected is got
    if isinstance(expected, (int, float)):
        try:
            return abs(float(got) - float(expected)) < 0.005
        except (TypeError, ValueError):
            return False
    squash = lambda s: re.sub(r"[^a-z0-9@]", "", str(s).lower())
    return squash(expected) == squash(got)


def score(result: dict, expected: dict) -> dict:
    flagged = {r["field"] for r in result["needs_review"]}
    rows = []
    for name, want in expected["fields"].items():
        got = result["fields"][name]
        rows.append({
            "field": name, "expected": want, "got": got["value"], "confidence": got["confidence"],
            "correct": same(want, got["value"]), "flagged": name in flagged,
        })
    return {
        "file": result["file"], "method": result["method"], "rows": rows,
        "correct": sum(r["correct"] for r in rows), "total": len(rows),
        "flagged": len(flagged),
        "wrong_flagged": sum(not r["correct"] and r["flagged"] for r in rows),
        "wrong_unflagged": sum(not r["correct"] and not r["flagged"] for r in rows),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="folder of result JSON files (one per sample)")
    ap.add_argument("--detail", action="store_true", help="also list every wrong or flagged field")
    args = ap.parse_args(argv)
    scores = []
    for exp_file in sorted(EXPECTED.glob("*.json")):
        res_file = Path(args.results) / exp_file.name
        if not res_file.exists():
            print(f"missing result for {exp_file.stem}", file=sys.stderr)
            continue
        scores.append(score(json.loads(res_file.read_text()), json.loads(exp_file.read_text())))
    if not scores:
        print("no results to score", file=sys.stderr)
        return 1

    print("| Document | Read via | Fields correct | Sent to review | Wrong and caught by review | Wrong and not caught |")
    print("|---|---|---|---|---|---|")
    for s in scores:
        print(f"| {s['file']} | {s['method']} | {s['correct']}/{s['total']} | {s['flagged']} | "
              f"{s['wrong_flagged']} | {s['wrong_unflagged']} |")
    c, t = sum(s["correct"] for s in scores), sum(s["total"] for s in scores)
    print(f"| **All** | | **{c}/{t} ({100 * c / t:.0f}%)** | {sum(s['flagged'] for s in scores)} | "
          f"{sum(s['wrong_flagged'] for s in scores)} | {sum(s['wrong_unflagged'] for s in scores)} |")
    if args.detail:
        print()
        for s in scores:
            for r in s["rows"]:
                if not r["correct"] or r["flagged"]:
                    mark = "OK " if r["correct"] else "BAD"
                    print(f"- {mark} {s['file']} {r['field']}: expected {r['expected']!r}, got {r['got']!r} "
                          f"({r['confidence']}{', flagged' if r['flagged'] else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
