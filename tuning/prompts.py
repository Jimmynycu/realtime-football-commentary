"""Prompt tuning for the colour re-voicing call: pick the prompt by measurement, like every other knob here.

Candidate 0 is the baseline (colour.SYSTEM, colour.SHOTS, temperature 0.8). Every other candidate changes ONE field and
carries a hypothesis tied to a failure seen in the baseline. All candidates run on the SAME drafts (every row of
data/train.jsonl and val.jsonl), one attempt each, through harness.call with the user message the Writer sends.

A rewrite is "on air" only if all hold, checked in this order:
  1. lexical: evaluate.verdict passes (no new name, number or event; one line; Latin script; 3..28 words);
  2. real rewording: not a near-copy. overlap = |W(out) & W(draft)| / |W(out) | W(draft)| over lower-cased word sets;
     overlap >= COPY (0.9) counts as "copied", so an echo cannot win. ("copied" is counted among lexical passes only.)
  3. meaning: colour.JUDGE_SPEC answers SAME.
Comparison is paired (same drafts): exact two-sided sign test on the discordant pairs (math.comb) against the baseline.
A candidate replaces the baseline only if its on-air count is higher AND p < 0.05. No correction for the number of
candidates tested is applied, so a marginal winner among many is weaker evidence than its p-value says. Round 2 combines
the two best single changes (if both beat the baseline's count) and must pass the same rule.

    python tuning/prompts.py            run the loop (needs the 7B at engine.llm_config()), write prompts.json
    python tuning/prompts.py --check    PASS/FAIL lines, no model
"""
import hashlib
import json
import statistics
import sys
import time
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import colour  # noqa: E402
import engine  # noqa: E402
import game  # noqa: E402
import harness  # noqa: E402
from tuning.build_dataset import TRAIN, VAL, load  # noqa: E402
from tuning.evaluate import RESULTS, verdict  # noqa: E402

OUT = HERE / "prompts.json"
COPY, ALPHA = 0.9, 0.05
BASE_TEMP = 0.8


def _ins(system: str, sentence: str) -> str:
    """Put a sentence before the closing 'Output only...' instruction so that one stays last."""
    tail = " Output only what you would say."
    return system[: -len(tail)] + " " + sentence + tail


# name -> (field, hypothesis, edit(system, shots, temperature) -> (system, shots, temperature))
EDITS = {
    "temp_0.4": ("temperature", "Fewer invented names and claims (name 9, claim 7 per 40) if sampling is less random.",
                 lambda s, h, t: (s, h, 0.4)),
    "no_spin": ("system", "Meaning drift (4 per 40): forbid making a line better or worse for a side than the draft.",
                lambda s, h, t: (_ins(s, "Never make it sound better or worse for either team than the draft does: a neutral statement "
                                         "stays neutral and a score read-out stays a plain read-out, not a judgement."), h, t)),
    "neutral_shots": ("shots", "Neutral and score-read-out drafts get judged or spun; show two re-voiced faithfully.",
                      lambda s, h, t: (s, h + [
                          ("Situation: 30 minutes played, Riverton 0-0 Eastfield.\nDraft: Riverton can put this one into the box.",
                           "Riverton can send this one into the box."),
                          ("Situation: 60 minutes played, Riverton 2-1 Eastfield.\nDraft: It is Riverton 2, Eastfield 1.",
                           "Riverton 2, Eastfield 1 at the moment.")], t)),
    "min_edit": ("system", "Added claims ('for both teams') come from free rewriting; ask for the fewest changed words.",
                 lambda s, h, t: (_ins(s, "Change as few words of the draft as you can; only make it sound like speech."), h, t)),
    "names_subset": ("system", "Name failures (9 per 40): state the rule that names in the line must come from the draft.",
                     lambda s, h, t: (_ins(s, "Every name in your line must also appear in the draft. Example: if the draft names only "
                                              "Ruiz, do not name any other player or the other team."), h, t)),
    "short_16": ("system", "Longer lines have more room to add claims and names; cap them at 16 words.",
                 lambda s, h, t: (s.replace("at most 24 words", "at most 16 words"), h, t)),
}


def _norm(shots) -> list:
    return [[u, a] for u, a in shots]


def build(name: str, parts: list[str]) -> dict:
    """Apply the named edits to the baseline; 'fields' lists what differs, so --check can verify it."""
    s, h, t = colour.SYSTEM, list(colour.SHOTS), BASE_TEMP
    for p in parts:
        s, h, t = EDITS[p][2](s, h, t)
    fields = sorted({EDITS[p][0] for p in parts})
    return {"name": name, "fields": fields, "hypothesis": " + ".join(EDITS[p][1] for p in parts) if parts else
            "Reference: the prompt in colour.py today.", "system": s, "shots": _norm(h), "temperature": t}


def overlap(out: str, draft: str) -> float:
    w = lambda x: set("".join(c if c.isalnum() or c == "'" else " " for c in x.lower()).split())  # noqa: E731
    a, b = w(out), w(draft)
    return len(a & b) / len(a | b) if a | b else 1.0


