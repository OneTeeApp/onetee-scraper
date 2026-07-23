"""Diagnostic: does a REAL headless browser clear the cps.golf WAF from a
datacenter IP, where the plain Python HTTP client gets 403?

For each tenant it runs the same token -> register -> TeeTimes flow twice:
  (A) plain `requests` (what the scraper uses now)
  (B) inside a real headless Chromium via Playwright (browser TLS + origin)

If B succeeds where A fails, the WAF keys on the TLS/client fingerprint and a
browser-based fetcher unlocks these courses. If B also 403s, the block is the
datacenter IP range itself and these are genuinely residential-only.

Run in CI (see .github/workflows/cps-browser-test.yml); read the RESULT lines.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

import requests

# tenant -> (websiteId, courseIds csv)   [captured from each site's GetAllOptions]
TENANTS = {
    "flatironsgolf":  ("d0c1d3f9-28c7-4f79-8ee1-08d926a72623", "1"),
    "fossiltrace":    ("b6c22f3a-944a-46e9-020e-08da90168fb2", "1,2,3"),
    "marianabutte":   ("e0496558-918b-4f2d-44dc-08dbf84ad30b", "3"),
    "indianpeaks":    ("f04abbc1-368f-40f4-096d-08d89aea9574", "10,11"),  # control: works today
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _date() -> str:
    return (dt.date.today() + dt.timedelta(days=1)).strftime("%a %b %d %Y")


def _headers(token: str, wid: str) -> dict:
    return {
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": UA, "client-id": "onlineresweb", "x-terminalid": "3",
        "x-websiteid": wid, "x-ismobile": "false", "x-productid": "1",
        "x-componentid": "1", "x-siteid": "1", "x-moduleid": "7",
        "x-timezoneid": "America/Denver", "x-timezone-offset": "360",
        "x-requestid": str(uuid.uuid4()),
    }


def plain_requests(tenant: str, wid: str, cids: str) -> dict:
    base = f"https://{tenant}.cps.golf"
    api = f"{base}/onlineres/onlineapi/api/v1/onlinereservation"
    try:
        tr = requests.post(f"{base}/identityapi/myconnect/token/short",
                           data={"client_id": "onlinereswebshortlived"},
                           headers={"User-Agent": UA}, timeout=20)
        if tr.status_code != 200:
            return {"stage": "token", "status": tr.status_code}
        token = tr.json().get("access_token")
        txid = str(uuid.uuid4())
        requests.post(f"{api}/RegisterTransactionId", json={"transactionId": txid},
                     headers={**_headers(token, wid), "Content-Type": "application/json"},
                     timeout=20)
        params = {"searchDate": _date(), "holes": 0, "numberOfPlayer": 0,
                  "courseIds": cids, "searchTimeType": 0, "transactionId": txid,
                  "teeOffTimeMin": 0, "teeOffTimeMax": 23, "isChangeTeeOffTime": "true",
                  "teeSheetSearchView": 5, "classCode": "R", "defaultOnlineRate": "N",
                  "isUseCapacityPricing": "false", "memberStoreId": 1, "searchType": 1}
        tt = requests.get(f"{api}/TeeTimes", params=params,
                          headers=_headers(token, wid), timeout=20)
        n = len((tt.json() or {}).get("content", [])) if tt.status_code == 200 else None
        return {"stage": "teetimes", "status": tt.status_code, "count": n}
    except Exception as e:  # noqa: BLE001
        return {"stage": "exception", "error": f"{type(e).__name__}: {e}"[:80]}


BROWSER_JS = """
async ([tenant, wid, cids, date]) => {
  const base = "https://" + tenant + ".cps.golf";
  const api = base + "/onlineres/onlineapi/api/v1/onlinereservation";
  const H = () => ({Authorization:"Bearer "+token,"client-id":"onlineresweb",
    "x-terminalid":"3","x-websiteid":wid,"x-ismobile":"false","x-productid":"1",
    "x-componentid":"1","x-siteid":"1","x-moduleid":"7","x-timezoneid":"America/Denver",
    "x-timezone-offset":"360","x-requestid":crypto.randomUUID(),"Accept":"application/json"});
  let token;
  const tr = await fetch(base+"/identityapi/myconnect/token/short",
    {method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
     body:"client_id=onlinereswebshortlived"});
  if (tr.status !== 200) return {stage:"token", status:tr.status};
  token = (await tr.json()).access_token;
  const txid = crypto.randomUUID();
  await fetch(api+"/RegisterTransactionId",{method:"POST",
    headers:{...H(),"Content-Type":"application/json"},
    body:JSON.stringify({transactionId:txid})});
  const url = api+`/TeeTimes?searchDate=${encodeURIComponent(date)}&holes=0`
    +`&numberOfPlayer=0&courseIds=${cids}&searchTimeType=0&transactionId=${txid}`
    +`&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true&teeSheetSearchView=5`
    +`&classCode=R&defaultOnlineRate=N&isUseCapacityPricing=false&memberStoreId=1&searchType=1`;
  const tt = await fetch(url,{headers:H()});
  let n=null; try{ n=((await tt.json()).content||[]).length; }catch(e){}
  return {stage:"teetimes", status:tt.status, count:n};
}
"""


def browser(pw, tenant: str, wid: str, cids: str) -> dict:
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        try:
            pg.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                    wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            return {"stage": "goto", "error": f"{type(e).__name__}"[:40]}
        return pg.evaluate(BROWSER_JS, [tenant, wid, cids, _date()])
    finally:
        b.close()


def main() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for tenant, (wid, cids) in TENANTS.items():
            a = plain_requests(tenant, wid, cids)
            b = browser(pw, tenant, wid, cids)
            print(f"RESULT {tenant}: plain={json.dumps(a)}  browser={json.dumps(b)}",
                  flush=True)


if __name__ == "__main__":
    main()
