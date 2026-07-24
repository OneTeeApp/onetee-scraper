"""Discover websiteId + courseIds for the AZ cps.golf tenants so browser_cps
can fetch them (it needs course_ids pinned). Same GetAllOptions flow as CO."""
from __future__ import annotations
import json, sys
sys.path.insert(0, ".")
from scraper.aggregate import load_registry

UA=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CPS_JS=r"""
async ([tenant]) => {
  const base="https://"+tenant+".cps.golf";
  const tr=await fetch(base+"/identityapi/myconnect/token/short",
    {method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
     body:"client_id=onlinereswebshortlived"});
  if(tr.status!==200) return {stage:"token",status:tr.status};
  const token=(await tr.json()).access_token;
  const H={Authorization:"Bearer "+token,"client-id":"onlineresweb",
    "x-productid":"1","x-componentid":"1","x-siteid":"1","x-moduleid":"7",
    "x-terminalid":"3","Accept":"application/json"};
  const ar=await fetch(base+"/onlineres/onlineapi/api/v1/onlinereservation/GetAllOptions/"+tenant,{headers:H});
  if(ar.status!==200) return {stage:"getalloptions",status:ar.status};
  const j=await ar.json();
  return {stage:"ok",webSiteId:j.webSiteId||null,
    courseOptions:(j.courseOptions||[]).map(c=>({id:c.courseId??c.id,name:c.courseName??c.name}))};
}
"""
def main():
    from playwright.sync_api import sync_playwright
    reg=load_registry("registry.json")
    tenants=sorted({c["ids"]["tenant"] for c in reg
                    if c["platform"]=="clubprophet" and c.get("state")=="AZ"
                    and c["ids"].get("tenant")})
    print("AZ cps tenants:", tenants, flush=True)
    with sync_playwright() as pw:
        for t in tenants:
            b=pw.chromium.launch(args=["--no-sandbox"])
            try:
                pg=b.new_page(user_agent=UA)
                pg.goto(f"https://{t}.cps.golf/onlineresweb/search-teetime",
                        wait_until="domcontentloaded",timeout=35000)
                pg.wait_for_timeout(6000)
                print(f"RESULT cps {t}: {json.dumps(pg.evaluate(CPS_JS,[t]))[:700]}",flush=True)
            except Exception as e:
                print(f"RESULT cps {t}: ERROR {type(e).__name__} {str(e)[:70]}",flush=True)
            finally:
                b.close()
if __name__=="__main__": main()
