// Throwaway probe: does Cloudflare Worker EDGE egress clear cps.golf's Cloudflare
// where GitHub datacenter IPs get 403? Read-only. Hits each challenged tenant's
// Configuration endpoint (first request in the reservation flow) and reports the
// status + cf-ray + whether an apiKey came back (i.e. real config, not a block
// page). If status 200 / hasApiKey for the challenged tenants, Cloudflare-edge
// egress is a FREE, scalable way to scrape cps — no proxy at all.
const SUBS = [
  "fossiltrace","flatironsgolf","gypsumcreekgolf","indiantree","marianabutte",
  "oldecourseloveland","universityofdenver","sewailo","cattailcreek",
  "eagleslanding","manowar","waradmiral","lighthousesound","nutterscrossing",
  "rumpointe","stonebrookfl","oldtrailgc","westfields","williamsburgnatgc",
  "glenmoor",
  // a couple known-free tenants as a control:
  "indianpeaks","emeraldgreens"
];
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

export default {
  async fetch(request) {
    const out = [];
    for (const sub of SUBS) {
      const url = `https://${sub}.cps.golf/onlineresweb/Home/Configuration`;
      try {
        const r = await fetch(url, {
          headers: { "User-Agent": UA, "Accept": "application/json, text/plain, */*" },
          cf: { cacheTtl: 0, cacheEverything: false },
        });
        const body = await r.text();
        const low = body.slice(0, 500).toLowerCase();
        const challenged = r.status === 403 || r.status === 503 ||
          low.includes("just a moment") || low.includes("attention required") ||
          low.includes("challenge-platform");
        out.push({
          sub, status: r.status, cfray: r.headers.get("cf-ray") || "",
          len: body.length, hasApiKey: body.includes("apiKey"),
          challenged,
        });
      } catch (e) {
        out.push({ sub, err: String(e && e.message || e).slice(0, 100) });
      }
    }
    const ok = out.filter(o => o.hasApiKey).length;
    return new Response(JSON.stringify({ edge: "cloudflare-worker", ok_with_apikey: ok, results: out }, null, 1),
      { headers: { "content-type": "application/json" } });
  }
};
