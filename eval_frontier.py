"""Accuracy-vs-delay points we already have, from the vision traces and the data pipeline."""
import glob
import json
import re
import statistics
import sys

import engine

m = engine.Match()
video = json.load(open(engine.ROOT / "video.json", encoding="utf-8"))
KICK = {int(p): v for p, v in video["kickoff"].items()}
vtime = lambda e: KICK[e["period"]] + (e["t"] - m.period_start[e["period"]])
goals = [(vtime(e), e["team"]["name"]) for e in m.events if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal"]
near = sorted(vtime(e) for e in m.events if e["period"] < 5 and e.get("location") and e["location"][0] >= 96)


def truth_score(vt):
    return (sum(1 for t, team in goals if t <= vt and team == m.home), sum(1 for t, team in goals if t <= vt and team == m.away))


def board(raw):
    b = raw.get("board")
    if not b:
        return None
    c = re.search(r"(\d{1,3}):(\d{2})", b)
    s = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", b[c.end():]) if c else None
    return (int(s.group(1)), int(s.group(2))) if s else None


runs = []
for f in sorted(glob.glob(str(engine.ROOT / "runs" / "**" / "vision-*.jsonl"), recursive=True)):
    obs = [json.loads(l) for l in open(f, encoding="utf-8")]
    obs = [o for o in obs if o["type"] == "obs" and o.get("src") == video["video_id"]]
    seg = []
    for o in obs:  # split a file into continuous viewings
        if seg and not (0 <= o["frame_t"] - seg[-1]["frame_t"] <= 8):
            runs.append(seg)
            seg = []
        seg.append(o)
    if seg:
        runs.append(seg)
cycles = sum(len(r) for r in runs)
print(f"{len(runs)} continuous viewings, {cycles} cycles, {sum(r[-1]['frame_t'] - r[0]['frame_t'] for r in runs) / 60:.1f} min")

# ---- GOAL: evidence tiers, each with delay after the shot and how often it fires without a goal
tiers = {"celebration seen once": [], "celebration seen twice": [], "scoreboard read once": [], "scoreboard read twice": []}
false = {k: 0 for k in tiers}
inst = 0
for r in runs:
    t0, t1 = r[0]["frame_t"], r[-1]["frame_t"]
    for gt, _ in goals:
        if not (t0 + 2 <= gt <= t1 - 20):
            continue
        inst += 1
        after = [o for o in r if 0 <= o["frame_t"] - gt <= 40]
        when = lambda o: o["frame_t"] + o["ms"] / 1000 - gt
        cel = [i for i, o in enumerate(after) if o["event"] == "celebration"]
        if cel:
            tiers["celebration seen once"].append(when(after[cel[0]]))
        two = next((i for i in cel if i + 1 < len(after) and after[i + 1]["event"] == "celebration"), None)
        if two is not None:
            tiers["celebration seen twice"].append(when(after[two + 1]))
        new = truth_score(gt)
        reads = [o for o in after if board(o["raw"]) == new]
        if reads:
            tiers["scoreboard read once"].append(when(reads[0]))
        if len(reads) > 1:
            tiers["scoreboard read twice"].append(when(reads[1]))
    # false alarms: evidence present when no goal happened in the previous 40 s
    quiet = lambda o: not any(0 <= o["frame_t"] - gt <= 40 for gt, _ in goals)
    for i, o in enumerate(r):
        if o["event"] == "celebration" and quiet(o):
            false["celebration seen once"] += 1
            if i + 1 < len(r) and r[i + 1]["event"] == "celebration":
                false["celebration seen twice"] += 1
        b = board(o["raw"])
        if b and quiet(o) and b != truth_score(o["frame_t"]) and b != truth_score(o["frame_t"] - 15):
            false["scoreboard read once"] += 1
print(f"\nGOAL evidence over {inst} goal viewings (delay after the shot, seconds):")
for k, v in tiers.items():
    if v:
        print(f"  {k:<24} fired for {len(v)}/{inst} | median {statistics.median(v):5.1f} | min {min(v):4.1f} max {max(v):4.1f} | false alarms {false[k]}"
              f" -> precision {len(v)}/{len(v) + false[k]}")

# ---- NEAR GOAL: how many consecutive sightings before speaking
print("\nNEAR GOAL claim by number of consecutive sightings required:")
for n in (1, 2, 3):
    ok = tot = 0
    wait = []
    for r in runs:
        streak, start = 0, None
        for o in r:
            if o["view"] == "wide" and o["goal"] in ("yes", "left", "right"):
                streak += 1
                start = start if streak > 1 else o["frame_t"]
                if streak == n:
                    tot += 1
                    ok += any(abs(o["frame_t"] - t) <= 6 for t in near)
                    wait.append(o["frame_t"] + o["ms"] / 1000 - start)
            else:
                streak = 0
    print(f"  {n} sighting(s): right {ok}/{tot} = {100 * ok / max(tot, 1):.0f}% | spoken {statistics.median(wait):.1f} s after first sighting (median)")

# ---- DATA pipeline: delay between an event and its line at 1x, by salience class
delays = {"priority (salience >= 7)": [], "ordinary": []}
words = []
for e, st, now, jobs, d in engine.play(m, 1.0):
    for j in jobs:
        if j["voice"] != "pbp":
            continue
        delays["priority (salience >= 7)" if j["sal"] >= 7 else "ordinary"].append(now - j.get("at", now))
        words.append(len(engine.render(j).split()))
print("\nDATA pipeline, 1x, Director wait before a line is spoken (LLM and feed lag not included):")
for k, v in delays.items():
    v.sort()
    print(f"  {k:<26} n={len(v)} | median {statistics.median(v):.1f} s | p90 {v[int(len(v) * .9)]:.1f} s")
minutes = m.duration / 60
print(f"  lines/min {(len(words)) / minutes:.1f} | words/min {sum(words) / minutes:.0f} | words per line {statistics.mean(words):.1f}")
named = sum(1 for e, st, now, jobs, d in engine.play(m, 1.0) for j in jobs if j["voice"] == "pbp" and j["facts"].get("player"))
print(f"  lines naming a player: {named / minutes:.1f} per minute")
