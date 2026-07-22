"""Embed the latest aggregated tee-time JSON into the demo page.

Usage: python build_demo.py [--data output/tee_times.json] [--out demo/index.html]
"""
import argparse
import json
import pathlib

p = argparse.ArgumentParser()
p.add_argument("--data", default="output/tee_times.json")
p.add_argument("--template", default="demo/template.html")
p.add_argument("--out", default="demo/index.html")
a = p.parse_args()

data = json.loads(pathlib.Path(a.data).read_text())
html = pathlib.Path(a.template).read_text()
marker = "/*__DATA__*/null"
assert marker in html, "template marker missing"
out = pathlib.Path(a.out)
out.write_text(html.replace(marker, json.dumps(data, separators=(",", ":"))))
print(f"wrote {out} ({out.stat().st_size/1024:.0f} KB, "
      f"{len(data['tee_times'])} tee times, simulated={data.get('simulated', False)})")
