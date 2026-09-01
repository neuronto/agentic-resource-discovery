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



# The site outgrew a flat list. Nine top level links plus a search field and a
# call to action stopped fitting, and the first item wrapped onto three lines
# rather than the row scrolling or collapsing, so the header looked broken on a
# wide desktop. Four groups of three or four is the shape the pages actually
# have, and the descriptions do real work: a person scanning "Verify" learns
# what an audit is without opening it.
NAV_GROUPS = [
    ("Discover", [
        ("/tools/",          "Tools by capability", "25 pages of verified tools, grouped by what they do"),
        ("/ard-publishers",  "Publishers",          "Every domain verified to serve an ARD manifest"),
        ("/ard-registries",  "ARD registries",      "Every public registry, compared and measured"),
        ("/adoption",        "Adoption",            "Who publishes a manifest, and on which path"),
    ]),
    ("Publish", [
        ("/submit",                 "Submit to the index", "An MCP endpoint or a domain, verified before listing"),
        ("/publish",                "Publishing guide",    "Write a manifest and be found, in about ten minutes"),
        ("/ard-manifest-generator", "Manifest generator",  "Build one from what your domain already serves"),
        ("/badge",                  "Badge",               "Show what was verified about your resources"),
    ]),
    ("Verify", [
        ("/console",         "Free audit",     "Can agents discover your domain, and who outranks you"),
        ("/ard-conformance", "Conformance",    "The official test, and how to run it on anything"),
        ("/bench",           "ARD-Bench",      "Retrieval measured head to head across registries"),
    ]),
    ("Learn", [
        ("/what-is-ard", "What is ARD",  "The specification, in plain terms"),
        ("/blog",        "Writing",      "What we measured, and what it turned out to mean"),
        ("/api-docs",    "API",          "Every endpoint, with schemas"),
        ("/published",   "Where we are", "Source, dataset, packages and registry entries"),
    ]),
]


NAV_JS = """<script>
(function(){
 var b=document.querySelector('.burger'), m=document.querySelector('.mobmenu');
 if(b&&m){b.addEventListener('click',function(){
   var open=m.classList.toggle('open');
   b.setAttribute('aria-expanded',open?'true':'false');
   document.body.style.overflow=open?'hidden':'';
 });}
 // On a touch device hover never fires, so the trigger toggles its own panel.
 if(window.matchMedia('(hover: none)').matches){
   document.querySelectorAll('.ngt').forEach(function(t){
     t.addEventListener('click',function(e){
       e.preventDefault();
       var p=t.parentElement, was=p.classList.contains('on');
       document.querySelectorAll('.ng.on').forEach(function(x){x.classList.remove('on');
         x.querySelector('.ngt').setAttribute('aria-expanded','false');});
       if(!was){p.classList.add('on');t.setAttribute('aria-expanded','true');}
     });
   });
 }
 document.addEventListener('keydown',function(e){
   if(e.key!=='Escape')return;
   document.querySelectorAll('.ng.on').forEach(function(x){x.classList.remove('on');});
   if(m&&m.classList.contains('open')){m.classList.remove('open');
     b.setAttribute('aria-expanded','false');document.body.style.overflow='';}
   if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();
 });
})();
</script>"""


def _nav_groups_html() -> str:
    out = []
    for label, items in NAV_GROUPS:
        links = "".join(
            f'<a href="{href}"><span class="ni">{title}</span>'
            f'<span class="nd">{desc}</span></a>' for href, title, desc in items)
        out.append(
            f'<div class="ng"><button type="button" class="ngt" aria-expanded="false">'
            f'{label}<svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true">'
            f'<path d="M2 4l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg></button>'
            f'<div class="ngp"><div class="ngp-in">{links}</div></div></div>')
    return "".join(out)


def _footer_groups_html() -> str:
    out = []
    for label, items in NAV_GROUPS:
        links = "".join(f'<a href="{href}">{title}</a>' for href, title, _ in items)
        out.append(f'<div class="fg"><h4>{label}</h4>{links}</div>')
    return "".join(out)


def _nav_mobile_html() -> str:
    out = []
    for label, items in NAV_GROUPS:
        links = "".join(f'<a href="{href}">{title}</a>' for href, title, _ in items)
        out.append(f'<div class="ms"><h4>{label}</h4>{links}</div>')
    return "".join(out)


