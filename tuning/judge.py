"""Model comparison on the same-meaning judge: does a rewrite still MEAN what the draft says?

The lexical checks in evaluate.py cannot tell models apart (untuned 1.5B 27/30, 7B 24/30), and they are blind to the
failure that matters, a fluent rewrite that changes the meaning. The same-meaning judge needs the 7B, which cannot share
the 8 GB GPU with another model, so this runs in two steps:

    python tuning/judge.py generate --name student-1.5b-base     with the model under test loaded
    python tuning/judge.py judge --name student-1.5b-base        with the 7B loaded

It uses every example (train and val); --val-only restricts it to val. Prompt candidates are compared by prompts.py.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import colour  # noqa: E402
import engine  # noqa: E402
import harness  # noqa: E402
from tuning.evaluate import RESULTS, ask, verdict  # noqa: E402


def examples(val_only: bool) -> list[tuple[str, str]]:
    out = []
    for name in ("val.jsonl",) if val_only else ("train.jsonl", "val.jsonl"):
        for line in (HERE / "data" / name).read_text(encoding="utf-8").splitlines():
            user = json.loads(line)["messages"][1]["content"]
            out.append((user, user.split("Draft: ", 1)[1]))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("generate", "judge"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--val-only", action="store_true")
    a = ap.parse_args()
    url, model = engine.llm_config()
    if not url:
        sys.exit("no language model is running")
    path = HERE / "data" / f"outputs_{a.name}.jsonl"
    if a.step == "generate":
        m = engine.Match()
        rows = [{"draft": d, "out": (o := ask(url, model, u)), "lexical": verdict(m, d, o), "model": model} for u, d in examples(a.val_only)]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        print(f"{a.name}: {sum(not r['lexical'] for r in rows)} of {len(rows)} pass the lexical checks ({model})")
    else:
        if "7b" not in model.lower():
            sys.exit(f"the judge is the 7B model; {model} is loaded")
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        ok = [r for r in rows if not r["lexical"]]
        for r in ok:
            r["same"] = harness.call(colour.JUDGE_SPEC, f"Draft: {r['draft']}\nRewording: {r['out']}", url, model).upper().startswith("SAME")
        same = sum(r["same"] for r in ok)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        res = {"name": a.name + "+judge", "model": rows[0]["model"], "judge": model, "n": len(rows), "lexical_pass": len(ok), "meaning_pass": same,
               "on_air_rate": round(same / len(rows), 3)}
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res) + "\n")
        print(json.dumps(res))
