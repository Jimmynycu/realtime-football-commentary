"""Score a model on the validation drafts with the checks that need no second model.

One attempt per example, the same prompt the Writer sends (SYSTEM, few-shot SHOTS, then the user turn without the
avoid-opening hints), temperature 0.8. Checks: engine.grounded against the draft, one line in Latin script, 3..28 words.
NOT run: the same-meaning judge. It needs the 7B loaded, and a second model cannot share the 8 GB GPU with the one
under test, so the pass rate here is an upper bound on what would reach the air. Latency is wall-clock per request
and is inflated when other processes use the same server.

    python tuning/evaluate.py --name teacher-7b [--url http://127.0.0.1:8081/v1 --model NAME]
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import colour  # noqa: E402
import engine  # noqa: E402
import game  # noqa: E402
from tuning.build_dataset import VAL, load  # noqa: E402

RESULTS = HERE / "results.jsonl"


def verdict(m, draft: str, out: str) -> str:
    """'' if the line passes, else the reason."""
    ok, why = engine.grounded(out, {"draft": draft}, m.vocab, draft)
    if not ok:
        return why.split(":")[0]
    if "\n" in out or any(ord(ch) > 0x24F and ch not in colour.QUOTES for ch in out):
        return "script"
    return "" if 3 <= len(out.split()) <= 28 else "length"


def ask(url: str, model: str, user: str) -> str:
    """Own request, so a refactor of Writer internals cannot change what is measured."""
    shots = [{"role": r, "content": c} for u, x in colour.SHOTS for r, c in (("user", u), ("assistant", x))]
    msgs = [{"role": "system", "content": colour.SYSTEM}] + shots + [{"role": "user", "content": user}]
    body = {"model": model, "max_tokens": 70, "temperature": 0.8, "messages": msgs}
    req = urllib.request.Request(f"{url}/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip().strip('"')


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--url")
    ap.add_argument("--model")
    a = ap.parse_args()
    url, model = engine.llm_config()
    url, model = a.url or url, a.model or model
    if not url:
        sys.exit("no model server: pass --url/--model or start the local server")
    m, _ = game.load()
    rows = load(VAL)
    fails, lat, words, outs = Counter(), [], [], []
    for r in rows:
        user = r["messages"][1]["content"]
        draft = user.split("\nDraft: ", 1)[1]
        t0 = time.time()
        try:
            out = ask(url, model, user)
        except Exception as ex:
            fails["error:" + type(ex).__name__] += 1
            continue
        lat.append(time.time() - t0)
        why = verdict(m, draft, out)
        fails[why or "ok"] += 1
        words.append(len(out.split()))
        outs.append(colour.opening(out))
    n = len(rows)
    res = {"name": a.name, "model": model, "n": n, "pass": fails["ok"], "pass_rate": round(fails["ok"] / n, 3),
           "failures": {k: v for k, v in fails.items() if k != "ok"},
           "words_per_line": round(statistics.mean(words), 1) if words else None,
           "distinct_openings": round(len(set(outs)) / len(outs), 3) if outs else None,
           "latency_p50": round(pct(lat, 0.5), 2) if lat else None, "latency_p95": round(pct(lat, 0.95), 2) if lat else None,
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
