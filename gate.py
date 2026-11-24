"""Release gate: may the current state ship? Runs both check suites, then holds the booth scorecard to fixed limits.

    python gate.py [--json]     exit 0 only if every gate passes; every run is appended to runs/gates.jsonl

Needs neither the broadcast transcript (the human numbers fall back to data/human_timing.json) nor a language model, so
the same command runs locally and in CI (.github/workflows/eval.yml). status() feeds the dashboard from the log alone.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "runs" / "gates.jsonl"
SUITES = ["eval.py", "eval_colour.py"]

# Limits, one line of reasoning each. Current numbers: wpm 73.3 vs 72.3, npm 6.4 vs 6.0, 6 of 7 phases, longest 19.9 s.
WPM_TOL = 0.05        # how much the booth talks: a human feels a 5% shift in pace; today 1.4%
NPM_TOL = 0.10        # how often a player is named; names drift more than pace; today 6.6%
PHASE_TOL = 0.10      # per-phase pace; the tiny "chance" phase (1% of play) is allowed to miss, so 5 of 7 is the bar
PHASES_MIN = 5        # today 6 of 7
SILENCE_MAX = 30.0    # seconds: dead air longer than this is a broken pipeline, not style; today 19.9
LONG_PAUSE_X = 2.0    # pauses over 10 s per minute, as a multiple of the human's; today 1.5x
NUMBER_MAX = 30       # percent of colour lines with a number: stat-reading is the tell of a bot; today 22


def run_suite(script: str) -> tuple[int, int, int]:
    """(pass, fail, exit code). Lines are counted by prefix, the same rule control.py uses for the dashboard."""
    p = subprocess.run([sys.executable, "-u", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    lines = p.stdout.splitlines()
    return sum(x.startswith("PASS") for x in lines), sum(x.startswith("FAIL") for x in lines), p.returncode


def gates() -> list[dict]:
    out = []

    def add(name, value, limit, ok):
        out.append({"name": name, "value": value, "limit": limit, "ok": bool(ok)})

    for s in SUITES:
        n_pass, n_fail, code = run_suite(s)
        add(f"{s} checks", f"{n_pass} pass, {n_fail} fail, exit {code}", "0 fail, exit 0", n_fail == 0 and code == 0 and n_pass > 0)

    import control, game  # after the suites: they load the match themselves, and this keeps a data error from hiding them
    m, frames = game.load()
    sc = control.Control(m, frames).scorecard()
    (mw, hw), (mn, hn) = sc["whole"]["wpm"], sc["whole"]["npm"]
    add("whole-match words per minute", f"{mw:.1f} vs human {hw:.1f}", f"within {WPM_TOL:.0%}", abs(mw / hw - 1) <= WPM_TOL)
    add("names per minute", f"{mn:.2f} vs human {hn:.2f}", f"within {NPM_TOL:.0%}", abs(mn / hn - 1) <= NPM_TOL)
    near = sum(abs(p["wpm"][0] / p["wpm"][1] - 1) <= PHASE_TOL for p in sc["phases"])
    add("phases within 10% on words per minute", f"{near} of {len(sc['phases'])}", f"at least {PHASES_MIN}", near >= PHASES_MIN)
    rhythm = {r[0]: r for r in sc["rhythm"]}
    longest = rhythm["Longest pause"][1]
    add("longest silence", f"{longest:.1f} s", f"at most {SILENCE_MAX:.0f} s", longest <= SILENCE_MAX)
    _, mine10, ref10, _ = rhythm["Pauses over 10 s, per minute"]
    add("pauses over 10 s per minute", f"{mine10:.2f} vs human {ref10:.2f}", f"at most {LONG_PAUSE_X:g}x human", mine10 <= LONG_PAUSE_X * ref10)
    number = {r[0]: r for r in sc["texture"]}["Lines containing a number"][1]
    add("colour lines containing a number", f"{number}%", f"at most {NUMBER_MAX}%", number <= NUMBER_MAX)
    return out


def commit() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except OSError:
        return None


def status() -> dict:
    """Dashboard card from the log only; never runs the gate."""
    rows = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines() if x.strip()] if LOG.exists() else []
    if not rows:
        return {"key": "workflow", "title": "AI R&D workflow", "state": "never run",
                "summary": "The release gate has never been run: python gate.py.", "facts": [["Gate runs logged", "0"]]}
    last = rows[-1]
    g = last["gates"]
    failed = [x["name"] for x in g if not x["ok"]]
    passed = len(g) - len(failed)
    state = "passing" if last["verdict"] == "PASS" else "failing"
    facts = [["Last verdict", last["verdict"]], ["Gates passed", f"{passed} of {len(g)}"], ["When (UTC)", last["time"][:16].replace("T", " ")],
             ["Commit", last["commit"] or "none"], ["Runs logged", str(len(rows))],
             ["Failing", ", ".join(failed) if failed else "none"]]
    facts += [[x["name"], f"{x['value']} (limit {x['limit']})"] for x in g if x["name"] in ("whole-match words per minute", "longest silence")]
    return {"key": "workflow", "title": "AI R&D workflow", "state": state,
            "summary": f"Last gate run: {last['verdict']}, {passed} of {len(g)} gates passed" + (f"; failing: {failed[0]}" if failed else "") + ".",
            "facts": facts[:8]}


def main() -> int:
    g = gates()
    ok = all(x["ok"] for x in g)
    verdict = "PASS" if ok else "FAIL"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now, "commit": commit(), "verdict": verdict, "gates": g}) + "\n")
    if "--json" in sys.argv:
        print(json.dumps({"time": now, "verdict": verdict, "gates": g}, indent=2))
    else:
        for x in g:
            print(f"{'PASS' if x['ok'] else 'FAIL'}  {x['name']}: {x['value']} (limit {x['limit']})")
        print(f"{verdict}  {sum(x['ok'] for x in g)} of {len(g)} gates passed" + ("" if ok else ": do not ship"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
