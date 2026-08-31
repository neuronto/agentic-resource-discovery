#!/usr/bin/env python3
"""Build the guide pages.

We rank for our own name and nothing else, because we have no page that answers
the questions a publisher actually types. This writes those pages: one per real
question, with the question as the H1, the answer in the first paragraph, and
the detail below it.

That order matters more than any markup. An answer engine quotes the sentence
that answers the query; if the answer is three scrolls down under a marketing
headline there is nothing to quote.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BASE = "https://neuronto.com"

SHELL_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{base}{path}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="ard" href="{base}/.well-known/ard.json">
<link rel="ai-catalog" href="{base}/.well-known/ai-catalog.json">
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{base}{path}">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap">
<script type="application/ld+json">{schema}</script>
{style}
</head>
<body>
<nav><div class="wrap">
  <a class="brand" href="/"><img src="/favicon.svg" width="24" height="24" alt="" aria-hidden="true">
    <span class="wm">neuronto</span></a>
  <div class="navlinks"><a href="/what-is-ard">What is ARD</a><a href="/publish">Publish</a>
    <a href="/console">Console</a><a href="/registries">Registries</a></div>
  <a class="btn btn--w nav-cta" href="/console" style="margin-left:auto">Free audit</a>
</div></nav>
<div class="wrap"><article class="doc">
"""

SHELL_FOOT = """</article></div>
<footer><div class="wrap">
  <div class="fl"><a href="/">Index</a><a href="/what-is-ard">What is ARD</a>
    <a href="/publish">Publish</a><a href="/console">Console</a>
    <a href="/registries">Registries</a><a href="/llms.txt">llms.txt</a>
    <a href="https://github.com/neuronto/neuronto">source</a></div>
  <div>An Agentic Resource Discovery registry, index and publisher.</div>
</div></footer>
</body></html>
"""

EXTRA_CSS = """<style>
.doc{max-width:74ch;padding:64px 0 40px}
.doc .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);margin:0 0 18px;display:flex;align-items:center;gap:10px}
.doc .eyebrow::before{content:"";width:22px;height:1px;background:var(--line2)}
.doc h1{font-family:var(--serif);font-weight:400;font-size:clamp(2.2rem,5vw,3.3rem);line-height:1.06;
  letter-spacing:-.018em;margin:0 0 20px}
.doc .answer{font-size:18px;line-height:1.62;color:var(--fg);font-weight:300;margin:0 0 30px;
  padding-left:18px;border-left:2px solid var(--line2)}
.doc .answer b{font-weight:500}
.doc h2{font-family:var(--serif);font-weight:400;font-size:1.7rem;letter-spacing:-.012em;
  margin:44px 0 14px;padding-top:22px;border-top:1px solid var(--line)}
.doc h3{font-size:14.5px;font-weight:600;margin:26px 0 7px}
.doc p{color:var(--fg2);font-weight:300;font-size:14.8px;line-height:1.68;margin:0 0 1.05rem}
.doc b,.doc strong{color:var(--fg);font-weight:500}
.doc ul,.doc ol{color:var(--fg2);font-weight:300;font-size:14.8px;padding-left:20px;margin:0 0 1.05rem}
.doc li{margin-bottom:.5rem}
.doc pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:15px 17px;
  overflow-x:auto;font-family:var(--mono);font-size:12.3px;line-height:1.72;margin:0 0 1.2rem;color:var(--fg2)}
.doc pre b{color:var(--fg);font-weight:400}
.doc table{border-collapse:collapse;width:100%;font-size:13.5px;margin:0 0 1.2rem}
.doc th{font-size:11.5px;font-weight:400;color:var(--mut);text-align:left;padding:0 12px 10px;
  border-bottom:1px solid var(--line);font-family:var(--mono);letter-spacing:.05em;text-transform:uppercase}
.doc td{padding:11px 12px;border-bottom:1px solid var(--line);color:var(--fg2)}
.doc tr:last-child td{border-bottom:0}
.doc .cta{border:1px solid var(--line2);border-radius:8px;padding:20px 22px;margin:34px 0 0;
  background:var(--panel);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.doc .cta p{margin:0;flex:1;min-width:220px}
.note{border-left:2px solid var(--line2);padding:2px 0 2px 16px;margin:0 0 1.2rem;color:var(--mut);font-size:13.5px}
</style>"""


def faq_schema(path, title, desc, qas):
    g = [{"@type": "WebPage", "@id": f"{BASE}{path}", "url": f"{BASE}{path}",
          "name": title, "description": desc,
          "isPartOf": {"@id": f"{BASE}/#site"}}]
    if qas:
        g.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in qas]})
    return json.dumps({"@context": "https://schema.org", "@graph": g}, ensure_ascii=False)


def build(page: dict, style: str) -> Path:
    html = SHELL_HEAD.format(
        title=page["title"], desc=page["desc"], base=BASE, path=page["path"],
        schema=faq_schema(page["path"], page["title"], page["desc"], page.get("faq", [])),
        style=style + EXTRA_CSS)
    html += (f'<p class="eyebrow">{page["eyebrow"]}</p>\n'
             f'<h1>{page["h1"]}</h1>\n'
             f'<p class="answer">{page["answer"]}</p>\n')
    html += page["body"]
    html += """<div class="cta"><p><b>Check your own domain.</b> The console fetches what you
      publish and asks every public registry whether they return you.</p>
      <a class="btn btn--w" href="/console">Run a free audit</a></div>"""
    html += SHELL_FOOT
    out = WEB / "pages" / (page["slug"] + ".html")
    out.write_text(html, encoding="utf-8")
    return out


def main():
    idx = (WEB / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>.*?</style>", idx, re.S).group(0)
    pages = json.loads((Path(__file__).parent / "pages.json").read_text(encoding="utf-8"))
    for p in pages:
        print("  wrote", build(p, style).name)
    print(f"{len(pages)} pages built")


if __name__ == "__main__":
    main()
