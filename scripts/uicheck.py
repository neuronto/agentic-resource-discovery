#!/usr/bin/env python3
"""Browser checks for the hero search: layout at real viewports, and the
behaviour of the animated hint.

The HTTP suite in e2e.py can assert that the markup and the type floor are
present, but it cannot see a layout. These are the assertions that need a real
engine: that no viewport scrolls sideways, that the hint always fits the box it
is drawn in, and that it stops animating when it should. Everything here was a
bug at some point, so nothing here is decorative.

Needs `zendriver`. Run against production or any deployment:

    python3 scripts/uicheck.py [base-url]

Two traps this file exists to remember:

  * Chrome's `--window-size` does NOT change the layout viewport in headless.
    Screenshots taken that way are laid out at 500px and merely cropped, which
    makes a correct page look broken and a broken page look fine. Use CDP's
    setDeviceMetricsOverride, as below.
  * A cache buster that is constant across runs is not a buster. An early
    version reused `?cb=<width>`, the edge cached it on the first run, and every
    later run graded a stale copy and reported a failure that no longer existed.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys

import zendriver as zd

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://neuronto.com").rstrip("/")
SIZES = [(320, 720), (360, 780), (390, 844), (430, 932), (768, 1024), (1024, 800), (1440, 900)]

_passed: list[str] = []
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    (_passed if ok else _failed).append(name)


def url() -> str:
    return f"{BASE}/?cb={secrets.token_hex(6)}"


# An element only counts as overflowing if nothing above it scrolls or clips:
# a wide table inside its own overflow-x container is correct, not a defect.
OVERFLOW = """
(() => {
  const d = document.documentElement, vw = d.clientWidth, bad = [];
  document.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.overflowX === 'auto' || cs.overflowX === 'scroll') return;
    let p = el.parentElement;
    while (p) { const pc = getComputedStyle(p);
      if (pc.overflowX === 'auto' || pc.overflowX === 'scroll' || pc.overflowX === 'hidden') return;
      p = p.parentElement; }
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1)
      bad.push({ r: Math.round(r.right), t: el.tagName, c: String(el.className || '').slice(0, 34) });
  });
  bad.sort((a, b) => b.r - a.r);
  return JSON.stringify({ vw, sw: d.scrollWidth, over: d.scrollWidth > vw + 1, bad: bad.slice(0, 5) });
})()
"""

HERO = """
(() => {
  const f = document.querySelector('.bigsearch--hero'), i = document.getElementById('q'),
        t = document.getElementById('hsType'), b = document.getElementById('btn'),
        x = document.getElementById('hsTypeText');
  if (!f || !i || !t || !b || !x) return JSON.stringify({ err: 'hero markup missing' });
  const fr = f.getBoundingClientRect(), tr = t.getBoundingClientRect();
  const br = b.getBoundingClientRect(), xr = x.getBoundingClientRect();
  return JSON.stringify({
    barW: Math.round(fr.width), barH: Math.round(fr.height),
    fontPx: parseFloat(getComputedStyle(i).fontSize),
    btnW: Math.round(br.width), btnH: Math.round(br.height),
    btnLabel: getComputedStyle(b.querySelector('span')).display,
    btnArrow: getComputedStyle(b.querySelector('svg')).display,
    tap: Math.min(Math.round(br.width), Math.round(br.height)),
    hintFits: xr.right <= tr.right + 1, hintW: Math.round(xr.width),
    chips: [...document.querySelectorAll('.ex')].filter(e => getComputedStyle(e).display !== 'none').length,
    placeholder: i.placeholder, text: x.textContent, on: t.classList.contains('on')
  });
})()
"""

TEXT = "(()=>document.getElementById('hsTypeText').textContent)()"
CENTRE = "document.querySelector('.bigsearch--hero').scrollIntoView({block:'center'})"


async def frames(page, n=7, gap=0.3):
    out = []
    for _ in range(n):
        out.append(await page.evaluate(TEXT))
        await asyncio.sleep(gap)
    return out


async def viewports(page):
    print("\n  Layout, at a real viewport for each size\n  " + "-" * 62)
    for w, h in SIZES:
        await page.send(zd.cdp.emulation.set_device_metrics_override(
            width=w, height=h, device_scale_factor=1, mobile=w < 800))
        await page.get(url())
        await asyncio.sleep(1.2)
        await page.evaluate(CENTRE)
        await asyncio.sleep(1.8)
        o = json.loads(await page.evaluate(OVERFLOW))
        hero = json.loads(await page.evaluate(HERO))
        moved = len(set(await frames(page))) > 1
        tag = f"{w}x{h}"
        check(f"{tag}: page does not scroll sideways", not o["over"],
              f"(scrollWidth {o['sw']} vs {o['vw']})" + (f" {o['bad']}" if o["bad"] else ""))
        # Below 16px, mobile Safari zooms on focus and never zooms back out.
        check(f"{tag}: input is at least 16px", hero.get("fontPx", 0) >= 16,
              f"({hero.get('fontPx')}px)")
        check(f"{tag}: hint fits its box", bool(hero.get("hintFits")),
              f"(hint {hero.get('hintW')}px)")
        check(f"{tag}: submit is a 44px target", hero.get("tap", 0) >= 44,
              f"({hero.get('btnW')}x{hero.get('btnH')}, "
              f"label={hero.get('btnLabel')} arrow={hero.get('btnArrow')})")
        check(f"{tag}: hint is animating", moved, f"(chips {hero.get('chips')})")


async def behaviour(page):
    print("\n  Behaviour of the hint\n  " + "-" * 62)
    # Without focus emulation the page is never focused, so el.focus() sets
    # activeElement but fires no focus event, and the test measures nothing.
    await page.send(zd.cdp.emulation.set_focus_emulation_enabled(enabled=True))
    await page.send(zd.cdp.emulation.set_device_metrics_override(
        width=390, height=844, device_scale_factor=1, mobile=True))
    await page.get(url())
    await asyncio.sleep(1.0)
    await page.evaluate(CENTRE)
    await asyncio.sleep(1.2)

    check("animates while visible and empty", len(set(await frames(page))) > 1)

    await page.evaluate("document.getElementById('q').focus()")
    await asyncio.sleep(0.5)
    check("stops while the field is focused", len(set(await frames(page, 6))) == 1)

    await page.evaluate("document.getElementById('q').blur()")
    await asyncio.sleep(0.6)
    check("resumes after blur while still empty", len(set(await frames(page))) > 1)

    await page.evaluate("(()=>{const i=document.getElementById('q');"
                        "i.value='postgres';i.dispatchEvent(new Event('input'))})()")
    await asyncio.sleep(0.6)
    st = json.loads(await page.evaluate(HERO))
    check("clears once there is real input", (not st["on"]) and st["text"] == "")

    await page.evaluate("(()=>{const i=document.getElementById('q');"
                        "i.value='';i.dispatchEvent(new Event('input'))})()")
    await asyncio.sleep(1.0)
    check("returns when the field is cleared", len(set(await frames(page))) > 1)

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1.2)
    check("stops when scrolled out of view", len(set(await frames(page, 6))) == 1,
          "(no timer running below the fold)")

    await page.evaluate(CENTRE)
    await asyncio.sleep(1.0)
    check("restarts on scrolling back", len(set(await frames(page))) > 1)

    await page.send(zd.cdp.emulation.set_emulated_media(
        features=[zd.cdp.emulation.MediaFeature(name="prefers-reduced-motion", value="reduce")]))
    await page.get(url())
    await asyncio.sleep(2.0)
    st = json.loads(await page.evaluate(HERO))
    check("reduced motion keeps the native placeholder instead",
          (not st["on"]) and st["text"] == "" and bool(st["placeholder"]),
          f"({st['placeholder']!r})")


async def main() -> int:
    print(f"\n  UI checks against {BASE}")
    browser = await zd.start(headless=True,
                             browser_args=["--no-sandbox", "--disable-dev-shm-usage"])
    try:
        page = await browser.get("about:blank")
        await viewports(page)
        await behaviour(page)
    finally:
        await browser.stop()
    print("  " + "-" * 62)
    print(f"  {len(_passed)} passed, {len(_failed)} failed")
    if _failed:
        print("\n  FAILURES:\n   - " + "\n   - ".join(_failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
