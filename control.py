"""The control room behind /dashboard: what data the system is running on, the levers that rebuild it, and the evals.

Two halves, one object:
  data   every input and derived file (where it comes from, how fresh, whether it leaves this machine), the preparation
         sheet (the one human-written input), and the lines that are on air right now, next to the human's
  eval   the scorecard against the human commentator, and the check suites, run from here and logged to runs/control.jsonl

Local only. Jobs come from a fixed list; nothing a browser sends is ever turned into a command.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import booth
import engine

ROOT, DATA = engine.ROOT, engine.DATA
LOG = ROOT / "runs" / "control.jsonl"

# name -> (label, what it proves, argv, what it needs, typical seconds)
JOBS = {
    "eval": ("Pipeline correctness", "Final score equals ground truth, every goal is called with the right scorer, the guardrail "
             "rejects fabricated names, numbers and events, and a hallucinating, slow or failing model never reaches the air.",
             ["eval.py"], None, 2),
    "eval_colour": ("Colour regressions", "Five mistakes that once went out, each found by reading the booth next to the human "
                    "transcript: a substituted player called quiet, a stale verdict after a goal, the score every minute.",
                    ["eval_colour.py"], None, 8),
    "eval_game": ("Football model signals", "How the match splits into phases, and whether the model's 'this matters' signals "
                  "really come before shots (the free-man signal does not: 14% against a 13% baseline).",
                  ["eval_game.py"], "transcript", 10),
    "eval_human": ("Human baseline", "The measuring stick itself: how fast the human names a player after the touch, how much "
                   "of the play is called, how quickly a goal is called.", ["eval_human.py"], "transcript", 5),
    "eval_vision": ("Vision mode vs answer key", "Picture-only mode scored against the event data it never sees: scoreboard "
                    "reads, goals, names, delay.", ["eval_vision.py"], "traces", 5),
    "eval_llm": ("Real model, 86 jobs", "The same pipeline with the local model writing: latency and how many lines the "
                 "guardrail lets through.", ["eval.py", "--llm"], "llm", 90),
    "tune": ("Re-tune to the human", "Replays the match, compares each phase with the human, nudges that phase's knobs, "
             "fourteen rounds. Writes booth_params.json.", ["booth.py"], None, 90),
    "write": ("Voice colour with the model", "Sends every colour draft the cache does not have to the model, three checks "
              "each, then re-tunes on what it wrote.", ["booth.py", "--write"], "llm", 600),
}
JOBS.update({
    "eval_harness": ("Harness checks", "A faithful answer goes through; a wrong one is retried and then replaced by the fallback; a timeout "
                     "and a server error fall back; every call leaves one trace.", ["eval_harness.py"], None, 15),
    "eval_products": ("Product checks", "The subtitle track is in order and inside the video, the highlights hold every goal, the report "
                      "states the true score and every scorer, every player has a rating.", ["eval_products.py"], None, 10),
    "gate": ("Release gate", "The decision to ship, as one command: the check suites plus thresholds on the scorecard against the "
             "human. The same command runs on every push.", ["gate.py"], None, 15),
    "bench": ("Benchmark the model", "Latency and tokens per second of the language model on this machine.", ["infra.py", "bench"], "llm", 10),
    "products": ("Build the products", "Subtitle track, highlights, match report and report card, from the same data.", ["products.py", "all"], None, 10),
    "dataset": ("Build the tuning set", "Collects the drafts the model is asked to re-voice, the fixed set every prompt is measured on.",
                ["tuning/build_dataset.py"], None, 10),
    "prompts": ("Tune the prompt", "Tries each candidate prompt on the same drafts, one attempt each, and keeps the baseline unless a "
                "candidate puts significantly more lines on air (paired sign test).", ["tuning/prompts.py"], "llm", 1500),
})
SUITES = ["eval", "eval_colour", "eval_harness", "eval_products", "eval_game", "eval_human", "eval_vision", "eval_llm"]
# The parts of a complete system: the production line first, then what it turns out. key, title, the name the part
# usually goes by in Chinese, group, jobs that can be started from its tile.
PARTS = [("eval", "Eval platform", "评测中台", "line", ["eval", "eval_colour"]), ("data", "Data platform", "数据中台", "line", []),
         ("tuning", "Prompt tuning", "微调（提示词）", "line", ["dataset", "prompts"]), ("infra", "AI infra", "AI Infra", "line", ["bench"]),
         ("harness", "Harness base", "Harness 底座", "line", ["eval_harness"]), ("workflow", "AI R&D workflow", "AI 研发流程", "line", ["gate"]),
         ("live", "Live commentary", "实时解说", "product", []), ("media", "Match report and social posts", "自媒体 Agent", "product", ["products"]),
         ("assess", "Player report card", "考核", "product", ["products"]), ("assets", "Asset library", "素材管理", "product", []),
         ("video", "Highlights and subtitle track", "视频处理 Agent", "product", ["products"])]


def stamp(path) -> dict:
    st = path.stat() if path.exists() else None
    return {"path": str(path.relative_to(ROOT)).replace("\\", "/"), "exists": bool(st), "bytes": st.st_size if st else 0,
            "mtime": st.st_mtime if st else None}


def up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.4):
            return True
    except Exception:
        return False


class Counting(booth.Writer):
    """The cache also holds drafts the model failed to improve; count the lines that go out in the model's words."""
    voiced = 0

    def write(self, draft, situation, variant=0):
        out = super().write(draft, situation, variant)
        self.voiced += out != draft
        return out


