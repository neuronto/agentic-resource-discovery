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

import hashlib
import html
import os
import re
import sqlite3
import threading
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
  <div class="navlinks"><a href="/what-is-ard">What is ARD</a><a href="/tools/">Tools</a><a href="/ard-publishers">Publishers</a><a href="/bench">Benchmark</a><a href="/ard-registries">Registries</a><a href="/publish">Publish</a><a href="/submit">Submit</a><a href="/console">Console</a><a href="/blog">Blog</a></div>
  <a class="btn btn--w nav-cta" href="/console" style="margin-left:auto">Free audit</a>
</div></nav>"""

FOOTER = """<footer><div class="wrap">
  <div class="fl"><a href="/">Index</a><a href="/what-is-ard">What is ARD</a><a href="/tools/">Tools</a><a href="/ard-publishers">Publishers</a><a href="/bench">Benchmark</a><a href="/ard-registries">Registries</a><a href="/adoption">Adoption</a><a href="/publish">Publish</a><a href="/submit">Submit</a><a href="/console">Console</a><a href="/badge">Badge</a><a href="/ard-manifest-generator">Manifest generator</a><a href="/ard-conformance">Conformance</a><a href="/blog">Blog</a><a href="/.well-known/ard.json">ard.json</a><a href="/api-docs">API</a><a href="/published">Published</a><a href="https://github.com/neuronto/agentic-resource-discovery">source</a></div>
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


# A page reports its own view, because most pages are served from a CDN and
# never reach the server. It also reports what a server cannot see: dwell time
# and how far the reader got. No cookie, no identifier, and it does not run at
# all for a reader who has set Do Not Track or Global Privacy Control.
BEACON = """<script>
(function(){try{
 if(navigator.doNotTrack==="1"||navigator.msDoNotTrack==="1"||navigator.globalPrivacyControl)return;
 var t0=Date.now(),mx=0,done=false;
 function depth(){var h=document.documentElement.scrollHeight-innerHeight;
   return h>0?Math.min(100,Math.round(scrollY/h*100)):100;}
 addEventListener("scroll",function(){var d=depth();if(d>mx)mx=d;},{passive:true});
 function send(t,x){
   if(t==="end"){if(done)return;done=true;}
   var o={t:t,p:location.pathname,r:document.referrer||"",vw:innerWidth,vh:innerHeight,
          tz:(Intl.DateTimeFormat().resolvedOptions().timeZone||""),
          lang:(navigator.language||"").slice(0,5)};
   for(var k in x)o[k]=x[k];
   var s=JSON.stringify(o);
   if(navigator.sendBeacon){navigator.sendBeacon("/e",new Blob([s],{type:"application/json"}));}
   else{fetch("/e",{method:"POST",body:s,keepalive:true,headers:{"content-type":"application/json"}});}
 }
 addEventListener("load",function(){send("view",{lt:Math.round(performance.now())});});
 function bye(){send("end",{d:Math.round((Date.now()-t0)/1000),s:mx});}
 addEventListener("pagehide",bye);
 document.addEventListener("visibilitychange",function(){
   if(document.visibilityState==="hidden")bye();});
}catch(e){}})();
</script>"""


