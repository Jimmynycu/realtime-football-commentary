"""Harness base: every model call goes through here, so HTTP, timeout, retries, the check loop, the fallback and the
trace record live in one place and not inside each caller.

A caller describes a call as a Spec and gets back (text, verdict). Verdicts:
    "ok"               an attempt passed every check
    "spare:<reason>"   no attempt passed, but the first one that failed only a soft check is kept (e.g. a stale opening)
    "fallback:<reason>" every attempt was rejected; text is the spec's fallback
    "error:<Name>"     the model errored or timed out (not retried: a dead server will not answer the second time either)
Every run appends one JSON line to TRACE; summary() and status() read it back, so quality and latency are measured, not felt.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import engine

TRACE = engine.ROOT / "runs" / "traces.jsonl"  # tests point this at a temporary file


class ModelError(Exception):
    """A nested model call failed; args[0] is the verdict ('error:<Name>') so the outer run reports the same one."""


@dataclass
class Spec:
    name: str
    system: str
    shots: list = field(default_factory=list)  # (user, assistant) pairs
    max_tokens: int = 70
    temperature: float = 0.8
    attempts: int = 3
    checks: list[Callable[[str], str]] = field(default_factory=list)  # each: text -> "" (pass) or a short reason
    fallback: str = ""
    soft: tuple = ()  # reasons that leave the text usable as a spare when no attempt is clean
    timeout: float = 20.0


def call(spec: Spec, user: str, url: str, model: str) -> str:
    """One chat-completions request to an OpenAI-compatible endpoint."""
    msgs = [{"role": "system", "content": spec.system}] + [{"role": r, "content": c} for u, a in spec.shots
                                                          for r, c in (("user", u), ("assistant", a))]
    body = {"model": model, "max_tokens": spec.max_tokens, "temperature": spec.temperature,
            "messages": msgs + [{"role": "user", "content": user}]}
    key = os.environ.get("LLM_API_KEY", "")
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    req = urllib.request.Request(f"{url}/chat/completions", json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=spec.timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip().strip('"')


def _trace(rec: dict) -> None:
    try:  # a trace failure must never break a call
        TRACE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(spec: Spec, user: str, url: str | None = None, model: str | None = None,
        on_try: Callable[[str], None] | None = None) -> tuple[str, str]:
    """Ask, check, retry up to spec.attempts, fall back. on_try gets each attempt's reason ('' when clean)."""
    if url is None:
        url, model = engine.llm_config()
    t0, tries, text, verdict, spare = time.perf_counter(), [], spec.fallback, "", None
    for _ in range(spec.attempts):
        t1, why = time.perf_counter(), ""
        try:
            out = call(spec, user, url, model or "")
            why = next((r for r in (c(out) for c in spec.checks) if r), "")
        except Exception as ex:
            verdict = ex.args[0] if isinstance(ex, ModelError) else "error:" + type(ex).__name__
            tries.append({"ms": round((time.perf_counter() - t1) * 1000), "verdict": verdict})
            break
        tries.append({"ms": round((time.perf_counter() - t1) * 1000), "verdict": why or "ok"})
        if on_try:
            on_try(why)
        if not why:
            text, verdict = out, "ok"
            break
        if spare is None and why in spec.soft:
            spare = (out, why)
        verdict = "fallback:" + why
    else:
        if spare:
            text, verdict = spare[0], "spare:" + spare[1]
    _trace({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "spec": spec.name, "model": model,
            "attempts": tries, "source": "model" if verdict == "ok" or verdict.startswith("spare") else "fallback",
            "total_ms": round((time.perf_counter() - t0) * 1000)})
    return text, verdict


def _pct(xs: list, p: float) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def summary(path: Path | None = None) -> dict:
    """Per spec name: calls, share accepted on the first attempt, share that fell back, rejection reasons,
    latency p50/p95 over single model attempts (ms)."""
    recs = []
    try:
        for line in (path or TRACE).read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass  # ponytail: a half-written line is skipped; ceiling is a file that fits in memory (rotate past ~100 MB)
    except OSError:
        return {}
    out = {}
    for name in sorted({r["spec"] for r in recs}):
        rs = [r for r in recs if r["spec"] == name]
        reasons: dict[str, int] = {}
        for r in rs:
            for a in r["attempts"]:
                if a["verdict"] != "ok":
                    reasons[a["verdict"]] = reasons.get(a["verdict"], 0) + 1
        lat = [a["ms"] for r in rs for a in r["attempts"]]
        out[name] = {"calls": len(rs),
                     "first_try": round(sum(r["attempts"][0]["verdict"] == "ok" for r in rs) / len(rs), 3),
                     "fallback": round(sum(r["source"] == "fallback" for r in rs) / len(rs), 3),
                     "reasons": reasons, "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95)}
    return out


def status() -> dict:
    """Card for the dashboard."""
    s = summary()
    if not s:
        return {"key": "harness", "title": "Harness base", "state": "no traces", "facts": [],
                "summary": "No model call has been traced yet (runs/traces.jsonl)."}
    top = min(s, key=lambda n: s[n]["first_try"])  # the spec that rejects most is the one worth reading
    t, calls = s[top], sum(v["calls"] for v in s.values())
    facts = [["calls traced", str(calls)]]
    for name, v in list(s.items())[:3]:
        facts += [[f"{name}: first try", f"{v['first_try']:.0%}"], [f"{name}: fell back", f"{v['fallback']:.0%}"]]
    facts.append([f"{top}: latency p50 / p95", f"{t['p50_ms']} / {t['p95_ms']} ms"])
    return {"key": "harness", "title": "Harness base", "state": "tracing",
            "summary": f"{top}: {t['first_try']:.0%} accepted on the first attempt, {t['fallback']:.0%} fell back "
                       f"({t['calls']} calls, p95 {t['p95_ms']} ms).", "facts": facts[:8]}
