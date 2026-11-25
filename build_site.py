#!/usr/bin/env python3
"""Builds the KubeChaos Advisor website (site/index.html) from advisor_data.json.
   python3 build_site.py                      # use the bundled numbers
   python3 build_site.py results/all.json     # put your newest KubeChaos results into the site first
The output is one self-contained HTML file: host it on GitHub Pages or just open it."""
import json, os, sys
import advisor

HERE = os.path.dirname(os.path.abspath(__file__))
data = advisor.load_data(sys.argv[1] if len(sys.argv) > 1 else None)
if len(sys.argv) > 1:                        # keep the new numbers for the CLI too
    data["version"] = sys.argv[2] if len(sys.argv) > 2 else data["version"]
    with open(advisor.DATA_FILE, "w") as f:
        json.dump(data, f, indent=1)
body = open(os.path.join(HERE, "site", "site_template.html")).read()
body = body.replace("/*DATA*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style>\n'
        + body.replace("<header", "</head>\n<body>\n<header", 1) + "\n</body>\n</html>\n")
page = "\n".join(l for l in page.split("\n") if "viewport-fit-sides" not in l)
open(os.path.join(HERE, "site", "index.html"), "w").write(page)
open(os.path.join(HERE, "site", "artifact_body.html"), "w").write(body)
print("Built site/index.html")