def page(title: str, description: str, body: str, canonical: str,
         jsonld: str = "") -> str:
    """One complete document.

    Every title carries the site name in the same form, so the phrase a person
    types to find a registry of this kind is present on every page, once, in
    the slot search engines and answer engines weight most. A caller that
    already wrote it is left alone.
    """
    if "Neuronto" not in title:
        title = title + " | Neuronto ARD Registry"
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
<meta property="og:image" content="https://neuronto.com/img/og-v1.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Neuronto ARD Registry, the Agentic Resource Discovery (ARD) index">
<meta property="og:site_name" content="Neuronto ARD Registry">
<meta name="twitter:image" content="https://neuronto.com/img/og-v1.png">
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
{BEACON}
</body></html>"""


# The page cache is on disk, shared by every worker, and survives a restart.
#
# It began as a dict per worker, and the failure was measured rather than
# theorised: the capability index took **22.4 seconds** to build and 2ms to
# serve, the service runs several workers so each cold miss happened once per
# worker, every deploy emptied all of them, and the reverse proxy gives up at 30
# seconds. Under crawl load that became a 504 on a page that is fast in the warm
# case, which is the worst possible shape of slow.
#
# Three properties fix it, and the third is the one that matters:
#   * shared, so one worker's work serves the others
#   * durable, so a restart does not send the next visitor to the back of a
#     22 second queue
#   * **stale while revalidate**, so an expired entry is served immediately and
#     rebuilt behind the request. After the very first build of a key, nobody
#     ever waits for a rebuild again.
_CACHE_DB = Path(os.getenv("NEURONTO_PAGECACHE_DB",
                           str(Path(os.getenv("NEURONTO_DB", "./data/neuronto.db")).parent
                               / "pagecache.db")))
# A durable page cache means a template change does not reach anybody until the
# cache is cleared, and forgetting is silent: the site keeps serving correct
# looking HTML built by the previous deploy. The beacon shipped and appeared on
# no cached page for exactly this reason. So the key carries a stamp derived
# from the code that renders the page; when that changes, every entry is a miss
# and rebuilds itself. No deploy step to remember.
def _build_stamp() -> str:
    h = hashlib.sha1()
    here = Path(__file__).resolve().parent
    for name in sorted(("render.py", "catalog.py", "badge.py")):
        f = here / name
        try:
            h.update(str(f.stat().st_mtime_ns).encode())
            h.update(str(f.stat().st_size).encode())
        except OSError:
            pass
    return h.hexdigest()[:8]


STAMP = _build_stamp()
_local = threading.local()
_building: set[str] = set()
_build_lock = threading.Lock()


def _cdb() -> sqlite3.Connection | None:
    """One connection per thread. SQLite objects are not shareable across them."""
    c = getattr(_local, "conn", None)
    if c is not None:
        return c
    try:
        _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(_CACHE_DB), timeout=5)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=OFF")      # a cache; losing it costs a rebuild
        c.execute("PRAGMA busy_timeout=4000")
        c.execute("""CREATE TABLE IF NOT EXISTS pages(
                       key TEXT PRIMARY KEY, built REAL NOT NULL, html TEXT NOT NULL
                     ) WITHOUT ROWID""")
        c.commit()
        _local.conn = c
        return c
    except Exception:
        return None


def _k(key: str) -> str:
    """The stored key, tied to the rendering code that produced it."""
    return f"{STAMP}:{key}"


def _read(key: str):
    key = _k(key)
    c = _cdb()
    if c is None:
        return _cache.get(key)
    try:
        r = c.execute("SELECT built, html FROM pages WHERE key=?", (key,)).fetchone()
        return (r[0], r[1]) if r else None
    except Exception:
        return _cache.get(key)


def _write(key: str, html_: str) -> None:
    key = _k(key)
    _cache[key] = (time.time(), html_)          # in-process copy, avoids a read per hit
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("INSERT INTO pages(key,built,html) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET built=excluded.built, html=excluded.html",
                  (key, time.time(), html_))
        # Entries from an earlier build of the rendering code can never be read
        # again, so they are dropped rather than left to grow the file forever.
        c.execute("DELETE FROM pages WHERE key NOT LIKE ? AND key != '__warmlock__'",
                  (STAMP + ":%",))
        c.commit()
    except Exception:
        pass


def _rebuild(key: str, build) -> None:
    try:
        _write(key, build())
    except Exception:
        pass
    finally:
        with _build_lock:
            _building.discard(key)


def cached(key: str, ttl: int, build) -> str:
    """Serve a generated page, rebuilding behind the request when it is stale.

    These pages aggregate tens of thousands of rows. Rebuilding one per request
    would spend the whole latency budget on work whose inputs change a few times
    a day, and rebuilding one *during* a request is how a fast page becomes a
    gateway timeout.
    """
    got = _cache.get(_k(key)) or _read(key)
    if got:
        if time.time() - got[0] >= ttl:
            # Stale: hand back what we have and refresh out of the way. One
            # rebuild per key at a time, or a burst of traffic on an expired
            # page starts a thread per request.
            with _build_lock:
                fresh = key not in _building
                if fresh:
                    _building.add(key)
            if fresh:
                threading.Thread(target=_rebuild, args=(key, build),
                                 name=f"rebuild:{key}", daemon=True).start()
        return got[1]
    # Nothing at all: this is the only path that waits, and warm_pages() exists
    # so that a visitor is not the one who pays for it.
    html_ = build()
    _write(key, html_)
    return html_


def claim_warm(holder: str, ttl: int = 300) -> bool:
    """Claim the right to warm the cache, across processes.

    Every worker starts at the same moment and each was warming the same 40-odd
    expensive pages independently. On a two core box that saturated both for a
    minute after every deploy, which is exactly when traffic arrives, and it was
    entirely duplicated work: the cache is shared, so one worker building it
    serves all of them. The claim lives in the cache itself, so it needs no
    extra coordination and expires on its own if a worker dies mid-warm.
    """
    c = _cdb()
    if c is None:
        return True                              # no shared store, warm locally
    now = time.time()
    try:
        r = c.execute("SELECT built, html FROM pages WHERE key='__warmlock__'").fetchone()
        if r and now - r[0] < ttl:
            return False                         # somebody else is warming
        c.execute("INSERT INTO pages(key,built,html) VALUES('__warmlock__',?,?) "
                  "ON CONFLICT(key) DO UPDATE SET built=excluded.built, html=excluded.html",
                  (now, holder))
        c.commit()
        return True
    except Exception:
        return True


def release_warm() -> None:
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("DELETE FROM pages WHERE key='__warmlock__'")
        c.commit()
    except Exception:
        pass


def warm(key: str, ttl: int, build) -> bool:
    """Build a page if it is missing or stale. Returns True if it built."""
    got = _read(key)
    if got and time.time() - got[0] < ttl:
        return False
    _write(key, build())
    return True


def invalidate() -> None:
    _cache.clear()
    c = _cdb()
    if c is None:
        return
    try:
        c.execute("DELETE FROM pages")
        c.commit()
    except Exception:
        pass


def cache_stats() -> dict:
    c = _cdb()
    out = {"backend": "sqlite" if c else "memory", "in_process": len(_cache)}
    if c:
        try:
            out["stored"] = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            out["bytes"] = c.execute("SELECT COALESCE(SUM(LENGTH(html)),0) FROM pages").fetchone()[0]
            out["oldest_s"] = int(time.time() - (c.execute(
                "SELECT MIN(built) FROM pages").fetchone()[0] or time.time()))
        except Exception:
            pass
    return out
