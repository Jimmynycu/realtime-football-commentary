"""Colour-voice regressions, each one a mistake that actually went out before it was caught by reading the booth next to
the human transcript. Runs on the plain drafts (no model needed):  python eval_colour.py
"""
import booth
import game
from colour import short

m, frames = game.load()
lines, _ = booth.run(m, frames, booth.load_params())
fill = [l for l in lines if l["kind"] == "fill"]
checks = []

# 1. a player who has gone off is not described as quiet "so far"
off = {short(m.name(e["player"])): e["t"] for e in m.events if e["type"]["name"] == "Substitution"}
bad = [l["text"] for l in fill for who, t in off.items() if l["t"] > t and l["text"].startswith(who + " has hardly had a kick")]
checks.append(("no 'quiet tonight' about a substituted player", not bad, bad[:2]))

# 2. the score is not read out twice inside three minutes
score = [l["t"] for l in fill if "minutes played" in l["text"]]
gaps = [round(b - a) for a, b in zip(score, score[1:]) if b - a < 170]
checks.append(("score read-outs at least 170 s apart", not gaps, gaps[:5]))

# 3. a goal that only reduces the deficit is not "a grip on this final"
goals = [e for e in m.events if e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal" and e["period"] < 5]
tally, bad = {m.home: 0, m.away: 0}, []
for g in goals:
    team = g["team"]["name"]
    tally[team] += 1
    other = m.away if team == m.home else m.home
    said = [l["text"] for l in fill if g["t"] < l["t"] < g["t"] + 90]
    if tally[team] <= tally[other] and any("grip on this final" in s for s in said):
        bad.append((g["minute"], team))
checks.append(("no 'grip on this final' for a side that is not ahead by two", not bad, bad))

# 4. nothing left over from before a goal is said in the half-minute after it
bad = [(g["minute"], l["text"]) for g in goals for l in fill if g["t"] < l["t"] < g["t"] + 30
       and l["text"] in ("The break came to nothing in the end.", "Nothing comes of the corner, dealt with at the first attempt.")]
checks.append(("no stale verdict straight after a goal", not bad, bad[:2]))

# 5. the same sentence does not come round again inside five minutes
last, bad = {}, []
for l in fill:
    if l["text"] in last and l["t"] - last[l["text"]] < 300 and "minutes played" not in l["text"]:
        bad.append((round(l["t"] - last[l["text"]]), l["text"]))
    last[l["text"]] = l["t"]
checks.append(("no identical colour line inside five minutes", len(bad) <= 3, bad[:4]))

for name, ok, detail in checks:
    print(f'{"PASS" if ok else "FAIL"}  {name}' + ("" if ok else f"  {detail}"))
raise SystemExit(0 if all(c[1] for c in checks) else 1)
