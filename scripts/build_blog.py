#!/usr/bin/env python3
"""Build the blog.

Distinct from the guide pages on purpose. The guides are reference, one
question, answered at the top, evergreen. These are analysis and walkthroughs
built on measurements we actually took, because original data is the thing an
answer engine has a reason to cite and a competitor cannot copy.
"""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BASE = "https://neuronto.com"

CSS = """<style>
.art{max-width:74ch;padding:52px 0 40px}
.art .kick{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mut);margin:0 0 16px;display:flex;align-items:center;gap:10px}
.art .kick::before{content:"";width:22px;height:1px;background:var(--line2)}
.art h1{font-family:var(--display);font-weight:600;font-size:clamp(2.1rem,4.9vw,3.15rem);
  line-height:1.07;letter-spacing:-.043em;margin:0 0 16px}
.art .meta{font-family:var(--mono);font-size:11.5px;color:var(--mut);margin:0 0 30px}
.art .hero{width:100%;aspect-ratio:1344/768;object-fit:cover;border-radius:10px;
  border:1px solid var(--line);display:block;margin:0 0 34px;background:#000}
.art .stand{font-size:18px;line-height:1.6;color:var(--fg);font-weight:300;margin:0 0 28px;
  padding-left:18px;border-left:2px solid var(--line2)}
.art .stand b{font-weight:500}
.art h2{font-family:var(--display);font-weight:600;font-size:1.48rem;letter-spacing:-.03em;
  margin:42px 0 13px;padding-top:22px;border-top:1px solid var(--line)}
.art h3{font-size:14.5px;font-weight:600;margin:26px 0 7px}
.art p{color:var(--fg2);font-weight:300;font-size:14.9px;line-height:1.7;margin:0 0 1.05rem}
.art b,.art strong{color:var(--fg);font-weight:500}
.art ul,.art ol{color:var(--fg2);font-weight:300;font-size:14.9px;padding-left:20px;margin:0 0 1.05rem}
.art li{margin-bottom:.5rem}
.art pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:15px 17px;
  overflow-x:auto;font-family:var(--mono);font-size:12.3px;line-height:1.72;margin:0 0 1.2rem;color:var(--fg2)}
.art pre b{color:var(--fg);font-weight:400}
.art table{border-collapse:collapse;width:100%;font-size:13.5px;margin:0 0 1.2rem}
.art th{font-size:11px;font-weight:400;color:var(--mut);text-align:left;padding:0 12px 10px;
  border-bottom:1px solid var(--line);font-family:var(--mono);letter-spacing:.05em;text-transform:uppercase}
.art td{padding:11px 12px;border-bottom:1px solid var(--line);color:var(--fg2)}
.art tr:last-child td{border-bottom:0}
.art .pull{font-family:var(--display);font-size:1.32rem;font-weight:500;letter-spacing:-.03em;
  color:var(--fg);line-height:1.32;margin:32px 0;padding:0 0 0 20px;border-left:2px solid var(--fg)}
.try{border:1px solid var(--line2);border-radius:10px;background:var(--panel);padding:22px 24px;margin:34px 0}
.try h4{font-family:var(--display);font-size:14.5px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em;color:var(--fg)}
.try p{margin:0 0 12px;font-size:13.8px}
.try pre{margin:0 0 12px}
.try .row{display:flex;gap:9px;flex-wrap:wrap}
.cards3{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:11px;margin:28px 0 0}
.pc{border:1px solid var(--line);border-radius:9px;padding:17px 18px;background:var(--panel);display:block}
.pc:hover{border-color:var(--line2)}
.pc .t{font-weight:500;font-size:13.8px;margin-bottom:4px;letter-spacing:-.01em}
.pc .d{color:var(--mut);font-size:12.5px;font-weight:300}
</style>"""

HEAD = """<!doctype html>
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
<meta property="og:image" content="{base}/img/{img}.jpg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{base}/img/{img}.jpg">
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
  <div class="navlinks"><a href="/what-is-ard">What is ARD</a><a href="/publish">Publish</a><a href="/console">Console</a><a href="/registries">Registries</a><a href="/blog">Blog</a></div>
  <a class="btn btn--w nav-cta" href="/console" style="margin-left:auto">Free audit</a>
</div></nav>
<div class="wrap"><article class="art">
"""

FOOT = """</article></div>
<footer><div class="wrap">
  <div class="fl"><a href="/">Index</a><a href="/what-is-ard">What is ARD</a><a href="/publish">Publish</a><a href="/console">Console</a><a href="/registries">Registries</a><a href="/blog">Blog</a><a href="/.well-known/ard.json">ard.json</a><a href="/llms.txt">llms.txt</a><a href="/api-docs">API</a><a href="https://github.com/neuronto/neuronto">source</a></div>
  <div>An Agentic Resource Discovery registry, index and publisher.</div>
</div></footer>
</body></html>
"""

