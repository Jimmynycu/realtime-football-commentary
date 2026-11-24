"""Offline eval: replay the whole match in virtual time and check the pipeline.

    python eval.py [speed]        deterministic checks (template commentator, ~1 s); exits 1 if any check fails
    python eval.py --llm          also run 120 real jobs through the configured LLM: latency, guardrail verdicts
"""
import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine

args = [a for a in sys.argv[1:] if not a.startswith("--")]
speed = float(args[0]) if args else 8.0
m = engine.Match()
comm = engine.Commentator(m)
comm.url = ""  # the checks below must be deterministic: template commentator only
lines, st, d = [], None, None
for e, st, now, jobs, d in engine.play(m, speed):
    for j in jobs:
        text, source, ms = comm.line(j)
        lines.append({"now": now, "job": j, "text": text, "source": source, "ms": ms})

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


# 1. State reducer vs ground truth from the match metadata (not from the events).
check("final score matches ground truth", tuple(st.score) == m.truth, f"{st.scoreline()} vs {m.truth}")
check("shootout tally", st.shootout == [4, 2], str(st.shootout))

# 2. Coverage: every goal gets a line, at the moment it happens, naming the scorer.
goals = [e for e in m.events if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal"]
goal_lines = [l for l in lines if l["job"]["kind"] in ("goal", "pen_goal")]
named = all(engine.fold(m.name(g["player"]).split()[-1]) in engine.fold(l["text"]) for g, l in zip(goals, goal_lines))
check("every goal has a line naming the scorer", len(goal_lines) == len(goals) == sum(m.truth) and named,
      f"{len(goal_lines)}/{len(goals)}")
check("last goal line carries the right score", f"{m.truth[0]}-{m.truth[1]}" in goal_lines[-1]["text"], goal_lines[-1]["text"])

# 3. Grounding: all emitted lines pass the guardrail, and the guardrail rejects planted hallucinations.
bad = [l["text"] for l in lines if not engine.grounded(l["text"], l["job"]["facts"], m.vocab, engine.render(l["job"]))[0]]
check("all emitted lines are grounded in their facts", not bad, f"{len(lines) - len(bad)}/{len(lines)} {bad[:2]}")
f, draft0 = goal_lines[0]["job"]["facts"], engine.render(goal_lines[0]["job"])  # Messi's penalty


def g(text, job=None):
    return engine.grounded(text, job["facts"] if job else f, m.vocab, engine.render(job) if job else draft0)


check("guardrail rejects wrong scorer", g("GOAL! Kylian Mbappe scores!") == (False, "name:kylian"))
check("guardrail rejects invented number", g("Lionel Messi scores from 35 yards!") == (False, "number:35"))
check("guardrail rejects a name from outside the match", g("Messi scores, just like Ronaldo would!") == (False, "proper:Ronaldo"))
check("guardrail accepts a faithful paraphrase", g("Messi buries the penalty! Argentina 1-0 France.")[0])
p = next(l["job"] for l in lines if l["job"]["kind"] == "buildup")  # an ordinary pass
names = (p["facts"]["player"], p["facts"]["recipient"])
check("guardrail rejects a wrong event verb", g("%s intercepts and sets up %s." % names, p) == (False, "claim:intercept"))
check("guardrail rejects an invented outcome", g("%s plays it to %s, who scores!" % names, p) == (False, "claim:goal"))

# 4. Airtime budget: ordinary lines never closer than GAP seconds of wall time.
gaps = [b["now"] - a["now"] for a, b in zip(lines, lines[1:]) if b["job"]["sal"] < engine.Director.PRIORITY]
check("airtime budget respected", min(gaps) >= engine.Director.GAP - 1e-6, f"min gap {min(gaps):.2f}s")

# 5. LLM path against a scripted OpenAI-compatible mock: faithful answer, hallucination, timeout, HTTP 500.
replies = iter(["Messi buries the penalty! Argentina 1-0 France.", "Kylian Mbappe scores the penalty!", "slow", "500"])


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        reply = next(replies)
        if reply == "slow":
            time.sleep(0.6)
        body = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        try:
            self.send_response(500 if reply == "500" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # client already timed out
            pass

    def log_message(self, *_):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
c = engine.Commentator(m)
c.url, c.model, c.timeout = f"http://127.0.0.1:{mock.server_port}/v1", "mock", 0.3
job, tmpl = goal_lines[0]["job"], engine.render(goal_lines[0]["job"])
got = [c.line(job) for _ in range(4)]
check("LLM faithful line is used", got[0][1] == "llm" and "buries" in got[0][0], got[0][1])
check("LLM hallucination -> template fallback", got[1][0] == tmpl and got[1][1].startswith("fallback:name:"), got[1][1])
check("LLM timeout -> template fallback within budget", got[2][0] == tmpl and got[2][1].startswith("fallback") and got[2][2] < 500, f"{got[2][1]} {got[2][2]} ms")
check("LLM HTTP error -> template fallback", got[3][0] == tmpl and got[3][1] == "fallback:HTTPError", got[3][1])
mock.shutdown()

# 6. Optional: the real LLM on a fixed sample (every 3rd play-by-play job + all analyst jobs, first 120).
if "--llm" in sys.argv:
    real = engine.Commentator(m)
    sample = ([l["job"] for l in lines if l["job"]["voice"] == "pbp"][::3] + [l["job"] for l in lines if l["job"]["voice"] == "analyst"])[:120]
    if not real.url:
        print("\n--llm: no LLM configured (run start_llm.cmd or set LLM_BASE_URL)")
    else:
        real.line(sample[0])  # warm-up
        out = [(j, *real.line(j)) for j in sample]
        lat = sorted(o[3] for o in out)
        why = {}
        for o in out:
            key = o[2] if o[2] == "llm" else o[2].split(":")[1]
            why[key] = why.get(key, 0) + 1
        used = why.get("llm", 0)
        print(f"\nLLM {real.model}: {used}/{len(out)} lines on air ({100 * used // len(out)}%), rest fell back to the draft | "
              f"p50 {statistics.median(lat)} ms, p95 {lat[int(len(lat) * 0.95)]} ms, max {lat[-1]} ms | verdicts {why}")
        check("LLM p95 latency within the 2.5 s budget", lat[int(len(lat) * 0.95)] < 2500)
        for j, text, source, _ in out[:6] + [o for o in out if o[0]["sal"] >= 9][:5] + [o for o in out if o[0]["voice"] == "analyst"][:3]:
            print(f'  [{source}] {text}')

# Report
wall_min = lines[-1]["now"] / 60
by_voice = {v: sum(l["job"]["voice"] == v for l in lines) for v in ("pbp", "analyst")}
src = {}
for l in lines:
    src[l["source"].split(":")[0]] = src.get(l["source"].split(":")[0], 0) + 1
ms = sorted(l["ms"] for l in lines)
print(f"\nspeed {speed}x | {len(m.events)} events -> {len(lines)} lines ({by_voice}) | "
      f"{len(lines) / wall_min:.1f} lines/min | superseded {d.dropped}")
print(f"sources {src} | latency p50 {statistics.median(ms)} ms, p95 {ms[int(len(ms) * 0.95)]} ms")
print("sample:", *[f'  {l["job"]["facts"]["minute"]:>3}\' [{l["job"]["voice"]}] {l["text"]}'
                   for l in lines if l["job"]["sal"] >= 9 or l["job"]["voice"] == "analyst"][:14], sep="\n")
sys.exit(0 if all(checks) else 1)
