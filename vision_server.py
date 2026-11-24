"""Vision mode: play-by-play from the picture alone. No event feed, no audio.

    start_vision.cmd                      -> http://127.0.0.1:8770
    python vision_server.py --frames DIR  -> offline: print what the model reads from saved frames

Input   JPEG frames of any football broadcast (the overlay posts ~2 per second) + vision_match.json
        (team names, kit colours, shirt number -> player name).
Output  SSE: observations (clock, score, view, zone) and commentary lines.

Pipeline  frame -> three 448x448 tiles -> local VLM (MiniCPM-V 2.6) -> Observer (debounce, sanity checks)
          -> engine.Director (airtime budget) -> template line.
The VLM is a sensor, not a narrator: it fills a fixed one-line form per tile, and a claim only becomes a line
after the Observer has seen it twice or the scoreboard confirms it.
"""
from __future__ import annotations

import base64
import io
import json
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from llama_cpp import Llama
from llama_cpp.llama_chat_format import MiniCPMv26ChatHandler
from PIL import Image

import engine

ROOT = Path(__file__).parent
MODELS = ROOT / "models" / "vision"
PORT = 8770
ORIGIN = "https://www.youtube.com"
CFG = json.loads((ROOT / "vision_match.json").read_text(encoding="utf-8"))
HOME, AWAY = CFG["home"], CFG["away"]

# ---------------------------------------------------------------- sensor: VLM on fixed tiles

# Small VLMs answer a plain question far better than they fill a form (a form made MiniCPM repeat one answer for
# every frame), so each tile gets one short natural question and the reply is parsed by keyword.
# Naming the teams in this question made the model answer "Argentina vs France" and stop describing the action.
SCENE_Q = ("Football TV broadcast frame. Answer in one short line: is this a WIDE shot of the pitch or a CLOSE-UP of players? "
           "Is a goal (net) visible? What is happening?")
BOARD_Q = (
    "Top-left corner of a football TV broadcast. If a scoreboard graphic with a running match clock is visible, answer exactly: "
    "CLOCK=<minutes:seconds>; HOME=<team code>; SCORE=<home goals>-<away goals>; AWAY=<team code>. "
    "If there is no scoreboard graphic in this picture, answer exactly: NONE")
CLOSE_Q = (
    f"Close-up from a football broadcast. HOME team wears {HOME['kit']}. AWAY team wears {AWAY['kit']}. "
    "Look at the most prominent player. Answer exactly: KIT=<home|away|referee|other>; NUMBER=<shirt number or none>. "
    "Only give a number you can clearly read on the shirt or shorts.")


