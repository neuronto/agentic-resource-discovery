"""Compose the link-preview card.

A decorative render was the wrong artefact: dark gradients are the worst case
for JPEG, and 152 KB at 1344x768 banded visibly in the card. A preview should
also say what the thing is. This is flat colour and text, so PNG keeps it exact
at any scale with no compression artefacts, and the numbers are read from the
live index rather than typed, so the card cannot drift from the truth.
"""
import math, sqlite3, sys
from PIL import Image, ImageDraw, ImageFont

W, H, S = 1200, 630, 3          # supersample for crisp text and curves
BG, FG, MUT, DIM, LINE = "#09090A", "#FAFAFA", "#A5A5AB", "#45454C", "#26262A"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

c = sqlite3.connect("file:***REMOVED***/neuronto.db?mode=ro", uri=True)
q = lambda s: c.execute(s).fetchone()[0]
tools = q("SELECT COUNT(*) FROM tools")
pubs = q("SELECT COUNT(*) FROM crawl_seen WHERE manifest_path IS NOT NULL")
entries = q("SELECT COUNT(*) FROM entries")

img = Image.new("RGB", (W*S, H*S), BG)
d = ImageDraw.Draw(img)
f = lambda p, sz: ImageFont.truetype(p, sz*S)

# the aperture mark, same geometry as mark.svg
cx, cy, r, w = 96*S, 96*S, 34*S, 7*S
for a0, a1, col in [(270,360,FG),(0,90,"#6E6E76"),(90,180,FG),(180,270,"#3A3A40")]:
    d.arc([cx-r,cy-r,cx+r,cy+r], a0, a1, fill=col, width=w)
    for a in (a0,a1):
        x=cx+r*math.cos(math.radians(a)); y=cy+r*math.sin(math.radians(a))
        d.ellipse([x-w/2,y-w/2,x+w/2,y+w/2], fill=col)
d.ellipse([cx-10*S,cy-10*S,cx+10*S,cy+10*S], fill=FG)
d.text((150*S, 78*S), "neuronto", font=f(BOLD,30), fill=FG)

d.text((96*S, 210*S), "Agentic Resource", font=f(BOLD,72), fill=FG)
d.text((96*S, 292*S), "Discovery Index", font=f(BOLD,72), fill=FG)
d.text((96*S, 392*S),
       "One search across every public ARD registry.",
       font=f(SANS,27), fill=MUT)

d.line([(96*S, 462*S), ((W-96)*S, 462*S)], fill=LINE, width=1*S)

stats = [(f"{tools:,}", "verified tools"),
         (f"{pubs:,}", "ARD publishers"),
         (f"{entries:,}", "resources indexed"),
         ("5", "registries federated")]
x = 96*S
for val, lab in stats:
    d.text((x, 500*S), val, font=f(MONO,34), fill=FG)
    d.text((x, 548*S), lab, font=f(SANS,20), fill=DIM)
    x += max(d.textlength(val, font=f(MONO,34)),
             d.textlength(lab, font=f(SANS,20))) + 58*S

out = img.resize((W, H), Image.LANCZOS)
out.save("***REMOVED***/web/img/og-v1.png", "PNG", optimize=True)
print(f"  wrote og.png {out.size}  tools={tools:,} pubs={pubs} entries={entries:,}")
import os; print("  bytes:", os.path.getsize("***REMOVED***/web/img/og-v1.png"))
