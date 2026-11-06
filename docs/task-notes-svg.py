#!/usr/bin/env python3
"""Writes task-notes.svg (the round-trip flowchart in task-notes.md). Edit here, then: python3 task-notes-svg.py task-notes.svg"""
import sys
from xml.sax.saxutils import escape as e
W, H = 1000, 830
BW, BH = 300, 66
COL = {'phone': 20, 'remote': 370, 'desk': 680}
RW = 260
out = []
def box(x, y, title, sub, w=BW, cls='box', step=None, mono=False):
    out.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{BH}" rx="8"/>')
    tx = x + (40 if step else 14)
    if step:
        out.append(f'<circle class="step" cx="{x+20}" cy="{y+BH/2}" r="12"/>'
                   f'<text class="stepn" x="{x+20}" y="{y+BH/2+5}" text-anchor="middle">{step}</text>')
    out.append(f'<text class="t" x="{tx}" y="{y+27}">{e(title)}</text>')
    out.append(f'<text class="{"m" if mono else "s"}" x="{tx}" y="{y+49}">{e(sub)}</text>')
def arrow(d, cls='a'):
    out.append(f'<path class="{cls}" d="{d}" marker-end="url(#{"hd" if cls=="dash" else "h"})"/>')
def label(x, y, text, anchor='middle', cls='l'):
    out.append(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{e(text)}</text>')

P, R, D = COL['phone'], COL['remote'], COL['desk']
# column heads
for x, w, name, note in ((P, BW, 'Phone', 'Termux'), (R, RW, 'Git remotes', 'shared by every station'), (D, BW, 'Desktop', 'nb-web, tw-web')):
    out.append(f'<rect class="col" x="{x-10}" y="14" width="{w+20}" height="{H-60}" rx="12"/>')
    label(x + w/2, 42, name, cls='h')
    label(x + w/2, 60, note, cls='s')

box(P, 80,  'Ask (here, or in tw-web)', 'task 42 annotate nb:', step=1, mono=True)
box(P, 176, 'Make: nb tw note', "nbnotes' hook, no network needed", step=2)
box(P, 272, 'The note, and the link to it', 'nb: tasknotes:kitchen/…note.md', mono=True)
box(P, 392, '~/.task', 'gittw commits; tw-web pushes', step=3)
box(P, 488, '~/.nb/tasknotes', 'm-sync pushes (every 15 min)', step=4)
box(P, 608, 'Open it on the phone', 'nb-web if it answers, else Documents')

box(R, 392, '~/.task repo', 'branch master', w=RW, cls='rbox')
box(R, 488, 'nb notes repo', 'branch tasknotes', w=RW, cls='rbox')

box(D, 80,  'The same hooks', 'nbnotes (+ tw2nb: archive, journal)', cls='obox')
box(D, 392, '~/.task', "tw-web's auto-sync pulls (or Sync)")
box(D, 488, '~/.nb/tasknotes', "nb-web's Sync pulls")
box(D, 608, 'Open: tw-web link → nb-web', 'the note, with the task header', step=5)
box(D, 704 - 0, 'Follow: task 42 done', 'nbnotes → nb tw status → tw_status', step=6)

cx = P + BW/2
arrow(f'M{cx} 146 V172'); arrow(f'M{cx} 242 V268'); arrow(f'M{cx} 338 V388')
arrow(f'M{P} 305 H6 V521 H{P-4}')                     # note -> tasknotes, round the left
label(cx+8, 368, 'the link is in the task', anchor='start')
arrow(f'M{cx} 554 V604')
# across
for y in (425, 521):
    arrow(f'M{P+BW} {y} H{R-4}'); arrow(f'M{R+RW} {y} H{D-4}')
label((P+BW+R)/2, 415, 'push'); label((R+RW+D)/2, 415, 'pull')
label((P+BW+R)/2, 511, 'push'); label((R+RW+D)/2, 511, 'pull')
dx = D + BW/2
arrow(f'M{dx} 554 V604')
arrow(f'M{D+BW} 425 H{W-6} V641 H{D+BW+4}')            # ~/.task -> open, round the right
arrow(f'M{dx} 674 V700')
# the hooks come with ~/.task
arrow(f'M{D+BW-40} 392 V150', cls='dash')
label(D+BW-46, 300, 'hooks arrive', anchor='end', cls='l')
label(D+BW-46, 316, 'with ~/.task', anchor='end', cls='l')
# the way back
arrow(f'M{D} 737 H{R+RW/2} V558', cls='dash')
label(R+RW/2+8, 700, 'status syncs back', anchor='start', cls='l')
label(R+RW/2+8, 716, 'the same way', anchor='start', cls='l')
# legend
out.append(f'<path class="a" d="M24 {H-20} H64"/>'); label(72, H-15, 'this walk-through', anchor='start')
out.append(f'<path class="dash" d="M224 {H-20} H264"/>'); label(272, H-15, 'arrives later, or goes back', anchor='start')

style = '''
text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: #1f2328; }
.h { font-size: 18px; font-weight: 600; }
.t { font-size: 15px; font-weight: 600; }
.s, .l { font-size: 13px; fill: #59636e; }
.m { font-size: 12.5px; fill: #59636e; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.col { fill: #ffffff; stroke: #d0d7de; }
.box, .obox { fill: #f6f8fa; stroke: #d0d7de; }
.obox { stroke-dasharray: 5 4; }
.rbox { fill: #fff8c5; stroke: #d4a72c; }
.step, .hh { fill: #0969da; }
.stepn { fill: #ffffff; font-size: 13px; font-weight: 700; }
.a { fill: none; stroke: #0969da; stroke-width: 2; }
.dash { fill: none; stroke: #59636e; stroke-width: 1.6; stroke-dasharray: 6 5; }
.hd { fill: #59636e; }
@media (prefers-color-scheme: dark) {
  text { fill: #e6edf3; }
  .s, .l, .m { fill: #9198a1; }
  .col { fill: #0d1117; stroke: #3d444d; }
  .box, .obox { fill: #161b22; stroke: #3d444d; }
  .rbox { fill: #2e2a10; stroke: #9e6a03; }
  .step, .hh { fill: #4493f8; }
  .a { stroke: #4493f8; }
  .dash { stroke: #9198a1; }
  .hd { fill: #9198a1; }
}
'''
svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="ttl dsc">
<title id="ttl">Task notes: the round trip</title>
<desc id="dsc">A bare nb: annotation on the phone makes the task's note there. ~/.task and the tasknotes notebook each sync through a git remote to the desktop, where the link opens the note in nb-web. Finishing the task updates the note's status, which syncs back the same way.</desc>
<style>{style}</style>
<defs>
<marker id="h" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="hh" d="M0 0 L10 5 L0 10 z"/></marker>
<marker id="hd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="hd" d="M0 0 L10 5 L0 10 z"/></marker>
</defs>
{chr(10).join(out)}
</svg>
'''
open(sys.argv[1], 'w').write(svg)