NAV = f"""<nav><div class="wrap">
  <a class="brand" href="/">
    <img src="/favicon.svg" width="24" height="24" alt="" aria-hidden="true">
    <span class="wm">neuronto</span>
  </a>
  <div class="navlinks">{_nav_groups_html()}</div>
  <form class="navsearch" action="/tools" method="get" role="search">
    <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5"
      fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M11 11l3.5 3.5"
      stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    <input id="navq" name="q" placeholder="Search tools" aria-label="Search tools"
           autocomplete="off" spellcheck="false">
  </form>
  <a class="btn btn--w nav-cta" href="/console">Free audit</a>
  <div class="navquick">
    <a href="/tools/">Tools</a><a href="/submit">Submit</a><a href="/console">Audit</a>
  </div>
  <button type="button" class="burger" aria-label="Menu" aria-expanded="false">
    <span></span><span></span></button>
</div>
<div class="mobmenu"><div class="wrap"><div class="msgrid">{_nav_mobile_html()}</div>
  <a class="btn btn--w" href="/console" style="margin-top:18px">Free audit</a>
</div></div></nav>"""

FOOTER = f"""<footer><div class="wrap">
  <div class="fgrid">
    {_footer_groups_html()}
    <div class="fg">
      <h4>Contact</h4>
      <a href="/published">Where we are published</a>
      <a href="https://github.com/neuronto/agentic-resource-discovery">Source</a>
      <a href="/.well-known/ard.json">ard.json</a>
      <a href="/llms.txt">llms.txt</a>
      <div class="femail"><span>Write to us</span>
        <img src="/img/contact.png" alt="Contact address for Neuronto"
             width="149" height="17" loading="lazy"></div>
    </div>
  </div>
  <div class="fnote">An Agentic Resource Discovery registry, index and publisher.
  Relevance scores are semantic only and are never a trust, compliance or safety rating.
  A tool listed as verified was read from that server's own tools/list; verification is a
  statement about reachability and capability, never about trustworthiness.</div>
</div></footer>"""

