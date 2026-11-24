"""Checks for the four products (products.py): they must agree with the match data, not just exist.

Run `python eval_products.py`; prints PASS/FAIL lines, exits non-zero on any FAIL. Rebuilds samples/ first.
"""
import json
import re
import sys

import engine
import products as P

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""))


def secs(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


for f in (P.subtitles, P.highlights, P.report, P.ratings):
    f()
m, ctl, v = P._plat()
S = P.OUT

# subtitles
cues = [(secs(a), secs(b), t) for a, b, t in re.findall(r"(\S+) --> (\S+)\n(.+)", (S / "commentary.vtt").read_text(encoding="utf-8"))]
lines = [l for l in ctl.lines if l["t"] < m.period_start[5]]
check("vtt cue count equals lines on air before the shootout", len(cues) == len(lines), f"{len(cues)} vs {len(lines)}")
check("vtt cues in time order", all(a[0] <= b[0] for a, b in zip(cues, cues[1:])))
check("vtt cues do not overlap", all(a[1] <= b[0] + 1e-6 for a, b in zip(cues, cues[1:])))
check("vtt cues have positive length", all(b > a for a, b, _ in cues))
check("vtt cues inside the video", cues[0][0] >= v["kick"][1] and cues[-1][1] <= P.video_end(), f"{cues[0][0]:.0f}-{cues[-1][1]:.0f}")
check("vtt every cue has a voice tag", all(t.startswith("<v ") for _, _, t in cues))

# highlights
hl = json.loads((S / "highlights.json").read_text(encoding="utf-8"))
check("highlight starts inside the video", all(v["kick"][1] <= h["video_start"] < P.video_end() for h in hl))
check("highlight links carry the start second", all(h["link"].endswith(f"&t={h['video_start']}s") and v["id"] in h["link"] for h in hl))
goals = [e for e in m.events if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal"]
got = {round(h["match_t"], 1) for h in hl}
check("all six goals are highlights", len(goals) == 6 and all(round(e["t"], 1) in got for e in goals), f"{len(goals)} goals")
check("highlights carry the lines said", all(h["lines"] for h in hl))

# report
rep = (S / "report.md").read_text(encoding="utf-8")
check("report states the ground-truth score", f"{m.truth[0]}-{m.truth[1]}" in rep, f"{m.home} {m.truth[0]}-{m.truth[1]} {m.away}")
scorers = {P.nm(e) for e in goals}
check("report names every scorer", all(n in rep for n in scorers), ", ".join(sorted(scorers)))
posts = re.findall(r"^(?:- )?(.+)$", rep.split("## Social")[1], re.M)
check("social posts fit 280 characters", all(len(p) <= 280 for p in posts))
story = rep.split("\n\n")[2]
check("story is 5 to 8 sentences", 5 <= len(re.findall(r"[.!?](?:\s|$)", story)) <= 8, story.count(". ") + 1)
check("no shootout kick is counted as a match goal", all(not e["period"] > 4 for e in goals))

# ratings
rt = json.loads((S / "ratings.json").read_text(encoding="utf-8"))
ids = {e["player"]["id"] for e in m.events if e.get("player")}
check("ratings cover every player with an event", len(rt) == len(ids), f"{len(rt)} vs {len(ids)}")
check("every rating within 1..10", all(1 <= r["rating"] <= 10 for r in rt))
check("ratings sorted high to low", all(a["rating"] >= b["rating"] for a, b in zip(rt, rt[1:])))
check("goalkeepers have no shots rated", all(not (r["saves"] and r["shots"]) for r in rt))
check("goals in ratings equal match goals", sum(r["goals"] for r in rt) == len(goals))

# search
r = P.search("Di Maria")
kinds = {x["kind"] for x in r}
check("search 'Di Maria' (no accent) finds lines, notes and a moment", kinds == {"line", "note", "moment"}, str(sorted(kinds)))
check("search is case-insensitive", len(P.search("DI MARIA")) == len(r))

# status
st = P.status()
check("status has the dashboard shape", set(st) == {"key", "title", "state", "summary", "facts"} and st["key"] == "products"
      and len(st["facts"]) <= 8 and bool(json.dumps(st)))

print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