# Repeated mid-article and closing block. Every article should leave the reader
# able to act without hunting for the API.
def try_block(heading, para):
    return f"""<div class="try">
<h4>{heading}</h4>
<p>{para}</p>
<pre>curl -s <b>https://neuronto.com/search</b> \\
  -H 'content-type: application/json' \\
  -d '{{"query":{{"text":"scrape a website"}},"federation":"auto"}}'</pre>
<pre>claude mcp add --transport http neuronto <b>https://neuronto.com/mcp</b></pre>
<div class="row"><a class="btn btn--w" href="/publish">How to publish</a>
<a class="btn btn--g" href="/console">Audit your domain</a></div>
</div>"""


def schema_for(a):
    g = [
        {"@type": "BlogPosting", "@id": f"{BASE}{a['path']}#post",
         "headline": a["title"], "description": a["desc"],
         "image": f"{BASE}/img/{a['img']}.jpg", "datePublished": a["date"],
         "dateModified": a["date"], "url": f"{BASE}{a['path']}",
         "author": {"@type": "Organization", "name": "Neuronto", "url": BASE},
         "publisher": {"@type": "Organization", "name": "Neuronto", "url": BASE,
                       "logo": f"{BASE}/favicon.svg"},
         "mainEntityOfPage": {"@type": "WebPage", "@id": f"{BASE}{a['path']}"}},
    ]
    if a.get("faq"):
        g.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": ans}} for q, ans in a["faq"]]})
    return json.dumps({"@context": "https://schema.org", "@graph": g}, ensure_ascii=False)


def build(a, style, others):
    html = HEAD.format(title=a["title"], desc=a["desc"], base=BASE, path=a["path"],
                       img=a["img"], schema=schema_for(a), style=style + CSS)
    html += (f'<p class="kick">{a["kicker"]}</p>\n<h1>{a["h1"]}</h1>\n'
             f'<p class="meta">{a["date_h"]} · {a["read"]} min read</p>\n'
             f'<img class="hero" src="/img/{a["img"]}.jpg" alt="{a["alt"]}" '
             f'width="1344" height="768">\n'
             f'<p class="stand">{a["stand"]}</p>\n')
    html += a["body"]
    html += try_block("Use Neuronto from your agent",
                      "One call searches this index and every other public ARD registry. "
                      "No key, no signup. Or install it as an MCP server and let the agent "
                      "search from the interface it already speaks.")
    rel = [o for o in others if o["slug"] != a["slug"]][:3]
    html += ('<h2>Keep reading</h2><div class="cards3">' + "".join(
        f'<a class="pc" href="{o["path"]}"><div class="t">{o["h1"]}</div>'
        f'<div class="d">{o["desc"][:88]}…</div></a>' for o in rel) + "</div>")
    html += FOOT
    out = WEB / "blog" / (a["slug"] + ".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def build_index(arts, style):
    html = HEAD.format(title="Neuronto Blog, Agentic Resource Discovery, measured",
                       desc="Analysis and walkthroughs on Agentic Resource Discovery, built on our own "
                            "measurements of every public ARD registry and the MCP ecosystem.",
                       base=BASE, path="/blog", img="discovery",
                       schema=json.dumps({"@context": "https://schema.org", "@type": "Blog",
                                          "name": "Neuronto Blog", "url": f"{BASE}/blog"}),
                       style=style + CSS)
    html += ('<p class="kick">Writing</p><h1>Agent discovery, measured</h1>'
             '<p class="stand">What we find when we actually test the agent web, and how to '
             'make your own resources findable in it.</p>')
    html += '<div class="cards3" style="margin-top:34px">' + "".join(
        f'<a class="pc" href="{a["path"]}"><div class="t">{a["h1"]}</div>'
        f'<div class="d">{a["desc"][:110]}…</div></a>' for a in arts) + "</div>"
    html += FOOT
    (WEB / "blog" / "index.html").write_text(html, encoding="utf-8")


def main():
    idx = (WEB / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>.*?</style>", idx, re.S).group(0)
    arts = json.loads((Path(__file__).parent / "articles.json").read_text(encoding="utf-8"))
    for a in arts:
        print("  wrote", build(a, style, arts).name)
    build_index(arts, style)
    print(f"  wrote index.html\n{len(arts)} articles + index")


if __name__ == "__main__":
    main()
