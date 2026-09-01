"""Shared page chrome for server-rendered pages.

Every page here is rendered on the server, in full, with its numbers already in
the HTML. That is not a preference. The homepage was once drawn client side and
an external reviewer reading it without JavaScript concluded the index was
empty, which is precisely the audience these pages exist for: crawlers and
answer engines deciding whether this index is worth citing.

The stylesheet is lifted from `web/index.html` rather than duplicated, so a
change to the design system reaches every generated page and there is no second
copy to drift.
"""
from __future__ import annotations

import html
import re
import time
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

_css: str | None = None
_cache: dict[str, tuple[float, str]] = {}


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def css() -> str:
    """The site stylesheet, read once from the homepage."""
    global _css
    if _css is None:
        try:
            src = (WEB / "index.html").read_text(encoding="utf-8")
            m = re.search(r"<style>(.*?)</style>", src, re.S)
            _css = m.group(1) if m else ""
        except Exception:
            _css = ""
    return _css


NAV = """<nav><div class="wrap">
  <a class="brand" href="/">
    <img src="/favicon.svg" width="24" height="24" alt="" aria-hidden="true">
    <span class="wm">neuronto</span>
  </a>
  <div class="navlinks"><a href="/what-is-ard">What is ARD</a><a href="/tools/">Tools</a><a href="/ard-publishers">Publishers</a><a href="/bench">Benchmark</a><a href="/publish">Publish</a><a href="/console">Console</a><a href="/blog">Blog</a></div>
  <a class="btn btn--w nav-cta" href="/console" style="margin-left:auto">Free audit</a>
</div></nav>"""

FOOTER = """<footer><div class="wrap">
  <div class="fl"><a href="/">Index</a><a href="/what-is-ard">What is ARD</a><a href="/tools/">Tools</a><a href="/ard-publishers">Publishers</a><a href="/bench">Benchmark</a><a href="/adoption">Adoption</a><a href="/publish">Publish</a><a href="/console">Console</a><a href="/registries">Registries</a><a href="/blog">Blog</a><a href="/.well-known/ard.json">ard.json</a><a href="/api-docs">API</a><a href="https://github.com/neuronto/agentic-resource-discovery">source</a></div>
  <div style="max-width:70ch">An Agentic Resource Discovery registry, index and publisher.
  Relevance scores are semantic only and are never a trust, compliance or safety rating.
  A tool listed as verified was read from that server's own tools/list; verification is a
  statement about reachability and capability, never about trustworthiness.</div>
</div></footer>"""

EXTRA_CSS = """
.pgh{padding:64px 0 28px;border-bottom:1px solid var(--line)}
.pgh h1{font-size:clamp(28px,4vw,44px);line-height:1.06;letter-spacing:-.02em;margin:0 0 14px}
.pgh .lede{color:var(--fg2);max-width:74ch;font-size:16px;line-height:1.6;margin:0}
.crumb{color:var(--mut);font-size:12px;margin-bottom:14px}
.crumb a{color:var(--mut)}.crumb a:hover{color:var(--fg2)}
.statline{display:flex;flex-wrap:wrap;gap:26px;margin:22px 0 0;padding:0;list-style:none}
.statline li{font-size:12px;color:var(--mut)}
.statline b{display:block;font-family:var(--mono);font-size:20px;color:var(--fg);
  font-weight:500;letter-spacing:-.01em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(258px,1fr));gap:12px;margin:26px 0}
.tile{border:1px solid var(--line);border-radius:var(--r);padding:16px;background:var(--panel);
  display:block;transition:border-color .15s}
.tile:hover{border-color:var(--line2)}
.tile .t{font-weight:500;margin-bottom:6px}
.tile .n{font-family:var(--mono);font-size:12px;color:var(--mut)}
table.tl{width:100%;border-collapse:collapse;margin:20px 0;font-size:13px}
table.tl th{text-align:left;font-weight:500;color:var(--mut);font-size:11px;
  text-transform:uppercase;letter-spacing:.06em;padding:0 12px 10px;border-bottom:1px solid var(--line)}
table.tl td{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:top}
table.tl td.num{text-align:right;font-family:var(--mono);white-space:nowrap}
.tn{font-family:var(--mono);color:var(--fg);font-size:12.5px}
.sv{color:var(--fg2);font-size:12px}
.dsc{color:var(--mut);font-size:12px;line-height:1.5;max-width:62ch;margin-top:4px}
.tag{display:inline-block;font-size:10.5px;font-family:var(--mono);padding:2px 6px;
  border:1px solid var(--line2);border-radius:4px;color:var(--mut);margin-left:6px}
.tag.ok{color:var(--pos);border-color:#1E3A2A}
.tag.auth{color:#E3B341;border-color:#3A3020}
.note{border:1px solid var(--line);border-left:2px solid var(--line2);border-radius:var(--r);
  padding:14px 16px;color:var(--fg2);font-size:13px;line-height:1.6;margin:22px 0;background:var(--panel)}
.mtable{width:100%;border-collapse:collapse;font-size:13px;margin:14px 0}
.mtable td{padding:7px 10px;border-bottom:1px solid var(--line)}
.mtable td.num{text-align:right;font-family:var(--mono);color:var(--fg2)}
.bar{height:5px;background:var(--line2);border-radius:3px;display:block}
"""


def page(title: str, description: str, body: str, canonical: str,
         jsonld: str = "") -> str:
    """One complete document."""
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<meta property="og:image" content="https://neuronto.com/img/discovery.jpg">
<meta property="og:site_name" content="Neuronto">
<meta name="twitter:image" content="https://neuronto.com/img/discovery.jpg">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="alternate" type="application/rss+xml" title="Neuronto verification events" href="https://neuronto.com/feed.xml">
<link rel="ard" href="https://neuronto.com/.well-known/ard.json">
<style>{css()}{EXTRA_CSS}</style>
{jsonld}
</head><body>
{NAV}
<div class="wrap">
{body}
</div>
{FOOTER}
</body></html>"""


def cached(key: str, ttl: int, build) -> str:
    """Serve a generated page from memory for `ttl` seconds.

    These pages aggregate tens of thousands of rows. Rebuilding one per request
    would spend the whole latency budget on work whose inputs change a few times
    a day.
    """
    now = time.time()
    got = _cache.get(key)
    if got and now - got[0] < ttl:
        return got[1]
    html_ = build()
    _cache[key] = (now, html_)
    return html_


def invalidate() -> None:
    _cache.clear()
