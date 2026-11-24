"""Check the football model (game.py) against the whole match and against the human commentator.

    python eval_game.py

1. How the match splits into phases of play (by time).
2. What the human commentator does in each phase (words and player names per minute, from the broadcast captions).
3. Do the model's "this matters" signals actually precede shots?
"""
import bisect
import json
import re
from collections import defaultdict

import engine
import game

m, frames = game.load()
video = json.load(open(engine.ROOT / "video.json", encoding="utf-8"))
KICK = {int(p): v for p, v in video["kickoff"].items()}
vtime = lambda e: KICK[e["period"]] + (e["t"] - m.period_start[e["period"]])

g, states = game.Game(m, frames), []
for e in m.events:
    if e["period"] > 4:
        continue
    s = g.step(e)
    if s:
        s["vt"], s["poss"], s["period"] = vtime(e), e["possession"], e["period"]
        states.append(s)

# ---- 1. time per phase. A gap of more than 4 s between ball events is dead ball.
dur = defaultdict(float)
segments = []  # (start, end, phase) in video seconds
for a, b in zip(states, states[1:]):
    gap = b["vt"] - a["vt"]
    if a["period"] != b["period"] or gap < 0:  # two events in the feed carry the wrong period label
        continue
    live = min(gap, 4.0)
    segments.append((a["vt"], a["vt"] + live, a["phase"]))
    dur[a["phase"]] += live
    if gap > 4.0:
        segments.append((a["vt"] + 4.0, b["vt"], "dead ball"))
        dur["dead ball"] += gap - 4.0
total = sum(dur.values())

# ---- 2. the human commentator by phase
names = {w for n in m.names.values() for w in re.findall(r"[a-z]{4,}", engine.fold(n))[-1:]} | {"mcallister", "mbape", "embape"}
words = sorted(((ev["tStartMs"] + seg.get("tOffsetMs", 0)) / 1000, w)
               for ev in json.load(open(engine.DATA / "captions_RgqKdplLIk4.json", encoding="utf-8"))["events"]
               for seg in ev.get("segs", []) or [] for w in re.findall(r"[a-z']+", engine.fold(seg.get("utf8", ""))))
starts = [s[0] for s in segments]
spoken, named = defaultdict(int), defaultdict(int)
for t, w in words:
    i = bisect.bisect(starts, t) - 1
    if i >= 0 and segments[i][0] <= t < segments[i][1]:
        spoken[segments[i][2]] += 1
        named[segments[i][2]] += w in names

ORDER = ["build-up", "progression", "final-third attack", "counter-attack", "chance", "set piece", "dead ball"]
print(f'{"phase":<20}{"share of time":>14}{"human words/min":>18}{"human names/min":>18}')
for p in ORDER:
    mins = dur[p] / 60
    print(f"{p:<20}{100 * dur[p] / total:>13.0f}%{spoken[p] / mins:>18.0f}{named[p] / mins:>18.1f}")

# ---- 3. do the signals mean anything? shot within 10 s, in the same possession
shots = [(s["vt"], s["poss"]) for s in states if s["type"] == "Shot"]
shot_soon = lambda s: any(p == s["poss"] and 0 <= t - s["vt"] <= 10 for t, p in shots)


def first_per_possession(pred):
    seen, out = set(), []
    for s in states:
        if s["poss"] not in seen and s["on_ball"] and pred(s):
            seen.add(s["poss"])
            out.append(s)
    return out


tests = [("ball in the final third (baseline)", lambda s: s["x"] >= 80),
         ("free man in space ahead of the ball", lambda s: s["picture"].get("free_man", {}).get("kind") == "space"),
         ("free man within shooting range", lambda s: s["picture"].get("free_man", {}).get("kind") == "range"),
         ("counter-attack", lambda s: s["phase"] == "counter-attack"),
         ("ball in the box", lambda s: s["phase"] == "chance" and s["type"] != "Shot")]
print("\nshot within 10 s after the signal (same possession):")
for label, pred in tests:
    hits = first_per_possession(pred)
    k = sum(shot_soon(s) for s in hits)
    print(f"  {label:<38}{k:>3}/{len(hits):<4}= {100 * k / max(len(hits), 1):.0f}%")

# ---- 4. how much the model says
prev, lines, by_phase = None, 0, defaultdict(int)
for s in states:
    line = g.call(s, prev)
    if line:
        lines += 1
        by_phase[s["phase"]] += 1
    prev = s["phase"]
print(f"\nmodel lines before the airtime budget: {lines} ({lines / (total / 60):.1f} per minute) | " +
      ", ".join(f"{p} {by_phase[p] / (dur[p] / 60):.0f}/min" for p in ORDER if dur[p] and p != "dead ball"))
