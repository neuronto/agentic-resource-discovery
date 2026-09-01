#!/usr/bin/env python3
"""Push the shared header, footer and their styles into the static pages.

The generated pages get their chrome from `render.page`. The static ones each
carry their own copy, which is how the navigation drifted out of sync more than
once and how the footer ended up listing a retired URL. This makes render.py the
single source and the static files a build output.

    python3 scripts/sync_chrome.py
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import render                                            # noqa: E402

MARK_A, MARK_B = "<!--chrome:css-->", "<!--/chrome:css-->"


def chrome_css() -> str:
    """The shared chrome only, not the whole stylesheet: the static pages have
    their own design and must keep it. Buttons are included because they appear
    on every page and had drifted into three slightly different whites."""
    css = render.EXTRA_CSS
    out = []
    for block in ("/* ── Buttons ", "/* ── Footer ", "/* ── Navigation "):
        i = css.find(block)
        if i < 0:
            continue
        j = css.find("/* ── ", i + 10)
        out.append(css[i:j if j > 0 else len(css)])
    return "\n".join(out)


def sync(path: Path) -> bool:
    s = orig = path.read_text(encoding="utf-8")
    s = re.sub(r"<nav>.*?</nav>", lambda _m: render.NAV, s, count=1, flags=re.S)
    s = re.sub(r"<footer>.*?</footer>", lambda _m: render.FOOTER, s, count=1, flags=re.S)

    css = f"{MARK_A}\n{chrome_css()}\n{MARK_B}"
    if MARK_A in s:
        s = re.sub(re.escape(MARK_A) + r".*?" + re.escape(MARK_B), lambda _m: css, s,
                   count=1, flags=re.S)
    else:
        s = s.replace("</style>", css + "\n</style>", 1)

    if "querySelector('.burger')" not in s:
        s = s.replace("</body>", render.NAV_JS + "\n</body>", 1)
    if s != orig:
        path.write_text(s, encoding="utf-8")
        return True
    return False


def main() -> int:
    files = ([ROOT / "web/index.html", ROOT / "web/console.html"]
             + [Path(p) for p in glob.glob(str(ROOT / "web/pages/*.html"))]
             + [Path(p) for p in glob.glob(str(ROOT / "web/blog/*.html"))])
    n = sum(1 for f in files if f.exists() and sync(f))
    print(f"synced header and footer into {n} of {len(files)} static pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