class Sensor:
    def __init__(self):
        handler = MiniCPMv26ChatHandler(clip_model_path=str(MODELS / "mmproj-model-f16.gguf"), verbose=False)
        self.llm = Llama(str(MODELS / "MiniCPM-V-2_6-Q4_K_M.gguf"), chat_handler=handler, n_ctx=2048, n_gpu_layers=-1, verbose=False)

    def ask(self, tile: Image.Image, prompt: str, max_tokens: int = 36) -> str:
        buf = io.BytesIO()
        tile.save(buf, "JPEG", quality=88)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        out = self.llm.create_chat_completion(temperature=0.0, max_tokens=max_tokens, messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": uri}}, {"type": "text", "text": prompt}]}])
        return out["choices"][0]["message"]["content"].strip()


def tile_whole(im: Image.Image) -> Image.Image:
    """Whole frame letterboxed to one 448x448 tile (one tile = 64 image tokens = fast; other shapes are 10x slower)."""
    t = im.copy()
    t.thumbnail((448, 448), Image.LANCZOS)
    out = Image.new("RGB", (448, 448))
    out.paste(t, ((448 - t.width) // 2, (448 - t.height) // 2))
    return out


def tile_board(im: Image.Image) -> Image.Image:
    x0, y0, x1, y1 = CFG.get("scoreboard_box", [0, 0, 0.35, 0.62])  # fractions of the frame; top-left by default
    return im.crop((int(x0 * im.width), int(y0 * im.height), int(x1 * im.width), int(y1 * im.height))).resize((448, 448), Image.LANCZOS)


def tile_close(im: Image.Image) -> Image.Image:
    s = im.height
    return im.crop(((im.width - s) // 2, 0, (im.width + s) // 2, s)).resize((448, 448), Image.LANCZOS)


EVENT_WORDS = [("corner", r"corner"), ("penalty", r"penalt"), ("free_kick", r"free[ -]?kick"), ("throw_in", r"throw"),
               ("goal_kick", r"goal[ -]kick"), ("celebration", r"celebrat"), ("injury", r"injur|on the ground|lying|treatment"),
               ("card", r"\bcard\b"), ("substitution", r"substitut")]


def parse_scene(text: str) -> dict:
    """Keyword parse of the one-line answer. Anything not clearly stated stays None and is ignored downstream."""
    t = text.lower()
    view = "close" if re.search(r"close[ -]?up", t) else "wide" if "wide" in t else "crowd" if re.search(r"crowd|fans|spectator|bench", t) else None
    if re.search(r"no goal[^,.;]*visible|goal[^,.;]*not visible|no net", t):
        goal = "none"
    else:
        m = re.search(r"goal[^.;]*?\b(left|right)\b|\b(left|right)\b[^.;]*?goal", t)
        goal = (m.group(1) or m.group(2)) if m else ("yes" if re.search(r"goal[^.;]*visible|visible[^.;]*goal", t) else None)
    event = next((k for k, rx in EVENT_WORDS if re.search(rx, t)), None)
    return {"view": view, "goal": goal, "event": event}


def parse_board(text: str) -> dict | None:
    c = re.search(r"(\d{1,3}):(\d{2})", text)
    s = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", text[c.end():]) if c else None
    if not (c and s) or int(c.group(2)) > 59:
        return None
    h, a = re.search(r"HOME\s*=\s*<?([A-Za-z]{2,14})", text), re.search(r"AWAY\s*=\s*<?([A-Za-z]{2,14})", text)
    return {"clock": int(c.group(1)) * 60 + int(c.group(2)), "score": (int(s.group(1)), int(s.group(2))),
            "codes": [h.group(1).upper(), a.group(1).upper()] if h and a else None}


def parse_close(text: str) -> dict:
    t = text.lower()
    n = re.search(r"number\s*(?:=|is|:)?\s*<?(\d{1,2})\b", t)
    kit = re.search(r"kit\s*=\s*<?(home|away|referee|other)", t)
    return {"kit": kit.group(1) if kit else None, "number": n.group(1) if n else None}


# ---------------------------------------------------------------- observer: observations -> verified events

# Three registers, set by measured reliability on this broadcast (see eval_vision.py):
#   fact        scoreboard readings (clock, score): ~100% correct -> stated plainly
#   impression  what the model sees on the pitch (near the goal, celebration, corner, penalty) -> "looks like", seen twice
#   silence     free kicks, throw-ins, goal kicks: the model claims them constantly in open play -> never announced
engine.T.update({
    "v_goal": ["GOAL! {team} score! It's {score}."],
    "v_scorer": ["The cameras go straight to {player}, number {number}. That looks like the scorer."],
    "v_celebration": ["Players are celebrating! Waiting for the scoreboard."],
    "v_corner": ["Looks like a corner kick."],
    "v_penalty": ["That looks like a penalty kick!"],
    "v_attack": ["Play is close to the goal now."],
    "v_closeup": ["The camera picks out {player}."],
    "v_card": ["It looks like the referee has a card out."],
    "v_substitution": ["It looks like a substitution."],
    "v_injury": ["A player seems to be down."],
    "v_clock": ["{minute} minutes played. {score}."],
})
SAL = {"v_goal": 10, "v_scorer": 8, "v_penalty": 8, "v_celebration": 7, "v_card": 6, "v_substitution": 5, "v_corner": 4,
       "v_injury": 4, "v_attack": 3, "v_closeup": 2, "v_clock": 1}
NEEDS_GOAL_IN_VIEW = ("corner", "penalty")
OTHER_EVENTS = ("card", "substitution", "injury")


class Observer:
    """Turns noisy per-frame readings into events. Rules: say it only when seen twice, or when the scoreboard says so."""
    REPEAT = 12.0  # wall seconds before the same kind of event may be announced again

    def __init__(self, known: bool):
        self.known = known  # True when vision_match.json describes this video: names and shirt numbers are available
        self.names = [HOME["name"], AWAY["name"]] if known else ["Home", "Away"]
        self.clock, self.score, self.pending_score = None, None, None
        self.view, self.goal, self.event = None, None, None
        self.streak = {"event": 0, "goal": 0}
        self.said: dict[str, float] = {}
        self.last_minute, self.goal_at, self.goal_team, self.scorer_named = -1, -1e9, None, True
        self.last_close = None  # (player, number, kit, time) of the last identified close-up
        self.close_read, self.clock_jump = None, None

    def scoreline(self) -> str:
        return f"{self.names[0]} {self.score[0]}-{self.score[1]} {self.names[1]}" if self.score else ""

    def _cand(self, kind: str, now: float, key: str = "", **facts) -> list[dict]:
        if now - self.said.get(kind + key, -1e9) < (40.0 if key else self.REPEAT) and SAL[kind] < 8:
            return []
        self.said[kind + key] = now
        return [{"kind": kind, "sal": SAL[kind], "voice": "pbp", "facts": {"event": kind, **{k: v for k, v in facts.items() if v}}}]

    def scene(self, s: dict, now: float) -> list[dict]:
        out = []
        for k in ("event", "goal"):
            self.streak[k] = self.streak[k] + 1 if s[k] and s[k] == getattr(self, k) else 1
            setattr(self, k, s[k])
        self.view = s["view"]
        in_view = self.goal in ("left", "right", "yes")
        if self.streak["event"] == 2 and (self.event in OTHER_EVENTS or (self.event in NEEDS_GOAL_IN_VIEW and in_view)):
            out += self._cand("v_" + self.event, now)
        if self.event == "celebration" and self.streak["event"] == 2 and now - self.goal_at > 60:
            out += self._cand("v_celebration", now)
        if self.view == "wide" and in_view and self.streak["goal"] == 2:
            out += self._cand("v_attack", now)
        return out

    def board(self, b: dict | None, now: float) -> list[dict]:
        if not b:
            return []
        out = []
        if not self.known and b["codes"]:
            self.names = b["codes"]
        if self.clock is None or -3 <= b["clock"] - self.clock <= 600:  # clocks only move forward, and not by 10 minutes
            self.clock, self.clock_jump = b["clock"], None
        elif self.clock_jump is not None and 0 <= b["clock"] - self.clock_jump <= 15:  # the same jump read twice: the viewer seeked
            self.clock, self.clock_jump = b["clock"], None
        else:
            self.clock_jump = b["clock"]
        if self.score is None:
            self.score = b["score"]
        elif b["score"] != self.score:
            dh, da = b["score"][0] - self.score[0], b["score"][1] - self.score[1]
            if (dh, da) in ((1, 0), (0, 1)) and b["score"] == self.pending_score:  # exactly one goal, read twice
                self.score, self.pending_score, self.goal_at = b["score"], None, now
                self.goal_team, self.scorer_named = "home" if dh else "away", False
                out += self._cand("v_goal", now, team=self.names[0 if dh else 1], score=self.scoreline())
                lc = self.last_close  # directors cut to the scorer first: use the close-up seen just before confirmation
                if lc and lc[2] == self.goal_team and now - lc[3] < 12:
                    self.scorer_named = True
                    out += self._cand("v_scorer", now, player=lc[0], number=lc[1])
            elif b["score"] == self.pending_score:  # any other change read twice: the viewer seeked, resync without a line
                self.score, self.pending_score = b["score"], None
            else:
                self.pending_score = b["score"]
        minute = self.clock // 60
        if minute % 5 == 0 and minute != self.last_minute and minute > 0:
            self.last_minute = minute
            out += self._cand("v_clock", now, minute=minute, score=self.scoreline())
        return out

    def on_pitch(self, side: dict) -> set[str]:
        """Shirt numbers that can be on the pitch at the current scoreboard time: starting XI adjusted by substitutions."""
        minute, on = (self.clock or 0) / 60, set(side.get("starting") or side["players"])
        for sub in side.get("substitutions", []):
            if sub["minute"] <= minute:
                on.discard(sub["off"])
                on.add(sub["on"])
        return on

    def close(self, c: dict, now: float) -> list[dict]:
        side = {"home": HOME, "away": AWAY}.get(c["kit"])
        prev, self.close_read = self.close_read, (c["kit"], c["number"])
        # a name needs: a known kit, a number read the same way in two consecutive close-ups, and a player who can be on the pitch
        if not side or not c["number"] or prev != self.close_read or c["number"] not in self.on_pitch(side):
            return []
        player = side["players"].get(c["number"])
        if not player:
            return []
        self.last_close = (player, c["number"], c["kit"], now)
        if now - self.goal_at < 20 and c["kit"] == self.goal_team and not self.scorer_named:
            self.scorer_named = True
            return self._cand("v_scorer", now, player=player, number=c["number"])
        if now - self.goal_at < 45:  # celebration shots show everybody: do not list them
            return []
        return self._cand("v_closeup", now, key=player, player=player)

    def state(self) -> dict:
        return {"clock": f"{self.clock // 60:02d}:{self.clock % 60:02d}" if self.clock is not None else None, "score": self.scoreline() or None,
                "view": self.view, "goal": self.goal, "event": self.event}


def analyse(sensor: Sensor, obs: Observer, im: Image.Image, i: int, now: float) -> tuple[list[dict], dict]:
    """One cycle: scene every frame, scoreboard every 3rd frame (and while a score change is pending), close-up when it is one."""
    t = time.perf_counter()
    raw = {"scene": sensor.ask(tile_whole(im), SCENE_Q)}
    cands = obs.scene(parse_scene(raw["scene"]), now)
    if i % 3 == 0 or obs.pending_score or obs.clock is None or obs.event == "celebration":
        raw["board"] = sensor.ask(tile_board(im), BOARD_Q)
        cands += obs.board(parse_board(raw["board"]), now)
    if obs.known and obs.view == "close":
        raw["close"] = sensor.ask(tile_close(im), CLOSE_Q, 20)
        cands += obs.close(parse_close(raw["close"]), now)
    return cands, {"raw": raw, "ms": round((time.perf_counter() - t) * 1000)}


# ---------------------------------------------------------------- live server

latest: dict | None = None            # newest unprocessed frame; older ones are overwritten (latest wins)
arrived = threading.Event()
subscribers: list[queue.Queue] = []


def publish(msg: dict) -> None:
    for q in list(subscribers):
        q.put(msg)


def worker(sensor: Sensor) -> None:
    global latest
    obs, d = Observer(False), engine.Director(None)
    d.next_analysis = float("inf")  # no data-driven analyst in vision mode
    st, t0, i, src = SimpleNamespace(period=1), time.monotonic(), 0, None
    (ROOT / "runs").mkdir(exist_ok=True)
    log = open(ROOT / "runs" / f"vision-{int(time.time())}.jsonl", "a", encoding="utf-8")
    while True:
        arrived.wait()
        arrived.clear()
        frame, latest = latest, None
        if not frame:
            continue
        if frame["src"] != src:  # a different video: forget everything
            obs, src = Observer(frame["src"] == CFG.get("video_id")), frame["src"]
        now = time.monotonic() - t0
        cands, info = analyse(sensor, obs, Image.open(io.BytesIO(frame["jpeg"])).convert("RGB"), i, now)
        i += 1
        out = [{"type": "line", "text": engine.render(job), "kind": job["kind"], "sal": job["sal"], "voice": "pbp",
                "frame_t": frame["t"], "ms": info["ms"]} for cand in cands or [None] for job in d.offer(cand, now, st, {"t": now})]
        out.append({"type": "obs", **obs.state(), "frame_t": frame["t"], "ms": info["ms"], "raw": info["raw"], "superseded": d.dropped})
        for msg in out:
            publish(msg)
            log.write(json.dumps({**msg, "src": src, "wall": round(now + info["ms"] / 1000, 2)}, ensure_ascii=False) + "\n")
        log.flush()


class Handler(BaseHTTPRequestHandler):
    def _head(self, code: int, ctype: str = "text/plain", length: int | None = 0):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_OPTIONS(self):
        self._head(204)

    def do_POST(self):
        global latest
        q = parse_qs(urlparse(self.path).query)
        n = int(self.headers.get("Content-Length", "0"))
        if urlparse(self.path).path != "/frame" or not 0 < n < 2_000_000:  # trust boundary: size and route
            return self._head(400)
        try:
            t = float(q.get("t", ["0"])[0])
        except ValueError:
            return self._head(400)
        latest = {"jpeg": self.rfile.read(n), "t": t, "src": q.get("src", [""])[0][:32]}
        arrived.set()
        self._head(200)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/live.js":
            body = (ROOT / "extension" / "live.js").read_bytes()
            self._head(200, "text/javascript; charset=utf-8", len(body))
            self.wfile.write(body)
        elif path == "/meta":
            body = json.dumps({"home": HOME["name"], "away": AWAY["name"], "model": "MiniCPM-V 2.6 (local)"}).encode()
            self._head(200, "application/json", len(body))
            self.wfile.write(body)
        elif path == "/stream":
            self._head(200, "text/event-stream", None)
            q: queue.Queue = queue.Queue()
            subscribers.append(q)
            try:
                while True:
                    self.wfile.write(f"data: {json.dumps(q.get())}\n\n".encode())
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                subscribers.remove(q)
        else:
            self._head(404)

    def log_message(self, *_):
        pass


def offline(sensor: Sensor, folder: str) -> None:
    """Print what the sensor reads from saved frames (no Observer state carried between unrelated frames)."""
    for p in sorted(Path(folder).glob("*.jpg")):
        im = Image.open(p).convert("RGB")
        t = time.perf_counter()
        raw = sensor.ask(tile_whole(im), SCENE_Q)
        s, b = parse_scene(raw), parse_board(sensor.ask(tile_board(im), BOARD_Q))
        c = parse_close(sensor.ask(tile_close(im), CLOSE_Q, 20)) if s["view"] == "close" else {}
        print(json.dumps({"frame": p.stem, "ms": round((time.perf_counter() - t) * 1000), "raw": raw, **s,
                          "clock": b and f'{b["clock"] // 60:02d}:{b["clock"] % 60:02d}', "score": b and list(b["score"]), **c}), flush=True)


if __name__ == "__main__":
    sensor = Sensor()
    if len(sys.argv) > 2 and sys.argv[1] == "--frames":
        offline(sensor, sys.argv[2])
    else:
        threading.Thread(target=worker, args=(sensor,), daemon=True).start()
        ThreadingHTTPServer.allow_reuse_address = False
        print(f"vision mode ready: http://127.0.0.1:{PORT}  ({HOME['name']} v {AWAY['name']})", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
