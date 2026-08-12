#!/usr/bin/env python3
"""Generate the explanatory figures used by docs/weftmark.mdx.

The diagrams are intentionally generated from code rather than stored as opaque
binary source assets. This keeps the editable repository bootstrap text-only;
CI can reproduce the exact PNG inputs before Pandoc embeds them into the
self-contained HTML and WeasyPrint PDF.
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "figures"

W, H = 1400, 820
CREAM = "#f7f0df"
PAPER = "#fffaf0"
NAVY = "#173b65"
BLUE = "#356da6"
LIGHT_BLUE = "#8fb0cc"
ORANGE = "#e99a4a"
SAND = "#efc38c"
INK = "#182330"
MUTED = "#66717b"
LINE = "#d6c8b4"
GREEN = "#56826d"
RED = "#a6534f"

try:
    FONT = ImageFont.truetype("DejaVuSans.ttf", 30)
    SMALL = ImageFont.truetype("DejaVuSans.ttf", 24)
    TINY = ImageFont.truetype("DejaVuSans.ttf", 20)
    BOLD = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    TITLE = ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
except OSError:  # Pillow's built-in fallback keeps the build portable.
    FONT = SMALL = TINY = BOLD = TITLE = ImageFont.load_default()


def canvas(title: str, subtitle: str = ""):
    im = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((38, 38, W-38, H-38), radius=42, fill=PAPER, outline=LINE, width=3)
    d.text((78, 68), title, fill=NAVY, font=TITLE)
    if subtitle:
        d.text((80, 126), subtitle, fill=MUTED, font=SMALL)
    return im, d


def box(d, xy, title, body=(), fill="#ffffff", outline=LINE, accent=None, width=3):
    x1,y1,x2,y2=xy
    d.rounded_rectangle(xy, radius=24, fill=fill, outline=outline, width=width)
    if accent:
        d.rounded_rectangle((x1, y1, x1+12, y2), radius=6, fill=accent)
    d.text((x1+28, y1+22), title, fill=INK, font=BOLD)
    yy=y1+76
    for line in body:
        d.text((x1+30, yy), line, fill=MUTED, font=SMALL)
        yy += 34


def arrow(d, a, b, color=BLUE, width=7, head=18):
    d.line((a,b), fill=color, width=width)
    import math
    dx,dy=b[0]-a[0], b[1]-a[1]
    ang=math.atan2(dy,dx)
    p1=(b[0]-head*math.cos(ang-0.55), b[1]-head*math.sin(ang-0.55))
    p2=(b[0]-head*math.cos(ang+0.55), b[1]-head*math.sin(ang+0.55))
    d.polygon([b,p1,p2], fill=color)


def pill(d, xy, text, fill, fg="#ffffff"):
    d.rounded_rectangle(xy, radius=22, fill=fill)
    bbox=d.textbbox((0,0), text, font=TINY)
    tw=bbox[2]-bbox[0]; th=bbox[3]-bbox[1]
    x1,y1,x2,y2=xy
    d.text(((x1+x2-tw)/2,(y1+y2-th)/2-2),text,fill=fg,font=TINY)


def save(im, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path=OUT/name
    im.save(path, "PNG", optimize=True)
    print(path.relative_to(ROOT))


def separation():
    im,d=canvas("Separation of concerns", "Workers do the work; WeftMark records coordination and proof.")
    box(d,(90,220,410,610),"Workers",("Claude Code / Codex","OpenCode / local agents","humans / scripts"),fill="#eef4fa",accent=BLUE)
    box(d,(540,190,860,640),"WeftMark",("scope + claims","Git lineage","evidence + handoff","review readiness"),fill="#fff5e7",accent=ORANGE)
    box(d,(990,220,1310,610),"Existing systems",("Git + forge","build / CI","issue trackers","runtime / deploy"),fill="#f3f0ea",accent=NAVY)
    arrow(d,(410,370),(540,370)); arrow(d,(860,370),(990,370)); arrow(d,(990,470),(860,470),color=ORANGE)
    save(im,"separation-of-concerns.png")


def bottleneck():
    im,d=canvas("The coordination bottleneck", "Cheap parallel code generation makes trustworthy integration the scarce resource.")
    y=255
    for i,(label,c) in enumerate([("Agent A",BLUE),("Agent B",NAVY),("Agent C",ORANGE),("Human",GREEN)]):
        yy=y+i*112
        pill(d,(105,yy,310,yy+58),label,c)
        arrow(d,(320,yy+29),(650,390),color=c,width=6)
    d.rounded_rectangle((620,310,825,500),radius=34,fill="#fff5e7",outline=ORANGE,width=5)
    d.text((655,360),"integration",fill=INK,font=BOLD); d.text((670,408),"proof gate",fill=MUTED,font=SMALL)
    for i,label in enumerate(["merge","release","deploy"]):
        yy=260+i*130
        arrow(d,(825,405),(1080,yy+30),color=NAVY,width=6)
        pill(d,(1085,yy,1280,yy+60),label,NAVY)
    save(im,"coordination-bottleneck.png")


def frog_today():
    im,d=canvas("RAGBAZ Frog today", "A coordination toolkit with proven primitives - not yet the full evidence/review ledger.")
    items=[("Identity", "agents + sessions"),("Tasks", "deps + claims"),("Locks","file/repo scope"),("Affected","target graph"),("Causality","event log / why"),("Federation","SSH workspaces"),("Surfaces","CLI + MCP + board")]
    for i,(a,b) in enumerate(items):
        col=i%4; row=i//4
        x=90+col*320; y=230+row*230
        box(d,(x,y,x+270,y+165),a,(b,),fill="#eef4fa" if i%2==0 else "#fff5e7",accent=BLUE if i%2==0 else ORANGE)
    save(im,"frog-today.png")


def lifecycle():
    im,d=canvas("Change Set lifecycle", "A durable envelope around transient worker sessions.")
    stages=[("PLAN",BLUE),("CLAIM",BLUE),("CHANGE",ORANGE),("PROVE",ORANGE),("REVIEW",NAVY),("MERGE",GREEN)]
    xs=[85,305,525,745,965,1185]
    for x,(s,c) in zip(xs,stages):
        pill(d,(x,335,x+150,405),s,c)
    for i in range(len(xs)-1): arrow(d,(xs[i]+152,370),(xs[i+1]-10,370),color=LIGHT_BLUE,width=5)
    d.text((135,505),"base SHA + goal + declared scopes + branch/worktree + commits + evidence + decisions",fill=MUTED,font=FONT)
    pill(d,(490,600,900,660),"blocked / stale / evidence-incomplete",RED)
    save(im,"changeset-lifecycle.png")


def semantic_collision():
    im,d=canvas("Why file locks are not enough", "Different files can still modify the same protocol, schema, or security boundary.")
    box(d,(100,235,470,540),"Agent A",("edits parser.py","files do not overlap"),fill="#eef4fa",accent=BLUE)
    box(d,(930,235,1300,540),"Agent B",("edits encoder.rs","files do not overlap"),fill="#fff5e7",accent=ORANGE)
    d.rounded_rectangle((545,285,855,500),radius=34,fill="#f4e9e7",outline=RED,width=5)
    d.text((585,330),"contract:event-v2",fill=RED,font=BOLD)
    d.text((600,392),"shared semantic",fill=INK,font=FONT)
    d.text((625,432),"collision",fill=INK,font=FONT)
    arrow(d,(470,390),(545,390),color=BLUE); arrow(d,(930,390),(855,390),color=ORANGE)
    pill(d,(500,610,900,672),"semantic / contract scope lock",NAVY)
    save(im,"semantic-collision.png")


def layering():
    im,d=canvas("Target layering", "One domain model, several replaceable adapters and user surfaces.")
    layers=[("SURFACES",("CLI","MCP","TUI","tablet/read"),"#eef4fa",BLUE),
            ("APPLICATION",("claim","handoff","review","readiness"),"#fff5e7",ORANGE),
            ("DOMAIN",("ChangeSet","Scope","Evidence","Decision"),"#eef4fa",NAVY),
            ("ADAPTERS",("Git","GitHub/GitLab","CI","local DB/files"),"#f3f0ea",GREEN)]
    y=215
    for title,vals,fill,c in layers:
        d.rounded_rectangle((160,y,1240,y+120),radius=26,fill=fill,outline=c,width=4)
        d.text((200,y+30),title,fill=c,font=BOLD)
        xx=500
        for v in vals:
            pill(d,(xx,y+30,xx+150,y+86),v,c); xx+=175
        y+=135
    save(im,"target-layering.png")


def ecosystem():
    im,d=canvas("Open-source ecosystem position", "WeftMark belongs between worker harnesses and durable engineering systems.")
    box(d,(70,245,390,555),"Worker / harness",("OpenCode","Aider","Cline/Roo","OpenHands"),fill="#eef4fa",accent=BLUE)
    box(d,(540,210,860,590),"WeftMark",("coordination ledger","proof + handoff","review readiness","vendor neutral"),fill="#fff5e7",accent=ORANGE)
    box(d,(1010,245,1330,555),"Durable systems",("Git forges","CI/build","issues/projects","deploy/runtime"),fill="#f3f0ea",accent=NAVY)
    arrow(d,(390,400),(540,400)); arrow(d,(860,400),(1010,400)); arrow(d,(1010,490),(860,490),color=ORANGE)
    save(im,"ecosystem-position.png")


def sequencing():
    im,d=canvas("Phase III implementation sequencing", "Build the trust model before expanding the interface surface.")
    phases=[("1  Domain", "ChangeSet / scope / lifecycle", BLUE),
            ("2  Lineage", "Git binding + branch/worktree", BLUE),
            ("3  Evidence", "typed proof + policies", ORANGE),
            ("4  Handoff", "portable continuation", ORANGE),
            ("5  Review", "ready / blocked / stale", NAVY),
            ("6  Surfaces", "CLI + MCP, then TUI/web", GREEN)]
    y=205
    for i,(a,b,c) in enumerate(phases):
        x=120 if i%2==0 else 720
        yy=y+(i//2)*185
        box(d,(x,yy,x+560,yy+135),a,(b,),fill="#ffffff",accent=c)
        if i<5:
            nx=720 if i%2==0 else 120
            ny=yy if i%2==0 else yy+185
    d.text((160,735),"Rule: no surface may invent semantics that the domain and evidence model cannot explain.",fill=MUTED,font=FONT)
    save(im,"phase-iii-sequencing.png")


GENERATORS=[separation,bottleneck,frog_today,lifecycle,semantic_collision,layering,ecosystem,sequencing]


def main() -> int:
    for fn in GENERATORS: fn()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
