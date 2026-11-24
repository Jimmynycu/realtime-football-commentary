"""Does the tuned prompt hold up on drafts it was not chosen on?

prompts.py picks a winner on 173 drafts and tests seven candidates on them, so its winner's score is optimistic twice
over: chosen after looking, and judged on the set it was chosen on. This checks the winner against the baseline on the
drafts that were NOT in that set: the ones the system sends to the model but whose cached rewrite is the plain draft,
because the baseline prompt failed all three attempts on them. They are the hard cases.

    python tuning/confirm.py        needs the 7B loaded; writes the verdict into prompts.json and results.jsonl
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import booth  # noqa: E402
import engine  # noqa: E402
import game  # noqa: E402
from tuning.build_dataset import Recording, TRAIN, VAL  # noqa: E402
from tuning.prompts import evaluate, numbers, sign_p  # noqa: E402

if __name__ == "__main__":
    url, model = engine.llm_config()
    if not url:
        sys.exit("no model server: start the local 7B first")
    m, frames = game.load()
    w = Recording(m)
    b = booth.Booth(m, frames, booth.load_params(), w)
    for e in m.events:
        if e["period"] >= 5:
            break
        b.feed(e)
    used = {json.loads(x)["messages"][1]["content"].split("\nDraft: ", 1)[1] for f in (TRAIN, VAL) for x in f.read_text(encoding="utf-8").splitlines()}
    drafts, seen = [], set()
    for call in w.calls:
        draft, situation = (call["draft"], call["situation"]) if isinstance(call, dict) else (call[0], call[1])
        if draft not in used and draft not in seen and draft not in b.colour.verbatim:
            seen.add(draft)
            drafts.append((situation, draft))
    cfg = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))
    cands = {c["name"]: c for c in cfg["candidates"]}
    base, best = dict(cands["baseline"], name="holdout_baseline"), dict(cands[cfg["active"]], name="holdout_active")
    rb, ra = evaluate(base, drafts, m, url, model), evaluate(best, drafts, m, url, model)
    nb, na = numbers(rb), numbers(ra)
    p = sign_p([r["on_air"] for r in rb], [r["on_air"] for r in ra])
    out = {"name": "holdout:" + cfg["active"], "model": model, "n": len(drafts), "baseline_on_air": nb["on_air"], "active_on_air": na["on_air"],
           "baseline_copied": nb["copied"], "active_copied": na["copied"], "p": round(p, 4), "confirmed": na["on_air"] > nb["on_air"] and p < 0.05,
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    cfg["holdout"] = out
    (HERE / "prompts.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    with (HERE / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    print(json.dumps(out, indent=1))
