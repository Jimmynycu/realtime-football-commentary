"""Prompt-tuning status for the control room. Reads files only; never imports the model code."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _lines(path: Path) -> list:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def status() -> dict:
    res = {r["name"]: r for r in _lines(HERE / "results.jsonl")}  # last result per label wins
    try:
        doc = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    t, s = res.get("teacher-7b-base+judge"), res.get("student-1.5b-base+judge")
    ts, ss = res.get("teacher-7b"), res.get("student-1.5b-base")
    facts = []
    if t and s:
        ratio = round(ts["latency_p50"] / ss["latency_p50"], 1) if ts and ss and ss["latency_p50"] else None
        facts.append(["model comparison (on air)", f"7B {t['on_air_rate']:.1%} vs 1.5B {s['on_air_rate']:.1%} of {t['n']}"
                      + (f", 1.5B about {ratio}x faster" if ratio else "")])
    if not doc:
        try:
            h = json.loads((HERE / "prompts.json").read_text(encoding="utf-8")).get("holdout")
            if h:
                facts = facts[:7] + [["held-out drafts", f"{h['active_on_air']} vs baseline {h['baseline_on_air']} of {h['n']}, p {h['p']}"]]
        except Exception:
            pass
        return {"key": "tuning", "title": "Prompt tuning", "state": "not measured",
                "summary": "No prompt candidate has been measured yet (run tuning/prompts.py).", "facts": facts}
    cs = doc["candidates"]
    base, others = cs[0], cs[1:]
    best = max(others, key=lambda c: c["on_air"])
    n, active = doc["n"], doc["active"]
    verdict = "baseline kept" if active == "baseline" else f"{active} replaces the baseline"
    facts += [["drafts", f"{n}, one attempt each"], ["candidates", f"{len(others)} against the baseline"],
              ["baseline on air", f"{base['on_air']}/{n} ({base['on_air'] / n:.1%})"],
              ["best candidate", f"{best['name']} {best['on_air']}/{n}, p {best['p_vs_baseline']}"],
              ["rule", f"higher on air and p < {doc['alpha']} (sign test)"], ["active", active]]
    return {"key": "tuning", "title": "Prompt tuning", "state": f"measured, {verdict}" if active == "baseline" else "measured, prompt replaced",
            "summary": f"{len(others)} candidate prompts on {n} drafts: baseline {base['on_air'] / n:.1%} on air, best "
                       f"({best['name']}) {best['on_air'] / n:.1%}, verdict: {verdict}.", "facts": facts[:8]}