def sign_p(base: list, cand: list) -> float:
    """Exact two-sided sign test on the pairs where exactly one of the two is on air."""
    b = sum(c and not a for a, c in zip(base, cand))
    c = sum(a and not c for a, c in zip(base, cand))
    n = b + c
    return 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def evaluate(cand: dict, drafts: list, m, url: str, model: str) -> list[dict]:
    """One attempt per draft. Cached in data/prompts_<name>.jsonl, keyed by a hash of the prompt, so a cut run resumes."""
    sig = hashlib.md5(json.dumps([cand["system"], cand["shots"], cand["temperature"], model]).encode()).hexdigest()
    cache = HERE / "data" / f"prompts_{cand['name']}.jsonl"
    if cache.exists():
        rows = load(cache)
        if len(rows) == len(drafts) and all(r["sig"] == sig for r in rows):
            return rows
    spec = harness.Spec("prompt_tuning", cand["system"], [tuple(x) for x in cand["shots"]], max_tokens=70,
                        temperature=cand["temperature"], attempts=1)
    rows = []
    for situation, draft in drafts:
        t0, out, row = time.perf_counter(), "", {"draft": draft, "sig": sig}
        try:
            out = harness.call(spec, f"Situation: {situation}\nDraft: {draft}", url, model)
        except Exception as ex:
            row.update(out="", ms=0, lex="error:" + type(ex).__name__, copied=False, same=False, on_air=False)
            rows.append(row)
            continue
        ms = round((time.perf_counter() - t0) * 1000)
        lex = verdict(m, draft, out)
        copied = not lex and overlap(out, draft) >= COPY
        same = False
        if not lex and not copied:
            same = harness.call(colour.JUDGE_SPEC, f"Draft: {draft}\nRewording: {out}", url, model).upper().startswith("SAME")
        rows.append({**row, "out": out, "ms": ms, "lex": lex, "copied": copied, "same": same, "on_air": same})
    cache.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return rows


def numbers(rows: list) -> dict:
    ok = [r for r in rows if r["out"]]
    openings = [colour.opening(r["out"]) for r in ok]
    return {"on_air": sum(r["on_air"] for r in rows), "lexical_pass": sum(not r["lex"] for r in rows),
            "copied": sum(r["copied"] for r in rows),
            "words_per_line": round(statistics.mean(len(r["out"].split()) for r in ok), 1) if ok else None,
            "distinct_openings": round(len(set(openings)) / len(openings), 3) if ok else None,
            "latency_p50_ms": round(statistics.median(r["ms"] for r in ok)) if ok else None}


def run() -> None:
    url, model = engine.llm_config()
    if not url:
        sys.exit("no model server: start the local 7B first")
    m, _ = game.load()
    drafts = []
    for r in load(TRAIN) + load(VAL):
        user = r["messages"][1]["content"]
        drafts.append((user.split("\nDraft: ", 1)[0].removeprefix("Situation: "), user.split("\nDraft: ", 1)[1]))
    cands = [build("baseline", [])] + [build(n, [n]) for n in EDITS]
    results, rows = {}, {}

    def go(c):
        rows[c["name"]] = evaluate(c, drafts, m, url, model)
        results[c["name"]] = numbers(rows[c["name"]])
        base = [r["on_air"] for r in rows["baseline"]]
        p = sign_p(base, [r["on_air"] for r in rows[c["name"]]])
        results[c["name"]]["p_vs_baseline"] = None if c["name"] == "baseline" else round(p, 4)
        print(f"{c['name']:<22}{results[c['name']]['on_air']:>4}/{len(drafts)}  copied {results[c['name']]['copied']:>3}  "
              f"p {p:.3f}  {results[c['name']]['words_per_line']} w  openings {results[c['name']]['distinct_openings']}  "
              f"p50 {results[c['name']]['latency_p50_ms']} ms", flush=True)

    for c in cands:
        go(c)
    top = sorted(EDITS, key=lambda n: -results[n]["on_air"])[:2]
    if all(results[n]["on_air"] > results["baseline"]["on_air"] for n in top):
        combo = build("+".join(top), top)
        cands.append(combo)
        go(combo)
    base_n = results["baseline"]["on_air"]
    winners = [c for c in cands[1:] if results[c["name"]]["on_air"] > base_n and results[c["name"]]["p_vs_baseline"] < ALPHA]
    best = max(winners, key=lambda c: results[c["name"]]["on_air"]) if winners else None
    act = best or cands[0]
    doc = {"model": model, "date": time.strftime("%Y-%m-%d"), "n": len(drafts), "copy_overlap": COPY, "alpha": ALPHA,
           "candidates": [{**c, **results[c["name"]]} for c in cands],
           "active": best["name"] if best else "baseline",
           "active_prompt": {"system": act["system"], "shots": act["shots"], "temperature": act["temperature"]}}
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    with RESULTS.open("a", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps({"name": "prompt:" + c["name"], "model": model, "n": len(drafts), **results[c["name"]],
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    print("verdict:", f"{best['name']} replaces the baseline" if best else "baseline kept")


def check() -> int:
    tests = []
    try:
        doc = json.loads(OUT.read_text(encoding="utf-8"))
        tests.append(("prompts.json parses", True))
    except (OSError, ValueError):
        print("FAIL prompts.json parses")
        return 1
    cs = {c["name"]: c for c in doc["candidates"]}
    base = doc["candidates"][0]
    tests += [("active candidate exists", doc["active"] == "baseline" or doc["active"] in cs),
              ("candidate 0 equals colour.py constants",
               base["name"] == "baseline" and base["system"] == colour.SYSTEM and base["shots"] == _norm(colour.SHOTS)
               and base["temperature"] == BASE_TEMP)]
    for c in doc["candidates"][1:]:
        diff = sorted(f for f, k in (("system", "system"), ("shots", "shots"), ("temperature", "temperature")) if c[k] != base[k])
        tests.append((f"{c['name']} differs only in {c['fields']}", diff == c["fields"] and bool(c["hypothesis"])))
    act = cs.get(doc["active"], base)
    tests.append(("active_prompt matches the active candidate", doc["active_prompt"] == {k: act[k] for k in doc["active_prompt"]}))
    tests.append(("colour.py uses the active prompt", colour.WRITE_SPEC.system == doc["active_prompt"]["system"]))
    for name, ok in tests:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in tests) else 1


if __name__ == "__main__":
    sys.exit(check()) if "--check" in sys.argv else run()
