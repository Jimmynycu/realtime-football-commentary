"""Human commentator baseline from the broadcast's auto-captions, aligned to the event data of the same match."""
import json
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict

import engine

CAP = engine.DATA / "captions_RgqKdplLIk4.json"  # auto-captions of the broadcast, saved from the YouTube player
m = engine.Match()
video = json.load(open(engine.ROOT / "video.json", encoding="utf-8"))
KICK = {int(p): v for p, v in video["kickoff"].items()}
fold = engine.fold


def vtime(e):
    return KICK[e["period"]] + (e["t"] - m.period_start[e["period"]])


# ---- words with timestamps
words = []
for ev in json.load(open(CAP, encoding="utf-8"))["events"]:
    for seg in ev.get("segs", []) or []:
        for w in re.findall(r"[a-z']+", fold(seg.get("utf8", ""))):
            words.append(((ev["tStartMs"] + seg.get("tOffsetMs", 0)) / 1000, w))
words.sort()
print("caption words:", len(words), "| first", round(words[0][0]), "s, last", round(words[-1][0]), "s")

# in-play windows: regulation halves + extra time
spans = [(KICK[p], KICK[p] + m.period_len[p]) for p in (1, 2, 3, 4)]
inplay = [(t, w) for t, w in words if any(a <= t <= b for a, b in spans)]
minutes = sum(b - a for a, b in spans) / 60
print(f"speech rate in play: {len(inplay) / minutes:.0f} words/min over {minutes:.0f} min")
secs = {int(t) for t, _ in inplay}
total = sum(int(b) - int(a) for a, b in spans)
print(f"seconds with at least one word: {100 * len(secs) / total:.0f}%")
# silence gaps
gaps = [b[0] - a[0] for a, b in zip(inplay, inplay[1:]) if b[0] - a[0] < 120]
print(f"pauses: median gap between words {statistics.median(gaps):.2f}s | gaps > 3s per minute: {sum(g > 3 for g in gaps) / minutes:.1f} | longest typical (p99) {sorted(gaps)[int(len(gaps) * .99)]:.1f}s")

# ---- which player names does the ASR spell consistently?
cnt = Counter(w for _, w in inplay)
surname = {}
for pid, name in m.names.items():
    toks = [t for t in re.findall(r"[a-z]+", fold(name)) if len(t) >= 4]
    if toks:
        surname[pid] = toks[-1]
ALIAS = {"allister": ["mcallister", "macallister", "allister"], "maria": ["maria"], "paul": ["paul"], "mbappe": ["mbappe", "mbape", "embape", "bape"],
         "muani": ["muani", "mwani"], "tchouameni": ["chuameni", "tchouameni", "chameni", "shuameni"]}
used = {pid: ALIAS.get(s, [s]) for pid, s in surname.items()}
mentions = {pid: [t for t, w in inplay if w in al] for pid, al in used.items()}
ok = {pid: ts for pid, ts in mentions.items() if len(ts) >= 12}
print("names recognised (>=12 mentions):", ", ".join(f"{m.names[p].split()[-1]} {len(ts)}" for p, ts in sorted(ok.items(), key=lambda x: -len(x[1]))))
all_mentions = sum(len(ts) for ts in ok.values())
print(f"player-name calls: {all_mentions / minutes:.1f} per minute ({100 * all_mentions / len(inplay):.0f}% of all words)")

# ---- spells: consecutive on-ball events by the same player
ONBALL = {"Ball Receipt*", "Pass", "Carry", "Shot", "Dribble", "Ball Recovery", "Interception", "Clearance", "Goal Keeper", "Miscontrol", "Dispossessed"}
spells, prev = defaultdict(list), None
for e in m.events:
    if e["period"] > 4 or e["type"]["name"] not in ONBALL or "player" not in e:
        continue
    pid, t = e["player"]["id"], vtime(e)
    if prev and prev[0] == pid and t - prev[2] < 4:
        prev[2] = t
    else:
        prev = [pid, t, t]
        spells[pid].append(prev)

# latency: name mention relative to the start of the nearest spell of that player
lat, covered, n_spells = [], 0, 0
for pid, ts in ok.items():
    starts = [s[1] for s in spells[pid]]
    for w in ts:
        near = [w - s for s in starts if -3 <= w - s <= 6]
        if near:
            lat.append(min(near, key=abs))
    for s in spells[pid]:
        n_spells += 1
        covered += any(-3 <= w - s[1] <= 6 for w in ts)
lat.sort()
q = lambda p: lat[int(len(lat) * p)]
print(f"name latency vs first touch (n={len(lat)}): p10 {q(.1):+.1f}s | median {q(.5):+.1f}s | p90 {q(.9):+.1f}s | said BEFORE the touch {100 * sum(x < 0 for x in lat) / len(lat):.0f}%")
print(f"coverage: {100 * covered / n_spells:.0f}% of {n_spells} touches by those players get the name within -3..+6 s")
hist = Counter(min(6, max(-3, round(x))) for x in lat)
print("latency histogram (s: count):", " ".join(f"{k:+d}:{hist[k]}" for k in range(-3, 7)))

# ---- goals: when does the scorer's name / 'goal' come?
for e in m.events:
    if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal":
        t, pid = vtime(e), e["player"]["id"]
        al = used.get(pid, [])
        nm = next((w_t - t for w_t, w in inplay if -2 <= w_t - t <= 15 and w in al), None)
        burst = sum(1 for w_t, _ in inplay if 0 <= w_t - t <= 10)
        print(f"  goal {m.names[pid].split()[-1]:<10} scorer named {('%+.1f s' % nm) if nm is not None else 'not found'} | words in next 10 s: {burst}")

# ---- what is said when nothing happens? words/min by event density
ev_times = sorted(vtime(e) for e in m.events if e["period"] <= 4 and e["type"]["name"] in ONBALL)
import bisect
busy, quiet = [0, 0.0], [0, 0.0]
for a, b in spans:
    t = a
    while t + 10 <= b:
        n_ev = bisect.bisect(ev_times, t + 10) - bisect.bisect(ev_times, t)
        n_w = sum(1 for wt, _ in inplay if t <= wt < t + 10)
        tgt = busy if n_ev >= 8 else quiet if n_ev <= 2 else None
        if tgt is not None:
            tgt[0] += n_w
            tgt[1] += 10
        t += 10
print(f"words/min when the ball is busy (>=8 events/10s): {60 * busy[0] / busy[1]:.0f} | when play is dead (<=2 events/10s): {60 * quiet[0] / quiet[1]:.0f}")