class Control:
    def __init__(self, m: engine.Match, frames: dict):
        self.m, self.frames = m, frames
        self.lock, self.job = threading.Lock(), None
        self.voiced, self.lines, self.segs, self._score, self.model = True, [], [], None, 0
        if frames:
            self.rebuild(True)

    # ------------------------------------------------------------------ data

    def rebuild(self, voiced: bool) -> dict:
        """Run the booth over the match again with the current knobs, notes and cache. New viewers get the new lines."""
        t0 = time.time()
        writer = Counting(self.m, live=False) if voiced else None
        lines, segs = booth.run(self.m, self.frames, booth.load_params(), writer)
        self.voiced, self.lines, self.segs, self._score, self.model = voiced, lines, segs, None, writer.voiced if writer else 0
        return {"lines": len(lines), "seconds": round(time.time() - t0, 1), "voiced": voiced}

    def clock(self, t: float) -> str:
        p = max(q for q in self.m.period_start if self.m.period_start[q] <= t)
        s = int(t - self.m.period_start[p]) + {1: 0, 2: 45, 3: 90, 4: 105}.get(p, 120) * 60
        return f"{s // 60:02d}:{s % 60:02d}"

    def data(self) -> dict:
        mid = engine.MATCH_ID
        cache = booth.Writer(self.m, live=False).cache
        notes = json.loads((ROOT / "prep_notes.json").read_text(encoding="utf-8"))["notes"]
        transcript = booth.CAPTIONS.exists()
        traces = sorted((ROOT / "runs").glob("vision-*.jsonl"))
        models = sorted((ROOT / "models").rglob("*.gguf")) if (ROOT / "models").exists() else []
        fill = [l for l in self.lines if l["kind"] == "fill"]
        sets = [
            ("events", "Match events", "Every on-ball action with its place and time. The stand-in for perfect eyes, and the answer key for every eval.",
             "StatsBomb open data, fetched on first run", "fetched", DATA / f"events_{mid}.json", len(self.m.events), "events"),
            ("lineups", "Team sheets", "Who can be on the pitch; the only names the system is allowed to say.",
             "StatsBomb open data, fetched on first run", "fetched", DATA / f"lineups_{mid}.json", len(self.m.names), "players"),
            ("frames", "Player positions", "Where the players around the ball are standing; the free-man picture comes from here.",
             "StatsBomb 360, fetched on first run", "fetched", DATA / f"three-sixty_{mid}.json", len(self.frames), "frames"),
            ("video", "Video calibration", "The video second at which each period kicks off. Makes the video the only clock.",
             "measured by hand on the broadcast", "in repo", ROOT / "video.json", len(self.m.period_start), "periods"),
            ("human", "Human commentator", "The measuring stick: when the human spoke and when a player was named."
             + (" The transcript is on this machine." if transcript else " Timing only; the transcript is not redistributed."),
             "broadcast captions; only timing and name flags are published", "local only" if transcript else "in repo",
             booth.CAPTIONS if transcript else booth.TIMING, len(booth.human_words(self.m)), "words"),
            ("notes", "Preparation sheet", "Background a commentator brings to the match. The one human-written input; said verbatim.",
             "written by hand, editable below", "in repo", ROOT / "prep_notes.json", len(notes), "notes"),
            ("params", "Tuned knobs", "How often each voice speaks in each phase. Set by the tuning loop, never by hand.",
             "written by Re-tune", "in repo", ROOT / "booth_params.json", len(booth.load_params()), "phases"),
            ("cache", "Model-voiced lines", "Colour drafts re-voiced by the language model and passed by all three checks.",
             "written by Voice colour with the model", "in repo", DATA / "colour_cache.json", len(cache), "lines"),
        ]
        out = [{"key": k, "label": lab, "role": role, "source": src, "public": pub, "n": n, "unit": unit, **stamp(path)}
               for k, lab, role, src, pub, path, n, unit in sets]
        out.append({"key": "traces", "label": "Vision traces", "role": "What the picture-only mode saw and said, frame by frame; input to the vision eval.",
                    "source": "written by vision_server.py", "public": "local only", "n": len(traces), "unit": "runs",
                    **(stamp(traces[-1]) if traces else {"path": "runs/", "exists": False, "bytes": 0, "mtime": None})})
        out.append({"key": "models", "label": "Local models", "role": "Language and vision models, run on this machine's GPU.",
                    "source": "downloaded once", "public": "local only", "n": len(models), "unit": "files", "path": "models/",
                    "exists": bool(models), "bytes": sum(p.stat().st_size for p in models), "mtime": max((p.stat().st_mtime for p in models), default=None)})
        url, model = engine.llm_config()
        return {"match": {"title": self.m.title, "home": self.m.home, "away": self.m.away, "id": mid},
                "sets": out,
                "services": [{"name": "Language model", "up": bool(url), "detail": model or "not running: colour goes out from the cache, new drafts go out plain"},
                             {"name": "Vision model", "up": up("http://127.0.0.1:8770/"), "detail": "port 8770, picture-only mode"}],
                "booth": {"lines": len(self.lines), "play": len(self.lines) - len(fill), "colour": len(fill),
                          "model": self.model, "voiced": self.voiced},
                "params": booth.load_params(), "transcript": transcript, "abouts": ["match", self.m.home, self.m.away, *sorted(set(self.m.names.values()))]}

    def notes(self) -> list[dict]:
        return json.loads((ROOT / "prep_notes.json").read_text(encoding="utf-8"))["notes"]

    def edit_notes(self, add: dict | None = None, remove: int | None = None) -> str | None:
        """Add or remove one preparation note. Returns an error message, or None. Takes effect on the next rebuild."""
        path = ROOT / "prep_notes.json"
        prep = json.loads(path.read_text(encoding="utf-8"))
        if add is not None:
            about, text = str(add.get("about", "")), " ".join(str(add.get("text", "")).split())
            if about not in ("match", self.m.home, self.m.away, *self.m.names.values()):
                return "about must be 'match', a team, or a player on the team sheet"
            if not 3 <= len(text.split()) <= 40 or len(text) > 300 or not text.endswith((".", "!", "?")):
                return "a note is one or two sentences: 3 to 40 words, ending with a full stop"
            if any(n["text"] == text for n in prep["notes"]):
                return "that note is already on the sheet"
            prep["notes"].append({"about": about, "text": text})
        elif remove is not None:
            if not (isinstance(remove, int) and 0 <= remove < len(prep["notes"])):
                return "no such note"
            del prep["notes"][remove]
        path.write_text(json.dumps(prep, ensure_ascii=False, indent=1), encoding="utf-8")
        return None

    def window(self, a: float, b: float) -> dict:
        """What is on air between two match seconds, and what the human said in the same window (local transcript only)."""
        rows = [{"t": l["t"], "clock": self.clock(l["t"]), "who": "colour" if l["kind"] == "fill" else "play", "kind": l["kind"],
                 "text": " ".join(l["words"]) + (" —" if len(l["words"]) < l["n"] else ""), "cut": len(l["words"]) < l["n"], "phase": l["phase"]}
                for l in self.lines if a <= l["t"] <= b]
        if booth.CAPTIONS.exists():
            cur = None
            for t, w in booth.human_words(self.m):
                if not a <= t <= b:
                    continue
                if cur and t - cur["end"] <= 1.0:
                    cur["text"] += " " + w
                    cur["end"] = t
                else:
                    cur = {"t": t, "end": t, "clock": self.clock(t), "who": "human", "kind": "", "text": w, "cut": False, "phase": ""}
                    rows.append(cur)
        return {"rows": sorted(rows, key=lambda r: r["t"]), "transcript": booth.CAPTIONS.exists()}

    # ------------------------------------------------------------------ eval

    def scorecard(self) -> dict:
        """The booth against the human commentator of the same broadcast: how much, how often a name, the pauses, the make-up."""
        if self._score is None:
            names = booth.names_set(self.m)
            mine = booth.profile(booth.booth_words(self.lines), self.segs, names)
            ref = booth.profile(booth.human_words(self.m), self.segs, names)
            tb = booth.texture([l["text"] for l in self.lines if l["kind"] == "fill"], names)
            th = booth.human_texture(self.m, self.segs, names)
            self._score = {
                "phases": [{"phase": ph, "share": mine["phase"][ph]["share"], "wpm": [mine["phase"][ph]["wpm"], ref["phase"][ph]["wpm"]],
                            "npm": [mine["phase"][ph]["npm"], ref["phase"][ph]["npm"]]} for ph in booth.PHASES if ph in mine["phase"]],
                "whole": {"wpm": [mine["wpm"], ref["wpm"]], "npm": [mine["npm"], ref["npm"]]},
                "rhythm": [["Pauses over 3 s, per minute", mine["gaps_3"], ref["gaps_3"], ""], ["Pauses over 10 s, per minute", mine["gaps_10"], ref["gaps_10"], ""],
                           ["99th-percentile pause", mine["p99"], ref["p99"], " s"], ["Longest pause", mine["longest"], ref["longest"], " s"]],
                "texture": [["Lines containing a number", tb["number"], th["number"], "%"], ["Lines naming a player", tb["name"], th["name"], "%"],
                            ["Distinct two-word openings", tb["openings"], th["openings"], "%"], ["Words per line", tb["words"], th["words"], ""]],
                "voiced": self.voiced}
        return self._score

    def system(self) -> list[dict]:
        """One tile per part of the system. Each module reports its own state; a module that cannot report says why."""
        own = {}
        for key, mod in (("tuning", "tuning"), ("infra", "infra"), ("harness", "harness"), ("workflow", "gate")):
            try:
                own[key] = importlib.import_module(mod).status()
            except Exception as ex:  # a part that cannot report is shown as broken, not hidden
                own[key] = {"state": "not available", "summary": f"{type(ex).__name__}: {ex}"[:160], "facts": []}
        score, d = self.scorecard(), self.data()
        last = {j["name"]: j["last"] for j in self.jobs()["jobs"]}
        ran = [last[n] for n in SUITES if last.get(n)]
        on = sum(abs(p["wpm"][0] / p["wpm"][1] - 1) <= .1 for p in score["phases"])
        own["eval"] = {"state": "running", "summary": f"{on} of {len(score['phases'])} phases within 10% of the human commentator; "
                       f"{sum(r['passed'] for r in ran)} checks passed and {sum(r['failed'] for r in ran)} failed in the last run of each suite.",
                       "facts": [["Check suites", len(SUITES)],
                                 ["Words per minute, system / human", f"{score['whole']['wpm'][0]:.0f} / {score['whole']['wpm'][1]:.0f}"],
                                 ["Names per minute, system / human", f"{score['whole']['npm'][0]:.1f} / {score['whole']['npm'][1]:.1f}"],
                                 ["Runs on record", len(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else 0]]}
        own["data"] = {"state": "running", "summary": f"{len(d['sets'])} sources tracked with origin, freshness and whether each leaves this machine; "
                       f"{sum(x['exists'] for x in d['sets'])} present.", "facts": [[x["label"], f"{x['n']:,} {x['unit']}"] for x in d["sets"][:6]]}
        b, S = d["booth"], ROOT / "samples"
        own["live"] = {"state": "running", "summary": f"{b['lines']:,} lines over the real broadcast, in sync with the video: {b['play']} play-by-play, "
                       f"{b['colour']} colour, {b['model']} of those in the model's words.",
                       "facts": [["Overlay", "extension/overlay.js on the YouTube page"], ["Clock", "the video's own currentTime"],
                                 ["Demo clip", "demo/demo.mp4" if (ROOT / "demo" / "demo.mp4").exists() else "not recorded"]]}

        def text(name):
            return (S / name).read_text(encoding="utf-8") if (S / name).exists() else ""

        cues = text("commentary.vtt").count("-->")
        clips = json.loads(text("highlights.json") or "[]")
        rated = json.loads(text("ratings.json") or "[]")
        clips, rated = (clips if isinstance(clips, list) else clips.get("highlights", [])), (rated if isinstance(rated, list) else rated.get("players", []))
        own["media"] = {"state": "built" if text("report.md") else "not built", "open": ["report.md"],
                        "summary": "A match report and three social posts, every name and number taken from the event data.",
                        "facts": [["Output", "samples/report.md"], ["Words", len(text("report.md").split())]]}
        own["assess"] = {"state": "built" if rated else "not built", "open": ["ratings.md"],
                         "summary": f"A report card for {len(rated)} players from a stated formula: touches, passing, shots, chances created, ball wins.",
                         "facts": [["Output", "samples/ratings.md"], ["Players rated", len(rated)]]}
        own["assets"] = {"state": "running", "summary": "One search over everything the system has said, knows and has clipped; accent- and case-insensitive.",
                         "facts": [["Lines on air", f"{b['lines']:,}"], ["Preparation notes", len(self.notes())], ["Highlight moments", len(clips)]]}
        own["video"] = {"state": "built" if cues and clips else "not built", "open": ["highlights.md", "commentary.vtt"],
                        "summary": f"A subtitle track of {cues:,} cues on the video's clock and an edit list of {len(clips)} highlights with deep links; "
                        "no video is downloaded.", "facts": [["Subtitle cues", f"{cues:,}"], ["Highlights", len(clips)]]}
        return [{"key": k, "title": t, "zh": zh, "group": g, "jobs": [{"name": j, "label": JOBS[j][0]} for j in jb], **own.get(k, {})}
                for k, t, zh, g, jb in PARTS]

    def find(self, q: str) -> list[dict]:
        if not q.strip():
            return []
        found = importlib.import_module("products").search(q)  # moments and notes are few and lines are many: show the few first
        return sorted(found, key=lambda r: ("moment", "note", "line").index(r.get("kind", "line")) if r.get("kind") in ("moment", "note", "line") else 3)[:80]

    def start(self, name: str) -> str | None:
        """Start one job from the fixed list. Returns an error message, or None."""
        if name not in JOBS:
            return "unknown job"
        need = JOBS[name][3]
        if need == "transcript" and not booth.CAPTIONS.exists():
            return "needs the broadcast transcript, which is not redistributed with the repo"
        if need == "llm" and not engine.llm_config()[0]:
            return "needs the language model: run start_llm.cmd first"
        if need == "traces" and not list((ROOT / "runs").glob("vision-*.jsonl")):
            return "needs at least one vision run (start_vision.cmd)"
        with self.lock:
            if self.job and self.job["state"] == "running":
                return f"{JOBS[self.job['name']][0]} is still running"
            self.job = {"name": name, "state": "running", "started": time.time(), "out": [], "passed": 0, "failed": 0, "exit": None}
        threading.Thread(target=self._work, args=(self.job,), daemon=True).start()
        return None

    def _work(self, job: dict) -> None:
        proc = subprocess.Popen([sys.executable, "-u", *JOBS[job["name"]][2]], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        for line in proc.stdout:
            job["out"].append(line.rstrip("\n"))
            del job["out"][:-400]
            job["passed"] += line.startswith("PASS")
            job["failed"] += line.startswith("FAIL")
        job["exit"] = proc.wait()
        job["seconds"] = round(time.time() - job["started"], 1)
        if job["name"] in ("tune", "write") and job["exit"] == 0:
            self.rebuild(self.voiced)  # the knobs or the cache changed: put the new lines on air
        job["state"] = "passed" if job["exit"] == 0 else "failed"
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({k: job[k] for k in ("name", "state", "started", "seconds", "passed", "failed", "exit")}) + "\n")

    def jobs(self) -> dict:
        history = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines()[-200:]] if LOG.exists() else []
        last = {h["name"]: h for h in history}
        return {"current": self.job, "history": history[-25:][::-1],
                "jobs": [{"name": n, "label": j[0], "proves": j[1], "needs": j[3], "typical": j[4], "suite": n in SUITES, "last": last.get(n)} for n, j in JOBS.items()]}
