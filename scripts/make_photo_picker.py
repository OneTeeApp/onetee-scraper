#!/usr/bin/env python3
"""
Build the photo picker: a page where a human chooses each course's picture.

WHY THIS EXISTS
`find_course_photos.py` crawls each course's own website and proposes a photo.
It is a guess. No filename can tell us whether there are people in the frame,
whether the picture is of the course or of the parking lot, or whether the
"clubhouse" shot is actually a wedding marquee. So the crawl proposes and a
human disposes, exactly as `enrich_phones.py` proposes and
`local/phones.curated.json` disposes.

This script turns the crawl output into a single self-contained HTML page:
every course in the directory for a state, its proposed photo, its runners-up,
and a box to paste anything better. Choices are saved in the browser as you go
and exported as `photos.curated.json`, which is the file the build reads.

THE CHAIN
  find_course_photos.py  ->  data/course_photos.json        (the crawl's guess)
  make_photo_picker.py   ->  data/photo_picker.html         (this page)
  <a human, clicking>    ->  local/photos.curated.json      (the decision)
  build_directory.py     ->  a `photo` field on every venue  (what ships)

Curated beats crawl, and an explicit null in the curated file is a deliberate
"show nothing here" that beats both — the same membership test the phone
pipeline uses, for the same reason.

USAGE
  python scripts/make_photo_picker.py --state CO
  python scripts/make_photo_picker.py --state CO --out data/photo_picker.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit(f"could not read {path}: {exc}")


def warnings_for(curated):
    """
    Problems a human cannot see in a thumbnail.

    Three kinds, all found in the first real pass: a URL that is not a URL a
    browser can load, and a picture doing duty for more than one course. The
    second is not always wrong — two nines on one site legitimately share a
    photo — so it is a flag to look at, never an automatic rejection.
    """
    warn = {}
    seen = {}
    for vid, e in curated.items():
        url = (e.get("image") if isinstance(e, dict) else e) or ""
        if not url:
            continue
        low = url.strip().lower()
        if low.startswith("data:"):
            warn[vid] = "This is a base64 blob, not a link — it will not load."
        elif " 1x," in url or " 2x," in url or url.count("http") > 1:
            warn[vid] = "This is a whole srcset, not one image URL."
        elif low.startswith("http://"):
            warn[vid] = "http:// is blocked as mixed content on the https site."
        seen.setdefault(url, []).append(vid)

    for url, vids in seen.items():
        if len(vids) > 1:
            others = ", ".join(v for v in vids)
            for vid in vids:
                if vid not in warn:
                    warn[vid] = "Same picture as: " + others.replace(vid + ", ", "").replace(", " + vid, "")
    return warn


def build_rows(state, full, directory, curated):
    """
    One entry per course in the directory for this state — including the ones
    the crawl found nothing for. A course with no candidates is exactly the
    case a human needs to see: it is where pasting a URL is the only fix, and
    leaving it off the page would quietly hide the gap.
    """
    by_vid = {r.get("venue_id"): r for r in full if r.get("venue_id")}
    warn = warnings_for(curated)
    rows = []
    for c in directory:
        if state and (c.get("state") or "").upper() != state.upper():
            continue
        vid = c.get("venue_id")
        crawled = by_vid.get(vid, {})
        cands = []
        if crawled.get("image"):
            cands.append({"url": crawled["image"], "score": crawled.get("score"),
                          "w": crawled.get("w"), "h": crawled.get("h"),
                          "source": "site", "licence": "", "credit": "",
                          "page": crawled.get("page") or ""})
        for a in (crawled.get("alts") or []):
            # Website-crawl alternates carry `image`; the supplemental sources
            # carry `url` plus where it came from and what licence it is under.
            cands.append({"url": a.get("image") or a.get("url"),
                          "score": a.get("score"),
                          "w": a.get("w"), "h": a.get("h"),
                          "source": a.get("source") or "site",
                          "licence": a.get("licence") or "",
                          "credit": a.get("credit") or "",
                          "page": a.get("page") or ""})
        rows.append({
            "venue_id": vid,
            "name": c.get("name") or vid,
            "city": c.get("city") or "",
            "state": c.get("state") or "",
            "website": c.get("website") or "",
            "page": crawled.get("page") or "",
            "note": crawled.get("note") or "",
            "candidates": [x for x in cands if x.get("url")],
            "chosen": curated.get(vid, {}).get("image")
            if isinstance(curated.get(vid), dict) else curated.get(vid),
            "hasChoice": vid in curated,
            "warn": warn.get(vid, ""),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>OneTee — choose course photos ({state})</title>
<style>
 :root {{ --ink:#111; --muted:#666; --line:#ddd; --bg:#f6f5f2; --green:hsl(92,55%,46%); }}
 * {{ box-sizing:border-box; }}
 body {{ font:15px/1.5 system-ui,-apple-system,sans-serif; margin:0; background:var(--bg); color:var(--ink); }}
 header {{ position:sticky; top:0; z-index:10; background:#fff; border-bottom:1px solid var(--line);
          padding:14px 22px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }}
 h1 {{ font-size:18px; margin:0; }}
 .stat {{ color:var(--muted); font-size:13px; }}
 .stat b {{ color:var(--ink); }}
 button {{ font:inherit; padding:7px 14px; border-radius:8px; border:1px solid var(--line);
          background:#fff; cursor:pointer; }}
 button.primary {{ background:var(--green); border-color:var(--green); color:#08192b; font-weight:700; }}
 input[type=text], input[type=search] {{ font:inherit; padding:7px 10px; border:1px solid var(--line);
          border-radius:8px; background:#fff; }}
 main {{ padding:20px 22px 80px; display:grid; gap:16px;
         grid-template-columns:repeat(auto-fill,minmax(430px,1fr)); }}
 .card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px; }}
 .card.done {{ border-color:var(--green); box-shadow:0 0 0 2px hsl(92,50%,88%); }}
 .card.none {{ opacity:.62; }}
 .hd {{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }}
 .hd b {{ font-size:15px; }}
 .hd span {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
 .shots {{ display:flex; gap:8px; margin-top:10px; overflow-x:auto; padding-bottom:4px; }}
 .shot {{ flex:0 0 auto; width:132px; cursor:pointer; border:2px solid transparent;
          border-radius:8px; padding:2px; background:none; }}
 .shot img {{ width:100%; height:88px; object-fit:cover; border-radius:6px; background:#eee; display:block; }}
 .shot.sel {{ border-color:var(--green); }}
 .shot em {{ font-size:10px; color:var(--muted); font-style:normal; display:block; margin-top:3px;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
 .shot em.lic {{ color:#166534; }}
 .shot em.lic.bad {{ color:#c2410c; }}
 .shot.risky img {{ outline:2px dashed #f6c9ab; outline-offset:-2px; }}
 .empty {{ font-size:13px; color:var(--muted); padding:12px 0; }}
 .row {{ display:flex; gap:8px; margin-top:10px; align-items:center; }}
 .row input {{ flex:1; min-width:0; }}
 .links {{ font-size:12px; margin-top:8px; }}
 .links a {{ color:#3a6ea5; }}
 .big {{ margin-top:10px; }}
 .big img {{ width:100%; height:200px; object-fit:cover; border-radius:8px; background:#eee; }}
 .tag {{ font-size:11px; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
         padding:2px 8px; border-radius:999px; background:#eee; color:var(--muted); }}
 .tag.ok {{ background:var(--green); color:#08192b; }}
 .tag.no {{ background:#444; color:#fff; }}
 .tag.warn {{ background:#c2410c; color:#fff; }}
 .warnbox {{ margin-top:8px; font-size:12.5px; background:#fff4ed; border:1px solid #f6c9ab;
             color:#7c2d12; border-radius:8px; padding:7px 10px; }}
 .card.flag {{ border-color:#f6c9ab; box-shadow:0 0 0 2px #ffe8d9; }}
</style>

<header>
  <h1>Choose course photos — {state}</h1>
  <span class="stat"><b id="nDone">0</b> of <b>{total}</b> decided ·
    <b id="nNone">0</b> set to no photo &middot;
    <b id="nWarn">{flagged}</b> flagged to check</span>
  <input type="search" id="q" placeholder="filter by name, city, or id" style="min-width:230px">
  <label class="stat"><input type="checkbox" id="onlyTodo"> only undecided</label>
  <label class="stat"><input type="checkbox" id="onlyWarn"> only flagged</label>
  <button class="primary" id="dl">Download photos.curated.json</button>
  <button id="clear">Clear all choices</button>
</header>

<main id="grid"></main>

<script>
const ROWS = {rows};
const KEY = 'onetee-photo-picks-{state}';

// Choices survive a reload, because deciding 240 courses is not one sitting.
let picks = {{}};
try {{ picks = JSON.parse(localStorage.getItem(KEY) || '{{}}'); }} catch (e) {{ picks = {{}}; }}
ROWS.forEach(r => {{ if (r.hasChoice && !(r.venue_id in picks)) picks[r.venue_id] = r.chosen || null; }});

const save = () => {{
  try {{ localStorage.setItem(KEY, JSON.stringify(picks)); }} catch (e) {{}}
  const vals = Object.values(picks);
  document.getElementById('nDone').textContent = vals.length;
  document.getElementById('nNone').textContent = vals.filter(v => !v).length;
}};

function card(r) {{
  const chosen = (r.venue_id in picks) ? picks[r.venue_id] : undefined;
  const decided = r.venue_id in picks;
  const el = document.createElement('section');
  el.className = 'card' + (decided ? ' done' : '') + (decided && !chosen ? ' none' : '')
              + (r.warn ? ' flag' : '');
  el.dataset.vid = r.venue_id;
  el.dataset.hay = (r.name + ' ' + r.city + ' ' + r.venue_id).toLowerCase();
  el.dataset.todo = decided ? '0' : '1';
  el.dataset.warn = r.warn ? '1' : '0';

  const tag = r.warn ? '<span class="tag warn">check this</span>'
            : !decided ? '<span class="tag">undecided</span>'
            : (chosen ? '<span class="tag ok">chosen</span>'
                      : '<span class="tag no">no photo</span>');

  const SRC = {{ site:'course site', wikimedia:'Wikimedia', flickr:'Flickr',
               brave:'Brave', usgs:'USGS aerial' }};
  const shots = r.candidates.map((c, i) => {{
    const risky = c.source === 'brave';
    return '<button class="shot' + (chosen === c.url ? ' sel' : '') +
      (risky ? ' risky' : '') + '" data-url="' + esc(c.url) + '" title="' +
      esc((c.licence || 'licence not stated') + (c.credit ? ' \u2014 ' + c.credit : '')) + '">' +
      '<img loading="lazy" src="' + esc(c.url) + '" alt="">' +
      '<em>' + esc(SRC[c.source] || c.source || 'site') + ' &middot; ' +
        (c.w || '?') + '\u00d7' + (c.h || '?') + '</em>' +
      (c.licence ? '<em class="lic' + (risky ? ' bad' : '') + '">' + esc(c.licence) + '</em>' : '') +
    '</button>';
  }}).join('');

  el.innerHTML =
    '<div class="hd"><b>' + esc(r.name) + '</b><span>' + esc(r.city) + ' &middot; ' + tag + '</span></div>' +
    (r.warn ? '<div class="warnbox">' + esc(r.warn) + '</div>' : '') +
    (chosen ? '<div class="big"><img loading="lazy" src="' + esc(chosen) + '" alt=""></div>' : '') +
    (r.candidates.length
      ? '<div class="shots">' + shots + '</div>'
      : '<div class="empty">No candidate found' + (r.note ? ' \\u2014 ' + esc(r.note) : '') +
        '. Open the site and paste a picture URL.</div>') +
    '<div class="row">' +
      '<input type="text" class="url" placeholder="or paste an image URL" value="' +
        (chosen && !r.candidates.some(c => c.url === chosen) ? esc(chosen) : '') + '">' +
      '<button class="use">Use</button><button class="none">No photo</button>' +
      (decided ? '<button class="undo">Undo</button>' : '') +
    '</div>' +
    '<div class="links">' +
      (r.website ? '<a href="' + esc(r.website) + '" target="_blank" rel="noopener">course site</a>' : '') +
      (r.page ? ' &middot; <a href="' + esc(r.page) + '" target="_blank" rel="noopener">source page</a>' : '') +
      ' &middot; <code>' + esc(r.venue_id) + '</code>' +
    '</div>';
  return el;
}}

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

const grid = document.getElementById('grid');
function render() {{
  grid.textContent = '';
  const frag = document.createDocumentFragment();
  ROWS.forEach(r => frag.appendChild(card(r)));
  grid.appendChild(frag);
  filter();
  save();
}}

function redrawOne(vid) {{
  const old = grid.querySelector('[data-vid="' + CSS.escape(vid) + '"]');
  const r = ROWS.find(x => x.venue_id === vid);
  if (old && r) old.replaceWith(card(r));
  filter();
  save();
}}

grid.addEventListener('click', e => {{
  const cardEl = e.target.closest('.card');
  if (!cardEl) return;
  const vid = cardEl.dataset.vid;
  const shot = e.target.closest('.shot');
  if (shot) {{ picks[vid] = shot.dataset.url; return redrawOne(vid); }}
  if (e.target.classList.contains('use')) {{
    const v = cardEl.querySelector('.url').value.trim();
    if (v) {{ picks[vid] = v; redrawOne(vid); }}
    return;
  }}
  if (e.target.classList.contains('none')) {{ picks[vid] = null; return redrawOne(vid); }}
  if (e.target.classList.contains('undo')) {{ delete picks[vid]; return redrawOne(vid); }}
}});

function filter() {{
  const q = document.getElementById('q').value.trim().toLowerCase();
  const todo = document.getElementById('onlyTodo').checked;
  const flagged = document.getElementById('onlyWarn').checked;
  grid.querySelectorAll('.card').forEach(c => {{
    const hit = (!q || c.dataset.hay.includes(q)) && (!todo || c.dataset.todo === '1')
              && (!flagged || c.dataset.warn === '1');
    c.style.display = hit ? '' : 'none';
  }});
}}
document.getElementById('q').addEventListener('input', filter);
document.getElementById('onlyTodo').addEventListener('change', filter);
document.getElementById('onlyWarn').addEventListener('change', filter);

document.getElementById('dl').addEventListener('click', () => {{
  // Shape mirrors local/phones.curated.json so build_directory.py reads the
  // two the same way: a courses map, and null meaning "deliberately nothing".
  const courses = {{}};
  Object.keys(picks).sort().forEach(vid => {{ courses[vid] = {{ image: picks[vid] }}; }});
  const blob = new Blob([JSON.stringify({{ courses }}, null, 2)], {{ type: 'application/json' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'photos.curated.json';
  a.click();
  URL.revokeObjectURL(a.href);
}});

document.getElementById('clear').addEventListener('click', () => {{
  if (!confirm('Clear every choice on this page?')) return;
  picks = {{}};
  render();
}});

render();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the course-photo picker page.")
    ap.add_argument("--state", default="CO", help="two-letter state (blank = all)")
    ap.add_argument("--full", default="data/course_photos_full.json")
    ap.add_argument("--directory", default="directory.json")
    ap.add_argument("--curated", default="local/photos.curated.json")
    ap.add_argument("--out", default="data/photo_picker.html")
    args = ap.parse_args()

    full = load_json(args.full, [])
    directory = load_json(args.directory, {})
    if isinstance(directory, dict):
        directory = directory.get("courses") or []
    curated = (load_json(args.curated, {}) or {}).get("courses") or {}

    if not directory:
        sys.exit(f"no courses in {args.directory} — run build_directory.py first")

    rows = build_rows(args.state, full, directory, curated)
    if not rows:
        sys.exit(f"no courses matched state {args.state!r}")

    parent = os.path.dirname(args.out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(PAGE.format(state=escape(args.state or "all"),
                             total=len(rows),
                             flagged=sum(1 for r in rows if r["warn"]),
                             rows=json.dumps(rows)))

    withc = sum(1 for r in rows if r["candidates"])
    flagged = sum(1 for r in rows if r["warn"])
    print(f"wrote {args.out}: {len(rows)} courses, {withc} with at least one candidate, "
          f"{len(rows) - withc} needing a pasted URL, {flagged} flagged to re-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
