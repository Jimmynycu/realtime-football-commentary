"""Live broadcast server: replays the match in real time and streams state + commentary over SSE.

    python server.py            then open http://localhost:8765

Stdlib only. One replay thread per viewer; commentary is generated off the replay thread so a slow
LLM never freezes the pitch.
"""
import bisect
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import control
import engine
import game

M = engine.Match()
PORT = int(os.environ.get("PORT", "8765"))
# The booth (ball-first rulebook + colour, tuned to the human commentator) writes the lines for regulation and extra time.
# It needs the player-position file; without it, and for the shootout, the older salience-based Director is used.
FRAMES = game.load()[1]
# Colour lines are voiced by the language model ahead of time (python booth.py --write) and read from its cache here,
# so a replay never waits on the model; a line the cache does not have goes out as its plain draft.
# The control room (control.py, /dashboard) owns those lines: it can rebuild them and score them against the human.
CTL = control.Control(M, FRAMES)
BOOTH_END = M.period_start.get(5, M.duration) if CTL.lines else -1.0


def video() -> dict | None:
    """Broadcast video config: YouTube id + the video second at which each period kicks off."""
    path = engine.ROOT / "video.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[\w-]{11}", cfg["video_id"]):
        raise ValueError("video.json: video_id must be an 11-character YouTube id")
    return {"id": cfg["video_id"], "kickoff": {int(p): float(v) for p, v in cfg["kickoff"].items()}}


VIDEO = video()
OVERLAY_ORIGIN = "https://www.youtube.com"  # the only other origin allowed to read this API (the overlay)


def moments() -> list[dict]:
    """Jump points for the demo: kick-off, 40 s before each goal, the shootout."""
    out, st = [{"label": "Kick-off", "t": 0}], engine.State(M)
    for e in M.events:
        st.apply(e)
        if e["period"] < 5 and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal":
            out.append({"label": f'{e["minute"] + 1}\' {M.name(e["player"])} ({st.score[0]}-{st.score[1]})',
                        "t": round(max(0, e["t"] - 40))})
        elif e["period"] == 5 and e["type"]["name"] == "Half Start" and out[-1]["label"] != "Penalty shootout":
            out.append({"label": "Penalty shootout", "t": int(e["t"])})
    return out


