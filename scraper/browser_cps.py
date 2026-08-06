"""Headless-browser fetcher for Club Prophet (cps.golf) tenants.

Some cps.golf tenants (City of Boulder / Flatirons, Fossil Trace, ...) run a WAF
that 403s the plain HTTP client's TLS fingerprint from a datacenter IP, while
letting a real browser through. Proven on GitHub's runner: Flatirons/Fossil
returned 200 via a headless Chromium and 403 via `requests`; Indian Peaks works
either way. So we run the SAME public anonymous token->register->TeeTimes flow
(see adapters/clubprophet.py) inside a real Chromium via Playwright and emit an
aggregate-format JSON document for `scraper.d1 push`.

This owns ALL clubprophet courses (the plain scraper excludes the platform), so
the two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_cps --date 2026-07-24 --out output/cps.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import hashlib
import os
import pathlib
import re
import sys
import time
import urllib.parse
import uuid

import requests

from .adapters.base import USER_AGENT
from .adapters.clubprophet import ClubProphetAdapter
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# The anonymous flow, run inside the page (real browser TLS + tenant origin).
#
# MULTI-DATE (2026-08-06): auth is established ONCE per browser session — one
# Cloudflare clear (the goto), one token/apiKey, one wid/cids discovery — then
# EVERY date reuses it via a cheap JSON TeeTimes call. The residential page-load
# is the expensive, metered part; batching makes it a per-TENANT cost instead of
# per-(tenant,date). Near (days 0-2) drops from 3 page-loads to 1; deep (0-30)
# from 31 to 1. Returns {stage, status, results:[{date, status, content|parse_failed}]}.
FLOW_JS = r"""
async ([tenant, wid, cids, dates]) => {
  const base = "https://" + tenant + ".cps.golf";
  const api = base + "/onlineres/onlineapi/api/v1/onlinereservation";
  const ZG = "00000000-0000-0000-0000-000000000000";
  // TWO cps auth variants:
  //  (a) newer tenants (Indian Tree, ...) mint an anonymous Bearer token at
  //      /identityapi/myconnect/token/short and authorize the reservation API
  //      with "Authorization: Bearer <token>".
  //  (b) the "apiKey" variant (Highlands Ridge, Wakulla Sands, Southern Dunes,
  //      St James Bay, Musket Ridge, ...) has NO anonymous token — token/short
  //      404s — and instead serves a per-tenant x-apiKey via Home/Configuration,
  //      which alone authorizes the SAME reservation API. Verified live: Wakulla
  //      Sands GetAllOptions+TeeTimes 200 with x-apiKey and no Bearer (50 slots).
  // Collect whichever this tenant offers and send both; the server uses the one
  // it recognises, so a single flow covers both variants.
  let token = "", apiKey = "";
  try {
    const cfg = await fetch(base + "/onlineresweb/Home/Configuration")
                  .then(r => r.ok ? r.json() : null);
    if (cfg) {
      if (cfg.apiKey) apiKey = cfg.apiKey;
      if (!wid && cfg.websiteId && cfg.websiteId !== ZG) wid = cfg.websiteId;
    }
  } catch (e) {}
  const mintToken = async () => {
    const tr = await fetch(base + "/identityapi/myconnect/token/short",
      {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
       body:"client_id=onlinereswebshortlived"});
    let t = "";
    if (tr.status === 200) { try { t = (await tr.json()).access_token || ""; } catch (e) {} }
    return {status: tr.status, token: t};
  };
  let tk = await mintToken();
  token = tk.token;
  // Fail at the token stage ONLY when neither auth is available (a truly
  // unsupported/dead tenant). The apiKey variant reaches here with token "".
  if (!token && !apiKey) return {stage: "token", status: tk.status, results: []};
  const H = (w) => { const h = {"client-id":"onlineresweb",
    "x-terminalid":"3", "x-websiteid": w || ZG, "x-ismobile":"false", "x-productid":"1",
    "x-componentid":"1", "x-siteid":"1", "x-moduleid":"7",
    "x-timezoneid":"America/Denver", "x-timezone-offset":"360",
    "x-requestid":crypto.randomUUID(), "Accept":"application/json"};
    if (token) h["Authorization"] = "Bearer " + token;
    if (apiKey) h["x-apiKey"] = apiKey;
    return h; };
  // Discover websiteId + courseIds when the registry did not pin them. Done
  // in-browser (post-challenge) via GetAllOptions, which needs only the token.
  // This is why unpinned tenants (emerald-greens etc.) used to return nothing:
  // run() skipped every course without pinned course_ids. Now nothing is skipped.
  if (!wid || !cids) {
    const dr = await fetch(api + "/GetAllOptions/" + tenant, {headers:H(ZG)});
    const dtext = await dr.text();
    let db;
    try { db = JSON.parse(dtext); }
    catch (e) { return {stage: "discover", status: dr.status, parse_failed: true,
                        bytes: dtext.length, results: []}; }
    if (!wid) wid = db.webSiteId || (db.reservationOptions && db.reservationOptions.webSiteId) || "";
    if (!cids) {
      const ids = (db.courseOptions || []).map(c => c.courseId != null ? c.courseId : c.id)
                    .filter(x => x != null);
      cids = ids.join(",");
    }
  }
  if (!wid || !cids) return {stage: "discover", status: 200, results: [], note: "no wid/cids"};
  const teeOne = async (date) => {
    const txid = crypto.randomUUID();
    await fetch(api + "/RegisterTransactionId", {method:"POST",
      headers:{...H(wid), "Content-Type":"application/json"},
      body:JSON.stringify({transactionId:txid})});
    const url = api + `/TeeTimes?searchDate=${encodeURIComponent(date)}&holes=0`
      + `&numberOfPlayer=0&courseIds=${cids}&searchTimeType=0&transactionId=${txid}`
      + `&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true&teeSheetSearchView=5`
      + `&classCode=R&defaultOnlineRate=N&isUseCapacityPricing=false&memberStoreId=1&searchType=1`;
    return await fetch(url, {headers:H(wid)});
  };
  // One cheap TeeTimes call per date, reusing the cleared session. Report a
  // non-JSON body AS a per-date failure (same fix as browser_golfnow): a WAF
  // interstitial served with status 200 must not become "0 tee times, success".
  const results = [];
  for (const date of dates) {
    try {
      let tt = await teeOne(date);
      // The short-lived Bearer token can lapse across a long (deep, 31-date)
      // loop; a 401 mid-loop -> re-mint once and retry just this date.
      if (tt.status === 401 && !apiKey) {
        const rt = await mintToken();
        if (rt.token) { token = rt.token; tt = await teeOne(date); }
      }
      const text = await tt.text();
      let content;
      try { content = (JSON.parse(text)).content || []; }
      catch (e) { results.push({date: date, status: tt.status, parse_failed: true,
                                bytes: text.length}); continue; }
      results.push({date: date, status: tt.status, content: content});
    } catch (e) {
      results.push({date: date, status: -1, error: String(e)});
    }
  }
  return {stage: "teetimes", status: 200, results: results};
}
"""


def _teetimes(course: dict, slots: list[dict]) -> list:
    # cps.golf occasionally returns `content` items that are strings (an error or
    # notice payload) instead of slot dicts; s.get() then raised "AttributeError:
    # 'str' object has no attribute 'get'" and errored the WHOLE tenant for that
    # date (indian-peaks 2026-08-06, both ocean-city tenants earlier). Keep only
    # real dict slots so any valid rows still publish instead of losing the venue.
    slots = [s for s in slots if isinstance(s, dict)]
    # Label per sub-course when one tenant query spans several (Fossil Trace's
    # 3 nines, GVR's sheets) so same-time slots don't collapse in D1.
    names = {s.get("courseName") for s in slots if s.get("courseName")}
    multi = len(names) > 1
    out = []
    for s in slots:
        t = s.get("startTime")
        if not t:
            continue
        prices = ClubProphetAdapter._prices(s)
        # open_spots via the shared helper: availableParticipantNo is an ARRAY of
        # bookable party sizes, so max() = remaining seats (see adapters/
        # clubprophet.py). The old inline scalar check dropped every row to None.
        out.append(ClubProphetAdapter.base_tee_time(
            course, teetime=str(t), holes=ClubProphetAdapter._holes(s),
            course_label=(s.get("courseName") or "") if multi else "",
            open_spots=ClubProphetAdapter._open_spots(s),
            price_min=min(prices) if prices else None,
            price_max=max(prices) if prices else None,
            raw={"course_name": s.get("courseName", course["name"])}))
    return out


def _proxy_launch_kwargs(session: str | None = None) -> dict:
    """chromium.launch kwargs, adding a residential proxy if TEEITUP_PROXY is set.

    cps.golf (like kenna) blocks/challenges the data-center IP; a residential
    proxy clears it. Playwright's Chromium ignores HTTPS_PROXY on its own, so the
    proxy MUST be passed to launch(proxy=). TEEITUP_PROXY is the shared secret.

    STICKY SESSION (session=): when given, pin ONE residential IP for every
    request in a course's flow, because cps.golf's Cloudflare binds its
    clearance cookie to the IP (a rotating proxy that egresses page.goto on one
    IP and the TeeTimes fetch on another invalidates the cookie -> 403, the
    2026-08-04 19/27 failure). A FRESH session per attempt also hands each retry
    a new IP and gives concurrent courses distinct IPs.

    The sticky-session SYNTAX is provider-specific, so this is provider-aware:
      * Webshare residential (host contains 'webshare'): append a NUMERIC id to
        the username -> `{user}-{sid}` (their format is `{username}-{cc}-{id}`;
        same id = same exit IP for the dashboard session timer, so set that
        timer to a few minutes). Any trailing `-rotate` is stripped first so a
        rotating endpoint in the secret still becomes sticky here.
      * DataImpulse (default): append `;sessid.<id>` (docs.dataimpulse.com/
        proxies/parameters/session-id).
    If a session id already looks present we leave the username alone.
    """
    kwargs = {"args": ["--no-sandbox"]}
    url = re.sub(r"\s+", "", os.environ.get("TEEITUP_PROXY", ""))
    # Accept a secret pasted in the provider's bare `user:pass@host:port` form
    # (no scheme). urlparse then reads the USERNAME as the scheme — observed
    # 2026-08-06 (diag run 31078013717): every launch failed with
    # ERR_NO_SUPPORTED_PROXIES because "jrjqocbe-rotate" parsed as the scheme.
    if url and "://" not in url:
        url = "http://" + url
    if url:
        pu = urllib.parse.urlparse(url)
        server = f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")
        proxy = {"server": server}
        if pu.username:
            user = urllib.parse.unquote(pu.username)
            if session:
                host = (pu.hostname or "").lower()
                if "webshare" in host:
                    # Webshare residential country+sticky format is
                    # {username}-{country}-{session_id}. FORCE a US exit: this
                    # plan's default country is BRAZIL (diag 31081627529:
                    # `exit country: br`), and cps.golf's Cloudflare heavily
                    # challenges Brazilian residential IPs — the token/teetimes
                    # 403s that kept 19/20 tenants dark on 2026-08-06. A US exit
                    # was verified live: `{base}-us-{sid}` returned 96.43.121.90
                    # and held it across two requests (sticky ✓). The Webshare
                    # username is alphanumeric with no internal hyphen, so the
                    # base is everything before the first hyphen (strips any
                    # -rotate / -{cc} / -{sid} the secret already carries).
                    base = user.split("-", 1)[0]
                    cc = os.environ.get("CPS_PROXY_COUNTRY", "us").lower()
                    num = int(hashlib.md5(session.encode()).hexdigest()[:8], 16) % 1000000
                    user = f"{base}-{cc}-{num}"
                elif "sessid." not in user:
                    user = f"{user};sessid.{session}"
            proxy["username"] = user
        if pu.password:
            proxy["password"] = urllib.parse.unquote(pu.password)
        kwargs["proxy"] = proxy
        log.info("cps: routing Chromium through proxy %s (sticky=%s)",
                 server, session or "off")
    return kwargs


def _scrape_one_course(course: dict, dates: list) -> dict:
    """Scrape ONE cps tenant for ALL `dates` in its OWN Playwright instance.

    This is the unit of the thread pool in run(). A sync_playwright object is not
    shareable across threads, so each worker opens its own here. Returns
    {"slug", "dates": {iso: {"ok", "tts"|"error"}}} — one entry per requested date.

    ONE Cloudflare clear per session, ALL dates: the managed JS challenge is
    cleared once by the goto+cf_clearance poll, then FLOW_JS fetches every date's
    TeeTimes over the already-authorized (and cheap, JSON) API. This makes the
    expensive metered residential page-load a per-tenant cost, not per-(tenant,
    date) — the single biggest residential-bandwidth reduction (near 3->1 loads,
    deep 31->1). Fresh sticky proxy session per attempt pins ONE residential IP
    for the whole flow (Cloudflare's clearance cookie is IP-bound), fresh IP per
    retry. A retry re-solves the challenge (a full page-load), so we only retry
    when the session never reached the TeeTimes stage OR cleared but no date
    succeeded — a per-date success is kept even if a sibling date failed.
    """
    from playwright.sync_api import sync_playwright

    ids = course["ids"]
    tenant = ids["tenant"]
    wid = ids.get("website_id") or ""
    cids = ",".join(str(x) for x in (ids.get("course_ids") or []))
    # cps searchDate wants the human "%a %b %d %Y" form (correct weekday); map it
    # back to the ISO key the caller aggregates by.
    isos = [d.isoformat() for d in dates]
    humans = [d.strftime("%a %b %d %Y") for d in dates]
    human2iso = dict(zip(humans, isos))
    resolved: dict = {iso: None for iso in isos}
    last = None
    with sync_playwright() as pw:
        # 4 attempts: each launches a FRESH sticky session = a fresh residential
        # exit IP, so a course flagged/challenged on one IP or dropped by a flaky
        # tunnel gets more independent tries. Residential exits are slow and
        # sometimes drop mid-connect (ERR_TUNNEL_CONNECTION_FAILED /
        # ERR_CONNECTION_CLOSED), so a dead attempt costs a retry, not the course.
        for attempt in range(4):
            sid = f"{course['slug']}-{attempt}"
            browser = pw.chromium.launch(**_proxy_launch_kwargs(sid))
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                # Residential bandwidth is METERED and full SPA page-loads are
                # heavy. cps.golf's Cloudflare managed challenge needs only HTML +
                # JS to clear, and the reservation API is JSON; images / media /
                # fonts are pure waste on the metered link. Abort them so the
                # plan's bandwidth lasts ~an order of magnitude longer. (Scripts,
                # XHR/fetch, document and stylesheets are kept — the challenge JS
                # needs them.) CPS_KEEP_ASSETS=1 disables this if a tenant breaks.
                if os.environ.get("CPS_KEEP_ASSETS") != "1":
                    def _block_heavy(route):
                        try:
                            if route.request.resource_type in ("image", "media", "font"):
                                route.abort()
                            else:
                                route.continue_()
                        except Exception:  # noqa: BLE001
                            pass
                    page.route("**/*", _block_heavy)
                # 45s: a residential exit is much slower than the datacenter path.
                page.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                          wait_until="domcontentloaded", timeout=45000)
                # Wait for Cloudflare's managed challenge to ACTUALLY clear, not a
                # fixed sleep. cps.golf sets `cf_clearance` only once the JS
                # challenge completes; before that TeeTimes 403s. Poll up to ~24s
                # for the cookie, then a short settle. Best-effort (not a hard
                # gate) — some tenants aren't challenged at all.
                for _ in range(12):
                    page.wait_for_timeout(2000)
                    try:
                        cookies = page.context.cookies()
                    except Exception:  # noqa: BLE001
                        cookies = []
                    if any(c.get("name") == "cf_clearance" for c in cookies):
                        break
                page.wait_for_timeout(1500)
                r = page.evaluate(FLOW_JS, [tenant, wid, cids, humans])
                last = f"{r.get('stage')} {r.get('status')}" + (
                    " parse_failed" if r.get("parse_failed") else "")
                results = r.get("results") or []
                any_ok = False
                for item in results:
                    iso = human2iso.get(item.get("date"))
                    if iso is None:
                        continue
                    if item.get("status") == 200 and not item.get("parse_failed"):
                        resolved[iso] = {"ok": True,
                                         "tts": _teetimes(course, item.get("content") or [])}
                        any_ok = True
                    elif resolved[iso] is None or not resolved[iso].get("ok"):
                        # Keep a prior attempt's success; otherwise record the miss.
                        pf = " parse_failed" if item.get("parse_failed") else ""
                        resolved[iso] = {"ok": False,
                                         "error": f"teetimes {item.get('status')}{pf}"}
                # Accept once the cleared session produced at least one date and
                # every date is resolved; a per-date 403 among successes is not
                # worth another full page-load. If NO date cleared, fall through
                # to a fresh-IP retry (the old per-date retry behaviour).
                if any_ok and all(resolved[i] is not None for i in isos):
                    break
                # A wrong/dead tenant token endpoint 404s (or 401s) identically on
                # every attempt — retrying just burns another goto + challenge
                # wait. Stop now; the fix is the registry subdomain, not a retry.
                if r.get("stage") == "token" and r.get("status") in (401, 404):
                    break
            except Exception as e:  # noqa: BLE001
                # Include the message, not just the type: ocean-city's two tenants
                # fail with a bare "AttributeError" whose cause the type name hides.
                last = f"{type(e).__name__}: {e}"[:120]
            finally:
                browser.close()
            time.sleep(2 * (attempt + 1))    # brief backoff before a fresh try
    for iso in isos:
        if resolved[iso] is None:
            resolved[iso] = {"ok": False, "error": f"browser {last}"}
    return {"slug": course["slug"], "dates": resolved}


# ===========================================================================
# PLAIN-HTTP DATACENTER-DIRECT path for the cps tenants whose Cloudflare does
# NOT challenge a datacenter IP (measured 2026-08-06, probe run 31057485748,
# claude/cps-datacenter-probe.md: 14/36 tenants returned real tee times over
# plain `requests` straight from the runner, no proxy, no browser). Those go
# through this path — fast, free, and it revives tenants that were broken on the
# browser+residential path (highlands-ridge, southern-dunes, musket-ridge, ...).
# The other tenants stay on the browser path (Cloudflare challenges them; they
# need a real browser and, for the hard ones, residential).
#
# NO proxy here on purpose: cps Cloudflare clearance is IP-bound and low volume
# (17 courses), so a keep-alive Session per course (one runner IP for the whole
# 5-request flow) is exactly right; rotation would break it. trust_env=False so
# no ambient HTTP(S)_PROXY/dead residential secret can leak in.
#
# To pull a tenant back to the browser path, just remove it from this set.
# ===========================================================================
_DC_DIRECT_TENANTS = frozenset({
    "emeraldgreens", "greenvalleyranch", "haymakerco", "indianpeaks",
    "cityofwestminster", "redhawkridge", "dellago",
    "stjamesbay", "wakullasandsfl", "southerndunes", "tanglewoodfl",
    "musketridgemd", "oceancitygc",
})
# HELD BACK: "highlandsridgefl". Its North and South are two SEPARATE registry
# courses sharing this ONE tenant, and both are UNPINNED (no course_ids), so the
# flow returns the combined sheet and _teetimes would publish it under BOTH
# slugs — duplication. Every other shared tenant here (oceancitygc,
# cityofwestminster) pins course_ids per course, so it filters correctly.
# FOLLOW-UP to move highlands-ridge to plain: pin the N/S course_ids in the
# registry (discoverable from GetAllOptions.courseOptions), then add it back.
_API = "/onlineres/onlineapi/api/v1/onlinereservation"
_ZG = "00000000-0000-0000-0000-000000000000"


def _plain_headers(w: str, token: str, apikey: str) -> dict:
    h = {"client-id": "onlineresweb", "x-terminalid": "3", "x-websiteid": w or _ZG,
         "x-ismobile": "false", "x-productid": "1", "x-componentid": "1",
         "x-siteid": "1", "x-moduleid": "7", "x-timezoneid": "America/Denver",
         "x-timezone-offset": "360", "x-requestid": str(uuid.uuid4()),
         "Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        h["Authorization"] = "Bearer " + token
    if apikey:
        h["x-apiKey"] = apikey
    return h


def _plain_flow(session, tenant: str, wid: str, cids: str, date_str: str) -> dict:
    """FLOW_JS ported to `requests` (datacenter-direct). Same return shape as
    FLOW_JS so _teetimes/caller handling is identical: {status, stage, content,
    parse_failed?}. Uses pinned wid/cids when present, else discovers them."""
    base = f"https://{tenant}.cps.golf"
    api = base + _API
    token = apikey = ""
    try:
        cfg = session.get(base + "/onlineresweb/Home/Configuration", timeout=25)
        if cfg.ok:
            j = cfg.json()
            if isinstance(j, dict):
                apikey = j.get("apiKey") or ""
                if not wid and j.get("websiteId") and j["websiteId"] != _ZG:
                    wid = j["websiteId"]
    except Exception:  # noqa: BLE001
        pass
    tstatus = -1
    try:
        tr = session.post(base + "/identityapi/myconnect/token/short",
                          data="client_id=onlinereswebshortlived",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          timeout=25)
        tstatus = tr.status_code
        if tr.status_code == 200:
            try:
                token = tr.json().get("access_token") or ""
            except ValueError:
                pass
    except Exception:  # noqa: BLE001
        pass
    if not token and not apikey:
        return {"status": tstatus, "stage": "token", "content": []}
    if not wid or not cids:
        try:
            dr = session.get(api + "/GetAllOptions/" + tenant,
                             headers=_plain_headers(_ZG, token, apikey), timeout=25)
            db = dr.json()
        except ValueError:
            return {"status": dr.status_code, "stage": "discover",
                    "parse_failed": True, "content": []}
        except Exception:  # noqa: BLE001
            return {"status": -1, "stage": "discover", "content": []}
        if not wid:
            wid = db.get("webSiteId") or (db.get("reservationOptions") or {}).get("webSiteId") or ""
        if not cids:
            ids = [c.get("courseId") if c.get("courseId") is not None else c.get("id")
                   for c in (db.get("courseOptions") or [])]
            cids = ",".join(str(x) for x in ids if x is not None)
    if not wid or not cids:
        return {"status": 200, "stage": "discover", "content": [], "note": "no wid/cids"}
    txid = str(uuid.uuid4())
    hr = _plain_headers(wid, token, apikey)
    hr["Content-Type"] = "application/json"
    session.post(api + "/RegisterTransactionId", headers=hr,
                 json={"transactionId": txid}, timeout=25)
    url = (f"{api}/TeeTimes?searchDate={urllib.parse.quote(date_str)}&holes=0"
           f"&numberOfPlayer=0&courseIds={cids}&searchTimeType=0&transactionId={txid}"
           f"&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true"
           f"&teeSheetSearchView=5&classCode=R&defaultOnlineRate=N"
           f"&isUseCapacityPricing=false&memberStoreId=1&searchType=1")
    tt = session.get(url, headers=_plain_headers(wid, token, apikey), timeout=25)
    try:
        content = (tt.json() or {}).get("content") or []
    except ValueError:
        return {"status": tt.status_code, "stage": "teetimes",
                "parse_failed": True, "content": []}
    return {"status": tt.status_code, "stage": "teetimes", "content": content}


def _scrape_one_course_plain(course: dict, dates: list) -> dict:
    """Plain-HTTP twin of _scrape_one_course for datacenter-direct tenants, for
    ALL `dates`. Same multi-date contract: {"slug", "dates": {iso: {...}}}. Plain
    is free (no proxy, no metered bandwidth), so each date runs its own short
    token->TeeTimes flow over a keep-alive Session (sticky runner IP)."""
    ids = course["ids"]
    tenant = ids["tenant"]
    wid = ids.get("website_id") or ""
    cids = ",".join(str(x) for x in (ids.get("course_ids") or []))
    resolved: dict = {}
    for d in dates:
        iso = d.isoformat()
        date_str = d.strftime("%a %b %d %Y")
        last = None
        got = None
        for attempt in range(2):
            s = requests.Session()
            s.trust_env = False           # ignore any ambient/dead proxy env
            s.headers.update({"User-Agent": USER_AGENT})
            try:
                r = _plain_flow(s, tenant, wid, cids, date_str)
                last = f"{r.get('stage')} {r.get('status')}" + (
                    " parse_failed" if r.get("parse_failed") else "")
                if r.get("status") == 200 and not r.get("parse_failed"):
                    got = {"ok": True, "tts": _teetimes(course, r.get("content") or [])}
                    break
                if r.get("stage") == "token" and r.get("status") in (401, 404):
                    break
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"[:120]
            finally:
                s.close()
            time.sleep(1.5 * (attempt + 1))
        resolved[iso] = got or {"ok": False, "error": f"plain {last}"}
    return {"slug": course["slug"], "dates": resolved}


def _select_courses(registry: list, shard: str | None) -> list:
    # course_ids is NO LONGER required — FLOW_JS discovers wid+cids in-browser
    # via GetAllOptions when they aren't pinned, so the ~14 unpinned cps tenants
    # (emerald-greens etc.) that used to be silently skipped are now scraped.
    courses = [c for c in registry
               if c["platform"] == "clubprophet" and c["ids"].get("tenant")]
    # CPS_ONLY selects which tenant set to scrape so the two cost profiles can run
    # on DIFFERENT cadences (see scrape-cps-browser.yml): the plain-direct set is
    # free (plain HTTP, no proxy, no bandwidth) and can run FREQUENTLY, while the
    # Cloudflare-challenged set goes through the METERED residential proxy and must
    # run sparingly.  "plain" → only _DC_DIRECT_TENANTS; "browser" → only the rest;
    # unset/anything else → both (original behaviour).
    _only = os.environ.get("CPS_ONLY", "").strip().lower()
    if _only == "plain":
        courses = [c for c in courses if c["ids"].get("tenant") in _DC_DIRECT_TENANTS]
    elif _only == "browser":
        courses = [c for c in courses if c["ids"].get("tenant") not in _DC_DIRECT_TENANTS]
    # CPS_TENANTS: optional comma-separated tenant allowlist for isolated probes —
    # scrape ONLY these tenants (e.g. verify a specific never-captured tenant
    # without running the whole challenged set and its residential bandwidth).
    # Composes with CPS_ONLY as an intersection. Matched against ids.tenant,
    # case-insensitive. Unset = no restriction (original behaviour).
    _tenants = os.environ.get("CPS_TENANTS", "").strip()
    if _tenants:
        _keep = {t.strip().lower() for t in _tenants.split(",") if t.strip()}
        courses = [c for c in courses
                   if (c["ids"].get("tenant") or "").lower() in _keep]
    return apply_shard(courses, shard)


def run(dates, registry_path: str, out_paths_by_iso: dict,
        shard: str | None = None) -> list:
    """Scrape every selected cps tenant for ALL `dates` in ONE session per tenant,
    then emit ONE aggregate doc PER date (d1.sync's contract is single-date:
    per-date deactivation + per-(course,date) freshness stamping). `dates` is a
    list[dt.date]; `out_paths_by_iso` maps each date's ISO string to its output
    file. Returns the list of written paths.

    Batching the dates into one browser session (not one process/browser per
    date, as the workflow used to loop) is the core residential-cost cut: the
    metered Cloudflare page-load happens once per tenant instead of once per
    (tenant, date) — near 3->1, deep 31->1.
    """
    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = _select_courses(registry, shard)

    # CONCURRENCY. Each tenant is an independent flow against its own cps.golf
    # host, so we fan out over a thread pool — each worker owns its own
    # sync_playwright (see _scrape_one_course). The work is almost all network +
    # the challenge wait, so the 2-core runner is not the bottleneck. Default 6.
    conc = max(1, int(os.environ.get("CPS_CONCURRENCY", "6")))
    n_plain = sum(1 for c in courses if c["ids"].get("tenant") in _DC_DIRECT_TENANTS)
    dlist = list(dates)
    log.info("cps: %d tenants x %d dates (%d concurrent) — %d plain-direct, %d browser",
             len(courses), len(dlist), conc, n_plain, len(courses) - n_plain)

    # Split by tenant: the datacenter-direct set goes over plain HTTP (no proxy,
    # no browser); the rest keep the browser path (Cloudflare challenge + the
    # residential TEEITUP_PROXY, unset = direct). Both scrape ALL dates per call.
    per_course: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(
                    _scrape_one_course_plain
                    if c["ids"].get("tenant") in _DC_DIRECT_TENANTS
                    else _scrape_one_course,
                    c, dlist)
                for c in courses]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            per_course[res["slug"]] = res["dates"]
            ok_n = sum(1 for v in res["dates"].values() if v.get("ok"))
            log.info("  %-34s %d/%d dates ok", res["slug"], ok_n, len(dlist))

    # One aggregate doc per date, so scraper.d1 push reconciles each date cleanly.
    written = []
    for d in dlist:
        iso = d.isoformat()
        tee_times, errors = [], []
        for c in courses:
            entry = per_course.get(c["slug"], {}).get(iso)
            if entry and entry.get("ok"):
                tee_times.extend(entry["tts"])
            else:
                errors.append({"course": c["slug"], "platform": "clubprophet",
                               "error": (entry or {}).get("error", "no result")})
        doc = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": iso,
            "courses_queried": len(courses),
            "courses_ok": len(courses) - len(errors),
            "tee_times": [t.to_dict() for t in tee_times],
            "errors": errors,
        }
        out = pathlib.Path(out_paths_by_iso[iso])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2))
        written.append(str(out))
        log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based cps.golf fetcher")
    # Single date (back-compat: writes ONE doc to --out).
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--out", default="output/cps.json")
    # Multi-date (preferred): ONE browser session per tenant covers all dates;
    # writes output/<out-prefix>_<iso>.json per date. Cuts residential page-loads.
    p.add_argument("--dates", help="comma-separated ISO dates (YYYY-MM-DD)")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--out-prefix", default="cps")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if a.dates:
        dates = [dt.date.fromisoformat(s.strip()) for s in a.dates.split(",") if s.strip()]
        out_by_iso = {d.isoformat(): f"{a.out_dir}/{a.out_prefix}_{d.isoformat()}.json"
                      for d in dates}
    else:
        d = dt.date.fromisoformat(a.date)
        dates = [d]
        out_by_iso = {d.isoformat(): a.out}
    run(dates, a.registry, out_by_iso, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