EXTRA_CSS = """


/* Rows far below the fold are not laid out until they are near it. A category
   page carries about sixty rows and the publisher index two hundred; this keeps
   first paint independent of the count. */
.tbl tbody tr,.mtable tbody tr{content-visibility:auto;contain-intrinsic-size:auto 44px}
img{max-width:100%;height:auto}


/* ── Buttons ────────────────────────────────────────────────────────────── */
/* Brushed metal rather than flat white. Three things do the work: a vertical
   gradient from white to a cool grey so the surface reads as curved, a one
   pixel inset highlight along the top edge which is what makes a control look
   machined rather than painted, and a cool neutral (a blue bias of a few
   points) because pure grey reads as unfinished next to a true black ground.
   No colour is introduced: the site is monochrome and a tinted button would be
   the only hue on the page. */
.btn--w{
  background:linear-gradient(180deg,#FFFFFF 0%,#EDEEF2 44%,#D8DAE2 100%);
  color:#08080A;
  border:1px solid #C7CAD3;
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.96),
    inset 0 -1px 0 rgba(10,12,20,.07),
    0 1px 1px rgba(0,0,0,.55),
    0 6px 16px -8px rgba(190,200,220,.30);
  transition:background .16s ease,box-shadow .18s ease,transform .09s ease;
}
.btn--w:hover{
  opacity:1;
  background:linear-gradient(180deg,#FFFFFF 0%,#F4F5F8 46%,#E6E8EF 100%);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,1),
    inset 0 -1px 0 rgba(10,12,20,.05),
    0 1px 1px rgba(0,0,0,.55),
    0 10px 26px -10px rgba(200,212,235,.45);
}
.btn--w:active{transform:translateY(.5px);
  box-shadow:inset 0 1px 2px rgba(10,12,20,.16),0 1px 1px rgba(0,0,0,.4)}
.btn--w:focus-visible{outline:2px solid #8FA3C8;outline-offset:2px}

/* The quiet button gains the same machined edge, one step darker, so the pair
   read as the same material rather than two unrelated styles. */
.btn--g{
  background:linear-gradient(180deg,#17171A 0%,#111114 100%);
  border:1px solid var(--line2);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 1px 1px rgba(0,0,0,.4);
  transition:background .16s ease,border-color .16s ease,box-shadow .18s ease,transform .09s ease;
}
.btn--g:hover{background:linear-gradient(180deg,#1D1D21 0%,#151519 100%);border-color:#3C3C44;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.07),0 1px 1px rgba(0,0,0,.4)}
.btn--g:active{transform:translateY(.5px)}
@media(prefers-reduced-motion:reduce){.btn--w,.btn--g{transition:none}}

/* ── Footer ─────────────────────────────────────────────────────────────── */
.fgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:30px 26px;
  padding-bottom:30px}
.fg h4{margin:0 0 11px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);font-weight:500}
.fg a{display:block;padding:3.5px 0;color:var(--mut);font-size:13px;text-decoration:none;
  transition:color .13s}
.fg a:hover{color:var(--fg)}
.femail{margin-top:14px}
.femail span{display:block;font-size:11px;color:var(--dim);margin-bottom:5px}
.femail img{display:block;opacity:.72;transition:opacity .15s}
.femail img:hover{opacity:1}
.fnote{border-top:1px solid var(--line);padding-top:20px;max-width:78ch;
  color:var(--dim);font-size:12px;line-height:1.65}

/* ── Navigation ─────────────────────────────────────────────────────────── */
.navlinks{display:flex;gap:2px;margin-left:22px;font-size:13px;color:var(--mut)}
.ng{position:relative}
.ngt{display:inline-flex;align-items:center;gap:5px;height:32px;padding:0 11px;
  background:none;border:0;border-radius:8px;color:var(--mut);font:inherit;font-size:13px;
  cursor:pointer;transition:color .14s,background .14s}
.ngt svg{opacity:.55;transition:transform .18s ease,opacity .14s}
.ng.on .ngt,.ng:hover .ngt,.ng:focus-within .ngt,.ngt[aria-expanded="true"]{color:var(--fg);background:var(--panel2)}
.ng:hover .ngt svg,.ng:focus-within .ngt svg{transform:rotate(180deg);opacity:1}

/* The panel sits below with a transparent bridge above it, so moving the
   pointer diagonally from the trigger to an item does not cross a dead gap and
   close the menu, which is the single most common way a hover menu feels broken. */
.ngp{position:absolute;top:100%;left:-6px;padding-top:9px;z-index:60;
  opacity:0;visibility:hidden;transform:translateY(-4px);
  transition:opacity .16s ease,transform .16s ease,visibility .16s}
.ng.on .ngp,.ng:hover .ngp,.ng:focus-within .ngp{opacity:1;visibility:visible;transform:translateY(0)}
.ngp-in{min-width:310px;background:#0E0E10;backdrop-filter:blur(20px) saturate(180%);
  border:1px solid var(--line2);border-radius:13px;padding:7px;
  box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 18px 44px -12px rgba(0,0,0,.85)}
.ngp-in a{display:block;padding:9px 11px;border-radius:9px;text-decoration:none;
  transition:background .13s}
.ngp-in a:hover,.ngp-in a:focus-visible{background:var(--panel2);outline:none}
.ngp-in .ni{display:block;color:var(--fg);font-size:13.5px;font-weight:540;letter-spacing:-.01em}
.ngp-in .nd{display:block;color:var(--mut);font-size:12px;line-height:1.45;margin-top:2px}
.ngp-in a:hover .nd{color:var(--fg2)}

.navsearch{margin-left:auto;display:flex;align-items:center;gap:8px;height:34px;
  padding:0 12px;min-width:190px;max-width:270px;
  background:var(--panel);border:1px solid var(--line2);border-radius:9px;color:var(--mut);
  transition:border-color .15s,background .15s}
.navsearch:focus-within{border-color:#3C3C44;background:var(--panel2);color:var(--fg2)}
.navsearch input{flex:1;min-width:0;background:0;border:0;outline:0;color:var(--fg);
  font-family:inherit;font-size:13px}
.navsearch input::placeholder{color:var(--dim)}
.nav-cta{margin-left:10px}

/* On a phone the three things people actually came for stay visible; the rest
   is one tap away. A header that hides everything behind a hamburger makes the
   most common action the hardest one. */
.navquick{display:none;margin-left:auto;gap:14px;font-size:13px}
.navquick a{color:var(--fg2);text-decoration:none;white-space:nowrap}
.navquick a:active{color:var(--fg)}
.burger{display:none;margin-left:auto;width:36px;height:32px;background:none;border:0;
  cursor:pointer;flex-direction:column;justify-content:center;gap:5px;padding:0 8px}
.burger span{display:block;height:1.5px;background:var(--fg2);border-radius:2px;
  transition:transform .2s ease,opacity .2s ease}
.burger[aria-expanded="true"] span:first-child{transform:translateY(3.25px) rotate(45deg)}
.burger[aria-expanded="true"] span:last-child{transform:translateY(-3.25px) rotate(-45deg)}

.mobmenu{display:none;position:absolute;top:62px;left:0;right:0;
  background:#09090A;backdrop-filter:blur(20px);
  border-bottom:1px solid var(--line);padding:22px 0 30px;max-height:calc(100vh - 62px);
  overflow-y:auto}
.mobmenu.open{display:block}
.msgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:22px 26px}
.ms h4{margin:0 0 9px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);font-weight:500}
.ms a{display:block;padding:7px 0;color:var(--fg2);font-size:14.5px;text-decoration:none}
.ms a:active{color:var(--fg)}

@media(max-width:1040px){.ngp-in{min-width:280px}.navlinks{margin-left:12px}.ngt{padding:0 8px}}
@media(max-width:900px){
  .navlinks{display:none}
  .nav-cta{display:none}
  .navsearch{display:none}
  .navquick{display:flex}
  .burger{display:flex;margin-left:14px}
}
@media(max-width:430px){
  .navquick{gap:11px;font-size:12.5px}
  .brand .wm{display:none}
}
/* A table wider than the screen scrolls itself. Without this the whole page
   grows to the table's width and every line of prose overflows with it, which
   is what happened on a phone to the comparison table. */
@media(max-width:760px){
  table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  pre{overflow-x:auto}
}
@media(prefers-reduced-motion:reduce){
  .ngp,.ngt svg,.burger span{transition:none}
}

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
table.tl th{text-align:left;font-weight:500;color:var(--mut);font-size:12.5px;
  text-transform:uppercase;letter-spacing:.06em;padding:0 12px 10px;border-bottom:1px solid var(--line)}
table.tl td{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:top}
table.tl td.num{text-align:right;font-family:var(--mono);white-space:nowrap}
.tn{font-family:var(--mono);color:var(--fg);font-size:12.5px}
.sv{color:var(--fg2);font-size:12px}
.dsc{color:var(--mut);font-size:12px;line-height:1.5;max-width:62ch;margin-top:4px}
.tag{display:inline-block;font-size:12px;font-family:var(--mono);padding:2px 6px;
  border:1px solid var(--line2);border-radius:4px;color:var(--mut);margin-left:6px}
.tag.ok{color:var(--pos);border-color:#1E3A2A}
.tag.auth{color:#E3B341;border-color:#3A3020}
.note{border:1px solid var(--line);border-left:2px solid var(--line2);border-radius:var(--r);
  padding:14px 16px;color:var(--fg2);font-size:13px;line-height:1.6;margin:22px 0;background:var(--panel)}
.mtable{width:100%;border-collapse:collapse;font-size:13px;margin:14px 0}
.mtable td{padding:7px 10px;border-bottom:1px solid var(--line)}
.mtable td.num{text-align:right;font-family:var(--mono);color:var(--fg2)}
.bar{height:5px;background:var(--line2);border-radius:3px;display:block}

/* ── Responsive ─────────────────────────────────────────────────────────── */
/* Every rule here was a measured failure at a real viewport, not a guess. */
img{max-width:100%;height:auto}
/* A flex item's default min-width is its content width, so an input inside a
   flex row refuses to shrink and pushes its button off the screen at 320px. */
.bigsearch input,.navsearch input{min-width:0}
/* Long unbroken strings (URLs, identifiers) break rather than widen the page.
   A bare `a` needs it too: a full URL used as link text was the last thing
   still pushing the tools index past a 320px screen. */
td,th,a,.dsc,.src,.nm,.mono,pre code,code{overflow-wrap:anywhere}
p.src{white-space:normal}
@media(max-width:760px){
  table,.tbl,.mtable,table.tl{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  pre{overflow-x:auto;white-space:pre-wrap;overflow-wrap:anywhere}
}
@media(max-width:900px){
  /* Tap targets. The guideline is 44px and links in tables and lists were 20,
     which on a phone is a coin toss between two adjacent rows. Padding rather
     than height so the text keeps its position in the row. */
  .fg a,.ms a{padding:11px 0;display:block}
  .navquick a{padding:12px 3px;display:inline-block}
  td a,.tbl a,.fl a,li a{display:inline-block;padding:12px 0;min-height:44px;
    line-height:20px;box-sizing:border-box}
  .ngp-in a{padding:13px 11px}
  /* Type floor: metadata set at 11px reads as 9 on a phone. */
  td,th,.dsc,.src,.fnote,.fg a,.nd,.cap,.num{font-size:12.5px}
  code,.tbl code{font-size:12px}
  /* The menu control is a primary tap target and was 32px tall. */
  .burger{height:44px;width:44px}
  .eyebrow{font-size:11px}
}
/* On a large screen 11px metadata is legible and deliberate, but it was being
   used for prose as well as for labels. Anything that is a sentence gets the
   body floor; only true labels stay small. */
code,kbd,samp,.tbl code,td code,.mono{font-size:12px}
th,td,.dsc,p.src{font-size:12.5px}
.tbl code,td code{font-size:12px}
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
{NAV_JS}
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
    """This build's copy, or the previous build's if there is not one yet.

    A stamp change used to make every page cold at once, so the first visitor
    after a deploy paid a full rebuild: measured at fifteen seconds on a
    capability page, and the CDN then cached that slow response for everyone.
    A new build should make pages *stale*, not absent. The old copy is returned
    with a timestamp of zero, which reads as expired everywhere, so the caller
    serves it immediately and rebuilds behind the request exactly as it does for
    any other stale entry. Content from one deploy ago for a few seconds is a
    far better answer than a fifteen second wait.
    """
    stamped = _k(key)
    c = _cdb()
    if c is None:
        return _cache.get(stamped)
    try:
        r = c.execute("SELECT built, html FROM pages WHERE key=?", (stamped,)).fetchone()
        if r:
            return (r[0], r[1])
        r = c.execute("SELECT html FROM pages WHERE key LIKE ? AND key != '__warmlock__' "
                      "ORDER BY built DESC LIMIT 1", ("%:" + key,)).fetchone()
        return (0.0, r[0]) if r else None
    except Exception:
        return _cache.get(stamped)


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
        # Drop only the older copies of the page just written: the rest are still
        # serving as the fallback for pages this build has not rebuilt yet.
        bare = key.split(":", 1)[1] if ":" in key else key
        c.execute("DELETE FROM pages WHERE key LIKE ? AND key != ? AND key != '__warmlock__'",
                  ("%:" + bare, key))
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


def cached_value(key: str, ttl: int, build):
    """The same cache, for a small JSON value instead of a page.

    Exists because `catalog.published()` was a per-process dict computed on the
    first request that needed it: twenty-five aggregate queries, ten seconds on
    an idle box, more under load, once per worker, in front of a visitor. The
    page cache made every page instant and this map, checked before the page
    cache was even consulted, put the whole cost back. Anything derived from
    the index that a request path needs belongs here, warmed like the pages,
    served stale like the pages, and never computed in front of anyone after
    the first build.
    """
    import json as _json
    got = _cache.get(_k(key)) or _read(key)
    if got:
        if time.time() - got[0] >= ttl:
            with _build_lock:
                fresh = key not in _building
                if fresh:
                    _building.add(key)
            if fresh:
                threading.Thread(target=_rebuild, args=(key, lambda: _json.dumps(build())),
                                 name=f"rebuild:{key}", daemon=True).start()
        try:
            return _json.loads(got[1])
        except Exception:
            pass
    val = build()
    _write(key, _json.dumps(val))
    return val


def warm_value(key: str, ttl: int, build) -> bool:
    import json as _json
    got = _read(key)
    if got and time.time() - got[0] < ttl and got[0] > 0:
        return False
    _write(key, _json.dumps(build()))
    return True


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