def run(speed: float, start: float, out: queue.Queue, stop: threading.Event) -> None:
    comm, pool = engine.Commentator(M), ThreadPoolExecutor(1)
    late_drops = 0

    def work(job, due):
        nonlocal late_drops
        text, source, ms = comm.line(job)
        lag = time.monotonic() - t0 - due
        if job["sal"] < engine.Director.PRIORITY and lag > engine.Director.STALE:
            late_drops += 1  # the LLM answered too late for an ordinary line: say nothing
            return
        out.put({"type": "line", "voice": job["voice"], "kind": job["kind"], "sal": job["sal"], "text": text,
                 "minute": job["facts"]["minute"], "source": source, "ms": ms})

    def sleep_until(due: float) -> bool:
        while (wait := t0 + due - time.monotonic()) > 0:
            if stop.wait(min(wait, 0.25)):
                return False
        return not stop.is_set()

    t0 = time.monotonic()
    BOOTH = CTL.lines  # this viewer keeps the lines that were on air when the stream started
    i, sent_init = bisect.bisect_left([l["t"] for l in BOOTH], start), False
    for e, st, now, jobs, d in engine.play(M, speed, start):
        if not sent_init:
            out.put({"type": "init", "mom": st.mom, "t": e["t"]})
            sent_init = True
        while i < len(BOOTH) and BOOTH[i]["t"] <= e["t"]:  # booth lines that fall before this event (colour during dead ball)
            l = BOOTH[i]
            i += 1
            if not sleep_until((l["t"] - start) / speed):
                break
            out.put({"type": "line", "voice": l["voice"], "kind": l["kind"], "sal": l["prio"], "cuts": l["cuts"], "text": l["text"],
                     "minute": l["minute"], "source": "rulebook", "ms": 0})
        if not sleep_until(now):
            pool.shutdown(wait=False, cancel_futures=True)
            return
        if e.get("location") or jobs:
            b = int(e["t"] // 60)
            out.put({"type": "state", "t": round(e["t"], 2), "clock": st.clock, "period": st.period, "score": st.score,
                     "shootout": st.shootout if st.period == 5 else None, "ball": st.ball, "side": st.side_on_ball,
                     "label": " · ".join(filter(None, [e["type"]["name"].rstrip("*"), M.name(e.get("player"))])),
                     "stats": st.snapshot(), "mom": [b, *st.mom[b]], "dropped": d.dropped + late_drops})
        if e["t"] >= BOOTH_END:
            for j in jobs:
                pool.submit(work, j, now)
    pool.shutdown(wait=True)
    out.put({"type": "end"})
    out.put(None)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy bind the same port silently


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", OVERLAY_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        """Control-room replies carry no CORS header: no other origin, the overlay's included, may read or drive them."""
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        """The control room answers this machine's own pages only (also shuts out DNS rebinding and cross-site posts)."""
        hosts = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or origin in {f"http://{h}" for h in hosts})

    def api(self, path: str, q: dict, body: dict | None):
        if not self._local():
            return self._json({"error": "the control room is local only"}, 403)
        try:
            if body is None:
                if path == "/api/data":
                    return self._json(CTL.data())
                if path == "/api/scorecard":
                    return self._json(CTL.scorecard())
                if path == "/api/jobs":
                    return self._json(CTL.jobs())
                if path == "/api/notes":
                    return self._json(CTL.notes())
                if path == "/api/lines":
                    a = min(M.duration, max(0.0, float(q.get("from", ["0"])[0])))
                    return self._json(CTL.window(a, min(a + 900, max(a, float(q.get("to", [str(a + 180)])[0])))))
            else:
                if path == "/api/run":
                    err = CTL.start(str(body.get("job")))
                elif path == "/api/rebuild":
                    return self._json(CTL.rebuild(bool(body.get("voiced", True))))
                elif path == "/api/notes":
                    err = CTL.edit_notes(add=body.get("add"), remove=body.get("remove"))
                else:
                    return self._json({"error": "not found"}, 404)
                return self._json({"error": err} if err else {"ok": True}, 409 if err else 200)
        except ValueError:
            return self._json({"error": "bad number"}, 400)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:  # validate at the trust boundary: JSON only (a cross-site form cannot send it), small, an object
            n = int(self.headers.get("Content-Length", "0"))
            if not self.headers.get("Content-Type", "").startswith("application/json") or n > 4096:
                raise ValueError
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "send a small JSON object"}, 400)
        self.api(urlparse(self.path).path, {}, body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path.startswith("/api/"):
            self.api(url.path, q, None)
        elif url.path == "/dashboard":
            self._send(200, "text/html; charset=utf-8", (engine.ROOT / "dashboard.html").read_bytes())
        elif url.path == "/":
            self._send(200, "text/html; charset=utf-8", (engine.ROOT / "index.html").read_bytes())
        elif url.path == "/overlay.js":  # lets the YouTube page load the overlay without installing the extension
            self._send(200, "text/javascript; charset=utf-8", (engine.ROOT / "extension" / "overlay.js").read_bytes())
        elif url.path == "/meta":
            meta = {"title": M.title, "home": M.home, "away": M.away, "moments": moments(), "video": VIDEO,
                    "periods": [{"p": p, "start": M.period_start[p], "len": M.period_len[p]} for p in sorted(M.period_start)],
                    "llm": engine.llm_config()[1] or None}
            self._send(200, "application/json", json.dumps(meta).encode())
        elif url.path == "/stream":
            try:  # validate at the trust boundary
                speed = min(60.0, max(0.5, float(q.get("speed", ["1"])[0])))
                start = min(M.duration, max(0.0, float(q.get("t", ["0"])[0])))
            except ValueError:
                return self._send(400, "text/plain", b"speed and t must be numbers")
            self.stream(speed, start)
        else:
            self._send(404, "text/plain", b"not found")

    def stream(self, speed: float, start: float):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", OVERLAY_ORIGIN)
        self.end_headers()
        out, stop = queue.Queue(), threading.Event()
        threading.Thread(target=run, args=(speed, start, out, stop), daemon=True).start()
        try:
            while (msg := out.get()) is not None:
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
        except OSError:  # viewer went away
            pass
        finally:
            stop.set()

    def log_message(self, *_):  # keep the console quiet during the demo
        pass


if __name__ == "__main__":
    try:
        httpd = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit(f"port {PORT} is already in use: the server is probably running already")
    print(f"{M.title}: {M.home} v {M.away}  ->  http://localhost:{PORT}  (LLM: {engine.llm_config()[1] or 'none, template commentator'})")
    httpd.serve_forever()
