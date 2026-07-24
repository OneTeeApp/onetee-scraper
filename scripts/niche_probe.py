"""Is the SuperSaaS day-view server-rendered (plain-scrapable) or JS-only?"""
import datetime as dt, re, sys
import requests
sys.path.insert(0, ".")
from scraper.adapters.base import USER_AGENT
D = dt.date.today() + dt.timedelta(days=1)
s = requests.Session(); s.headers["User-Agent"] = USER_AGENT
url = "https://www.supersaas.com/schedule/Terry's_Golf/SAGC_TEE_TIMES"
r = s.get(url, params={"year": D.year, "month": D.month, "day": D.day, "view": "day"}, timeout=25)
html = r.text
TIME = re.compile(r"\d?\d:\d\d\s*[ap]m", re.I)
ntimes = len(TIME.findall(html))
nchip = html.count("chip")
nnr = len(re.findall(r"class=[^>]*\bnr\b", html))
print("status=%s len=%s" % (r.status_code, len(html)), flush=True)
print("chip=%d nr=%d times=%d json_blobs=%d" % (nchip, nnr, ntimes, html.count("application/json")), flush=True)
m = TIME.search(html)
if m:
    chunk = html[max(0, m.start()-250):m.start()+250].replace("\n", " ")
    print("HTML around first time:", chunk, flush=True)
else:
    print("NO time strings in plain HTML -> JS-rendered (needs browser)", flush=True)
for pat in (r'data-url="([^"]+)"', r'(/schedule/[^"\']*\.json[^"\']*)', r'src="([^"]*supersaas[^"]*\.js)"'):
    hits = re.findall(pat, html)
    if hits:
        print(" pat %s -> %s" % (pat, hits[:3]), flush=True)
