"""Render the per-state coverage report as a single self-contained HTML page.

Reads the JSON emitted by `state_status.py --json`. Nothing here computes
anything: if a number looks wrong, it is wrong in state_status.py, not here.

The layout is chosen to survive fifty states. The national bar and the state
table both grow one row per state, which is fine. The part that would NOT
survive — a list of every broken course — is demoted into a collapsed panel
per state, and the thing you are meant to read every day is the blocker table,
which grows with the number of booking PLATFORMS (bounded, ~15) rather than
the number of courses (unbounded).

  python3 scripts/state_status.py --counts local/live_venues.tsv \
      --json local/state_status.json
  python3 scripts/state_dashboard.py local/state_status.json \
      -o local/onetee-status.html
"""
from __future__ import annotations

import argparse
import html
import json

BUCKET_ORDER = ["silent", "needs_ids", "experimental", "unsupported",
                "no_platform", "no_booking"]

# What you would actually DO about each bucket. Kept next to the number so the
# dashboard answers "so what" without anyone having to remember the model.
ACTION = {
    "silent": "Adapter runs but yields nothing — debug the adapter or confirm "
              "the course is genuinely closed.",
    "needs_ids": "Capture the missing tenant/course identifiers. Mechanical, "
                 "one pass per platform.",
    "experimental": "Promote the adapter to production (golfnow, ezlinks).",
    "unsupported": "Needs a new adapter for this booking engine, or the "
                   "course is not scrapable at all.",
    "no_platform": "Source directory has a booking URL we could not classify.",
    "no_booking": "Directory says no online booking. Verify, then either find "
                  "the booking engine or accept the gap.",
}

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#26303d;--txt:#e6edf3;
--dim:#8b949e;--live:#2ea043;--warn:#d29922;--bad:#da3633;--info:#388bfd}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;margin:0 0 2px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);margin:38px 0 12px;font-weight:600}
.sub{color:var(--dim);margin:0 0 28px;font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px 18px;flex:1;min-width:150px}
.card .n{font-size:26px;font-weight:600;line-height:1.1}
.card .l{color:var(--dim);font-size:12px;margin-top:3px}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{text-align:right;padding:9px 10px;font-size:11px;text-transform:uppercase;
letter-spacing:.05em;color:var(--dim);border-bottom:1px solid var(--line);
font-weight:600;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);
white-space:nowrap}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#1c2430}
.bar{position:relative;height:6px;background:#26303d;border-radius:3px;
min-width:90px}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--live);
border-radius:3px}
.z{color:#3d4753}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
border:1px solid}
.t-silent{color:var(--bad);border-color:#5a1f1f;background:#2a1416}
.t-needs_ids{color:var(--warn);border-color:#5a4416;background:#2a2113}
.t-experimental{color:var(--info);border-color:#1f3f6e;background:#111d31}
.t-unsupported{color:var(--dim);border-color:var(--line);background:#1a212b}
.t-no_platform,.t-no_booking{color:var(--dim);border-color:var(--line);
background:#1a212b}
details{background:var(--panel);border:1px solid var(--line);border-radius:8px;
margin-bottom:10px}
summary{padding:12px 16px;cursor:pointer;font-weight:600;
display:flex;gap:14px;align-items:center}
summary::-webkit-details-marker{display:none}
summary:before{content:"\\25B8";color:var(--dim);font-size:11px}
details[open] summary:before{content:"\\25BE"}
summary .pill{margin-left:auto;font-weight:400;color:var(--dim);font-size:12px}
.body{padding:0 16px 16px;border-top:1px solid var(--line)}
.grp{margin-top:16px}
.grp .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}
.grp .act{color:var(--dim);font-size:12px;margin:0 0 8px}
.grp ul{margin:0;padding:0;list-style:none;
display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:2px}
.grp li{font-size:13px;padding:3px 0;color:#c9d1d9}
.grp li span{color:var(--dim)}
.note{color:var(--dim);font-size:12px;margin-top:10px}
.ok{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px 18px;color:var(--dim)}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def render(rep: dict, stamp: str) -> str:
    states = rep["states"]
    live = sum(s["counts"]["live"] for s in states)
    addr = sum(s["addressable"] for s in states)
    total = sum(s["venues"] for s in states)
    priv = total - addr
    gap = addr - live
    pct = 100.0 * live / addr if addr else 0

    o = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
         "<meta name=viewport content='width=device-width,initial-scale=1'>",
         "<title>OneTee — coverage by state</title>",
         f"<style>{CSS}</style></head><body><div class=wrap>"]

    o.append("<h1>OneTee coverage by state</h1>")
    o.append(f"<p class=sub>{esc(stamp)} &middot; {len(states)} state"
             f"{'s' if len(states) != 1 else ''} live &middot; "
             "every course lands in exactly one bucket, so the columns add up "
             "to the whole market.</p>")

    o.append("<div class=cards>")
    for n, l in [(f"{pct:.0f}%", "of bookable courses live"),
                 (live, "live now"),
                 (gap, "bookable, not yet live"),
                 (addr, "bookable courses"),
                 (priv, "private / military (out of scope)")]:
        o.append(f"<div class=card><div class=n>{esc(n)}</div>"
                 f"<div class=l>{esc(l)}</div></div>")
    o.append("</div>")

    # ---- state table -------------------------------------------------
    o.append("<h2>By state</h2><table><thead><tr><th>State</th>"
             "<th>Coverage</th><th>Live</th><th>Bookable</th>")
    for b in BUCKET_ORDER:
        o.append(f"<th>{esc(b.replace('_', ' '))}</th>")
    o.append("</tr></thead><tbody>")
    for s in sorted(states, key=lambda s: -(s["counts"]["live"]
                                            / (s["addressable"] or 1))):
        p = 100.0 * s["counts"]["live"] / (s["addressable"] or 1)
        o.append(f"<tr><td><b>{esc(s['state'])}</b></td>"
                 f"<td><div class=bar><i style='width:{p:.0f}%'></i></div></td>"
                 f"<td>{s['counts']['live']} <span class=z>"
                 f"({p:.0f}%)</span></td><td>{s['addressable']}</td>")
        for b in BUCKET_ORDER:
            n = s["counts"].get(b, 0)
            o.append(f"<td class='{'z' if not n else ''}'>{n}</td>")
        o.append("</tr>")
    o.append("</tbody></table>")
    o.append("<p class=note>Bookable excludes private and military clubs — no "
             "aggregator can sell those, so counting them as misses would "
             "understate coverage forever.</p>")

    # ---- blockers ----------------------------------------------------
    o.append("<h2>What's blocking &mdash; the whole backlog</h2>")
    if rep.get("blockers"):
        o.append("<table><thead><tr><th>Bucket</th><th>Platform</th>"
                 "<th>Courses</th><th>States</th><th>Fix</th>"
                 "</tr></thead><tbody>")
        for e in rep["blockers"]:
            spread = ", ".join(f"{k} {v}" for k, v in sorted(e["states"].items()))
            o.append(f"<tr><td><span class='tag t-{esc(e['bucket'])}'>"
                     f"{esc(e['bucket'].replace('_', ' '))}</span></td>"
                     f"<td>{esc(e['platform'])}</td>"
                     f"<td><b>{e['courses']}</b></td>"
                     f"<td>{esc(spread)}</td>"
                     f"<td style='text-align:left;white-space:normal;"
                     f"color:var(--dim);font-size:12px'>"
                     f"{esc(ACTION.get(e['bucket'], ''))}</td></tr>")
        o.append("</tbody></table>")
        o.append("<p class=note>One row per unit of work, not per course. This "
                 "table grows with the number of booking platforms, which is "
                 "bounded &mdash; which is why it still reads at 50 states "
                 "while a list of course names would not.</p>")
    else:
        o.append("<div class=ok>Nothing blocked.</div>")

    # ---- integrity ---------------------------------------------------
    drift = rep.get("orphan_registry") or []
    unattr = rep.get("orphan_live") or []
    o.append("<h2>Data integrity</h2>")
    if not drift and not unattr:
        o.append("<div class=ok>Clean. Every registry entry maps to a course "
                 "in a state directory, and every course serving tee times is "
                 "traceable back to one.</div>")
    else:
        o.append("<table><thead><tr><th>Problem</th><th>What it means</th>"
                 "<th>Count</th></tr></thead><tbody>")
        if drift:
            o.append("<tr><td>Registry drift</td><td style='text-align:left;"
                     "white-space:normal'>In registry.json with no matching "
                     "course in any state directory &mdash; renamed or "
                     "retired.</td>"
                     f"<td><b>{len(drift)}</b></td></tr>")
        if unattr:
            o.append("<tr><td>Unattributed live</td><td style='text-align:left;"
                     "white-space:normal'>Serving tee times but not traceable "
                     "to a directory course &mdash; a stale slug in D1.</td>"
                     f"<td><b>{len(unattr)}</b></td></tr>")
        o.append("</tbody></table>")
        for k in unattr[:40]:
            o.append(f"<p class=note>{esc(k)}</p>")

    # ---- per-state detail --------------------------------------------
    o.append("<h2>Course detail</h2>")
    for s in sorted(states, key=lambda s: s["state"]):
        p = 100.0 * s["counts"]["live"] / (s["addressable"] or 1)
        stuck = sum(s["counts"].get(b, 0) for b in BUCKET_ORDER)
        o.append(f"<details><summary>{esc(s['state'])}"
                 f"<span class=pill>{s['counts']['live']} live of "
                 f"{s['addressable']} bookable ({p:.0f}%) &middot; {stuck} "
                 f"not live</span></summary><div class=body>")
        any_shown = False
        for b in BUCKET_ORDER:
            items = s["detail"].get(b) or []
            if not items:
                continue
            any_shown = True
            o.append(f"<div class=grp><div class=hd>"
                     f"<span class='tag t-{esc(b)}'>"
                     f"{esc(b.replace('_', ' '))}</span>"
                     f"<b>{len(items)}</b></div>"
                     f"<p class=act>{esc(ACTION.get(b, ''))}</p><ul>")
            for it in sorted(items, key=lambda i: i["name"]):
                plat = it.get("platform") or "no platform"
                o.append(f"<li>{esc(it['name'])} "
                         f"<span>&middot; {esc(plat)} &middot; "
                         f"{esc(it.get('city'))}</span></li>")
            o.append("</ul></div>")
        if not any_shown:
            o.append("<p class=note>Fully covered.</p>")
        o.append("</div></details>")

    o.append("</div></body></html>")
    return "".join(o)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="output of state_status.py --json")
    ap.add_argument("-o", "--out", default="local/onetee-status.html")
    ap.add_argument("--stamp", default="", help="as-of label for the header")
    a = ap.parse_args()
    with open(a.json) as fh:
        rep = json.load(fh)
    with open(a.out, "w") as fh:
        fh.write(render(rep, a.stamp or "coverage snapshot"))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
