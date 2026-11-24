"""AI infra: what models exist, what each is for, what is running, what the GPU does, how fast the LLM answers.

    python infra.py          registry, routing, services, GPU
    python infra.py bench    latency benchmark against the running language model
    python infra.py --check  self-check (exit 1 on failure)

Facts here were read from llm_server.py (model choice), vision_server.py (fixed vision files), server.py and
engine.py (cache / templates), not guessed. status() is cheap on purpose: it never runs the benchmark.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
MODELS = ROOT / "models"
BENCH = ROOT / "runs" / "bench.jsonl"
SERVICES = [("language model", 8081, "/v1/models"), ("vision model", 8770, "/"), ("commentary server", 8765, "/meta")]

# Why-per-call routing: the one table the demo can point at.
ROUTING = [
    {"call": "colour line (analyst/play-by-play voice)", "model": "language model (Qwen2.5 GGUF, :8081)",
     "why": "voiced ahead of time (booth.py --write) and served from a cache, so a replay never waits on a model"},
    {"call": "laws-of-the-game event line", "model": "none (template)",
     "why": "rules decide the event; a template states it in 0 ms and cannot hallucinate"},
    {"call": "live re-voice of a draft", "model": "language model (:8081)",
     "why": "optional, guarded, 2.5 s timeout (LLM_TIMEOUT), falls back to the plain draft"},
    {"call": "picture-only mode (no event feed)", "model": "vision model (MiniCPM-V 2.6 + mmproj, :8770)",
     "why": "reads scoreboard/view/shirt from frames as a sensor; lines still come from templates"},
]
VRAM_NOTE = "8 GB VRAM holds the 7B language model OR the vision model, not both: start one server at a time"


def _role(path: Path) -> str:
    """Role from the loaders' own rules: vision_server.py loads fixed files in models/vision/; llm_server.py loads
    $LLM_GGUF or, by default, the largest *.gguf directly in models/."""
    if "vision" in path.relative_to(MODELS).parts[:-1]:
        return "vision: projector for the picture model" if "mmproj" in path.name else "vision: picture model (vision_server.py)"
    top = sorted((f for f in MODELS.glob("*.gguf")), key=lambda f: f.stat().st_size)
    if top and path == top[-1]:
        return "language: default served by llm_server.py (largest in models/; override with LLM_GGUF)"
    return "language: available via LLM_GGUF, not default (smaller)"


def registry() -> list[dict]:
    return [{"file": str(p.relative_to(MODELS)).replace("\\", "/"), "size_mb": round(p.stat().st_size / 2**20),
             "role": _role(p)} for p in sorted(MODELS.rglob("*.gguf"))] if MODELS.is_dir() else []


def _get(port: int, path: str, timeout: float = 0.4):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read(65536)


def services() -> list[dict]:
    out = []
    for name, port, path in SERVICES:
        row = {"name": name, "port": port, "up": False, "detail": ""}
        try:
            code, body = _get(port, path)
            row["up"] = True
            try:
                j = json.loads(body)
                row["detail"] = j["data"][0]["id"] if "data" in j else str(j.get("title") or j.get("match") or "")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                pass
        except urllib.error.HTTPError:
            row["up"] = True  # something answered, just not on this path
        except OSError:
            pass
        out.append(row)
    return out


def gpu() -> dict:
    g = {"name": None, "used_mb": None, "total_mb": None, "util_pct": None}
    try:
        line = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                               "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
        n, u, t, ut = [x.strip() for x in line.splitlines()[0].split(",")]
        g.update(name=n, used_mb=int(u), total_mb=int(t), util_pct=int(ut))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        pass
    return g


PROMPTS = [  # re-voice drafts like the commentary pipeline sends
    "Draft: Shot saved by the keeper after a run down the left.", "Draft: Corner kick for the home side.",
    "Draft: Foul in midfield, free kick to the away team.", "Draft: Goal! The striker finishes from close range.",
    "Draft: The home side keeps the ball and builds from the back.", "Draft: Offside flag goes up, play is stopped.",
    "Draft: Yellow card for a late tackle.", "Draft: Long throw into the box, cleared by the defence."]
SYSTEM = "You are a live TV football play-by-play commentator. Rewrite the DRAFT in one lively sentence."


def bench(n: int = 8) -> dict:
    """Sequential calls, wall latency each. Shared server: another client's request inflates numbers."""
    up = next((s for s in services() if s["port"] == 8081), None)
    if not up or not up["up"]:
        return {"error": "no language model on 127.0.0.1:8081 (start_llm.cmd); benchmark not run"}
    model, lat, toks, est = up["detail"], [], 0, False
    for i in range(n):
        body = json.dumps({"model": model, "max_tokens": 80, "temperature": 0.6, "messages": [
            {"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPTS[i % len(PROMPTS)]}]}).encode()
        req = urllib.request.Request("http://127.0.0.1:8081/v1/chat/completions", body, {"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
        lat.append(time.perf_counter() - t0)
        n_tok = (out.get("usage") or {}).get("completion_tokens")
        if n_tok is None:  # ponytail: ~1.3 tokens per word is a rough English estimate; ceiling is +-30 %
            est, n_tok = True, round(len(out["choices"][0]["message"]["content"].split()) * 1.3)
        toks += n_tok
    lat_s = sorted(lat)
    res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "n": n, "p50_s": round(statistics.median(lat), 3),
           "p95_s": round(lat_s[min(n - 1, int(0.95 * n))], 3), "tokens_per_s": round(toks / sum(lat), 1),
           "tokens_estimated": est}
    BENCH.parent.mkdir(exist_ok=True)
    with BENCH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    return res


def last_bench() -> dict | None:
    try:
        return json.loads(BENCH.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, IndexError, ValueError):
        return None


def status() -> dict:
    reg, svc, g, lb = registry(), services(), gpu(), last_bench()
    running = [s["name"] for s in svc if s["up"]]
    total_gb = sum(m["size_mb"] for m in reg) / 1024
    facts = [["models on disk", f"{len(reg)} files, {total_gb:.1f} GB"],
             ["running", ", ".join(running) or "nothing"],
             ["GPU", g["name"] or "nvidia-smi not available"],
             ["GPU memory", f"{g['used_mb']} / {g['total_mb']} MB, {g['util_pct']}% busy" if g["used_mb"] is not None else "unknown"],
             ["last benchmark", f"p50 {lb['p50_s']} s, p95 {lb['p95_s']} s, {lb['tokens_per_s']} tok/s ({lb['model']})"
              if lb else "none yet (python infra.py bench)"],
             ["VRAM rule", "7B language model OR vision model, not both"]]
    if lb:
        summary = f"Language model answers in {lb['p50_s']} s median ({lb['tokens_per_s']} tok/s) on {g['name'] or 'this machine'}."
    else:
        summary = f"{len(reg)} models on disk ({total_gb:.1f} GB); {len(running)} of {len(svc)} services up; no benchmark yet."
    return {"key": "infra", "title": "AI infra", "state": f"{len(running)} of {len(svc)} up", "summary": summary,
            "facts": facts[:8]}


def report() -> str:
    lines = ["MODELS"] + [f"  {m['file']}  {m['size_mb']} MB  {m['role']}" for m in registry()]
    lines += ["", "ROUTING"] + [f"  {r['call']} -> {r['model']}\n      {r['why']}" for r in ROUTING] + ["  " + VRAM_NOTE]
    lines += ["", "SERVICES"] + [f"  :{s['port']} {s['name']}: {'UP ' + s['detail'] if s['up'] else 'down'}" for s in services()]
    g = gpu()
    lines += ["", "GPU", f"  {g['name']}  {g['used_mb']} / {g['total_mb']} MB  {g['util_pct']}% busy"]
    return "\n".join(lines)


def check() -> int:
    fails = 0

    def t(ok: bool, label: str):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + label)
        fails += not ok

    on_disk = {str(p.relative_to(MODELS)).replace("\\", "/") for p in MODELS.rglob("*.gguf")} if MODELS.is_dir() else set()
    t({m["file"] for m in registry()} == on_disk, f"registry matches {len(on_disk)} gguf files on disk")
    t(bool(ROUTING), "routing table non-empty")
    try:
        t(json.loads(json.dumps(status()))["key"] == "infra", "status() is JSON-serialisable")
    except (TypeError, ValueError) as ex:
        t(False, f"status() JSON: {ex}")
    t0 = time.perf_counter()
    s = status()
    t(time.perf_counter() - t0 < 2 and len(s["facts"]) <= 8, "status() under 2 s, at most 8 facts")
    return 1 if fails else 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--check":
        sys.exit(check())
    print(json.dumps(bench(), indent=2) if arg == "bench" else report())
