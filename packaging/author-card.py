#!/usr/bin/env python3
"""Render a small SVG stats card for the GitHub profile README.

Reads the JSON summary printed by generate.py on stdin and writes the
SVG to stdout. Pure static SVG: no scripts, no external references,
so GitHub's image proxy renders it fine.
"""
import datetime as dt
import html
import json
import sys


def main():
    summary = json.load(sys.stdin)
    counts = summary.get("counts", {})
    author = summary.get("author", "")
    md = summary.get("date", "")
    try:
        label = dt.datetime.strptime("2026-%s" % md, "%Y-%m-%d").strftime(
            "%B %d")
    except ValueError:
        label = md

    bits = []
    if counts.get("commits"):
        bits.append("%d commit%s" % (counts["commits"],
                                    "" if counts["commits"] == 1 else "s"))
    if counts.get("merged"):
        bits.append("%d PR%s merged" % (counts["merged"],
                                        "" if counts["merged"] == 1 else "s"))
    if counts.get("prs_opened"):
        bits.append("%d PR%s opened" % (counts["prs_opened"],
                                        "" if counts["prs_opened"] == 1 else "s"))
    if counts.get("closed"):
        bits.append("%d issue%s closed" % (counts["closed"],
                                           "" if counts["closed"] == 1 else "s"))
    stats = " · ".join(bits) if bits else "a quiet day in the archives"

    esc = html.escape
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="600" height="170" role="img">
<rect width="600" height="170" fill="#f5f1e6" stroke="#8a8272" stroke-width="2"/>
<text x="30" y="38" font-family="Georgia, serif" font-size="15" letter-spacing="4" fill="#6b655a">THE DAILY COMMIT</text>
<text x="30" y="82" font-family="Georgia, serif" font-size="34" font-weight="bold" fill="#1c1a16">%s</text>
<text x="30" y="114" font-family="Georgia, serif" font-size="16" fill="#1c1a16">%s</text>
<text x="30" y="140" font-family="Georgia, serif" font-size="14" fill="#6b655a">%s</text>
<text x="30" y="160" font-family="Georgia, serif" font-size="11" fill="#8a8272">roguealg0.github.io/the-daily-commit/author/</text>
</svg>""" % (esc(author), esc(label), esc(stats))
    sys.stdout.write(svg)


if __name__ == "__main__":
    main()
