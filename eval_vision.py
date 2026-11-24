"""Score vision mode against ground truth.

    python eval_vision.py            reads runs/vision-*.jsonl (traces written by vision_server.py)

The vision pipeline never sees the event data. Here the event data of the same match is the answer key:
the video second of every frame maps to a match time (video.json), so each reading and each line can be checked.
"""
import glob
import json
import statistics
import sys

import engine

m = engine.Match()
video = json.load(open(engine.ROOT / "video.json", encoding="utf-8"))
KICK = {int(p): v for p, v in video["kickoff"].items()}


def to_match(vt):
    """video second -> (period, seconds into period), or None in a break."""
    p = max((p for p in KICK if vt >= KICK[p]), default=None)
    if p is None or vt - KICK[p] > m.period_len[p] + 1:
        return None
    return p, vt - KICK[p]


def vtime(e):
    return KICK[e["period"]] + (e["t"] - m.period_start[e["period"]])


rows = [json.loads(l) for f in sorted(glob.glob(str(engine.ROOT / "runs" / "vision-*.jsonl"))) for l in open(f, encoding="utf-8")]
rows = [r for r in rows if r.get("src") == video["video_id"]]
obs, lines = [r for r in rows if r["type"] == "obs"], [r for r in rows if r["type"] == "line"]
if not obs:
    sys.exit("no vision traces for this video in runs/")

# windows of video that were actually watched (gaps > 8 s split windows)
ts = sorted(o["frame_t"] for o in obs)
windows, start = [], ts[0]
for a, b in zip(ts, ts[1:]):
    if b - a > 8:
        windows.append((start, a))
        start = b
windows.append((start, ts[-1]))
watched = sum(b - a for a, b in windows)
seen = lambda vt, pad=0: any(a + pad <= vt <= b - pad for a, b in windows)

# truth from the event data
goals = [(vtime(e), e["team"]["name"]) for e in m.events
         if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal"]
corners = [vtime(e) for e in m.events if e["type"]["name"] == "Pass" and e["pass"].get("type", {}).get("name") == "Corner" and e["period"] < 5]
near_goal = [vtime(e) for e in m.events if e["period"] < 5 and e.get("location") and e["location"][0] >= 96]  # final ~20 m


def truth_score(vt):
    h = sum(1 for t, team in goals if t <= vt and team == m.home)
    return h, sum(1 for t, team in goals if t <= vt and team == m.away)


print(f"watched {watched / 60:.1f} min of video in {len(windows)} windows, {len(obs)} analysis cycles, {len(lines)} lines\n")

# 1. scoreboard readings (fact register)
clock_err, score_ok, score_n = [], 0, 0
for o in obs:
    b = o["raw"].get("board")
    mt = to_match(o["frame_t"])
    if not b or not mt or "NONE" in b.upper():
        continue
    import re
    c = re.search(r"(\d{1,3}):(\d{2})", b)
    s = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", b[c.end():]) if c else None
    if not (c and s):
        continue
    base = {1: 0, 2: 45 * 60, 3: 90 * 60, 4: 105 * 60}[mt[0]]
    if mt[1] < {1: 45, 2: 45, 3: 15, 4: 15}[mt[0]] * 60:  # the broadcast clock freezes in stoppage time
        clock_err.append(int(c.group(1)) * 60 + int(c.group(2)) - (base + mt[1]))
    score_n += 1
    # the graphic itself updates a few seconds after a goal: accept either side of a goal within 15 s
    read = (int(s.group(1)), int(s.group(2)))
    score_ok += read in (truth_score(o["frame_t"]), truth_score(o["frame_t"] - 15))
print(f"scoreboard  score read correctly {score_ok}/{score_n}" + (f" | clock error median {statistics.median(clock_err):+.1f} s, "
      f"within 3 s in {sum(abs(x) <= 3 for x in clock_err)}/{len(clock_err)} readings" if clock_err else ""))

# 2. goals (fact register): every goal inside a watched window should be announced once, with the right score
gl = [l for l in lines if l["kind"] == "v_goal"]
inside = [(t, team) for t, team in goals if seen(t, 0) and seen(t + 25)]
hit, delays = 0, []
for t, team in inside:
    match = next((l for l in gl if 0 <= l["frame_t"] - t <= 40 and team in l["text"]), None)
    if match:
        h, a = truth_score(t)
        hit += f"{h}-{a}" in match["text"]
        delays.append(match["frame_t"] + match["ms"] / 1000 - t)
false_goals = [l for l in gl if not any(0 <= l["frame_t"] - t <= 40 for t, _ in goals)]
print(f"goals       announced with correct team and score {hit}/{len(inside)}" + (f" | {statistics.median(delays):.1f} s after the shot (median)"
      if delays else "") + f" | false goal calls {len(false_goals)}")

# 3. impressions: is each claim true within a tolerance?
def precision(kind, truth, before, after):
    said = [l for l in lines if l["kind"] == kind]
    ok = sum(any(-before <= l["frame_t"] - t <= after for t in truth) for l in said)
    return ok, len(said)

ok, n = precision("v_attack", near_goal, 6, 6)
print(f"near goal   'Play is close to the goal' true (ball within ~20 m of goal, +-6 s) {ok}/{n}")
ok, n = precision("v_corner", corners, 40, 8)
rec = sum(any(-40 <= l["frame_t"] - t <= 8 for l in lines if l["kind"] == "v_corner") for t in corners if seen(t, 0))
print(f"corners     claims that were real corners {ok}/{n} | real corners announced {rec}/{sum(seen(t, 0) for t in corners)}")
ok, n = precision("v_celebration", [t for t, _ in goals], 0, 40)
print(f"celebration claims that followed a real goal {ok}/{n}")

# 4. names from close-ups: was the named player involved in the play around that moment?
def involved(name, vt, span=25):
    return any(abs(vtime(e) - vt) <= span and m.name(e.get("player")) == name for e in m.events if e["period"] < 5)

named = [l for l in lines if l["kind"] in ("v_closeup", "v_scorer")]
ok = sum(any(n in l["text"] and involved(n, l["frame_t"]) for n in m.names.values()) for l in named)
print(f"names       player named from a close-up was on the ball within +-25 s {ok}/{len(named)}")
sc = [l for l in lines if l["kind"] == "v_scorer"]
ok = sum(any(m.name(e["player"]) in l["text"] for e in m.events if e["period"] < 5 and e["type"]["name"] == "Shot"
             and e["shot"]["outcome"]["name"] == "Goal" and 0 <= l["frame_t"] - vtime(e) <= 45) for l in sc)
print(f"scorer      'looks like the scorer' was the real scorer {ok}/{len(sc)}")

# 5. speed
ms = sorted(o["ms"] for o in obs)
print(f"\nspeed       analysis cycle p50 {ms[len(ms) // 2]} ms, p95 {ms[int(len(ms) * .95)]} ms | {len(lines) / (watched / 60):.1f} lines per minute")
print("\nlines:")
for l in lines[-40:]:
    mt = to_match(l["frame_t"])
    print(f'  video {l["frame_t"]:7.1f}  [{l["kind"][2:]:<11}] {l["text"]}')
