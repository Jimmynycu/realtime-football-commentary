"""Harness base checks against a scripted OpenAI-compatible mock (no real model needed):  python eval_harness.py"""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import booth
import colour
import game
import harness

replies = []  # popped one per request: text, "slow" (sleeps past the timeout) or "500"


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        reply = replies.pop(0)
        if reply == "slow":
            time.sleep(0.6)
        body = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        try:
            self.send_response(500 if reply == "500" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # client already timed out
            pass

    def log_message(self, *_):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{mock.server_port}/v1"
trace = Path(tempfile.mkdtemp()) / "t.jsonl"
harness.TRACE = trace
spec = harness.Spec("t", "sys", checks=[lambda out: "bad" if "BAD" in out else ""], fallback="DRAFT", timeout=0.3)
checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


def lines():
    return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []


def go(*rs):
    replies[:] = list(rs)
    before = len(lines())
    return harness.run(spec, "u", URL, "mock") + (len(lines()) - before,)


text, verdict, n = go("fine line")
check("faithful answer accepted on the first attempt", (text, verdict) == ("fine line", "ok") and len(replies) == 0, verdict)
check("accepted run leaves one trace line", n == 1)
text, verdict, n = go("BAD 1", "BAD 2", "BAD 3")
check("failing answer is retried then falls back", text == "DRAFT" and verdict == "fallback:bad" and not replies, verdict)
check("rejected run leaves one trace line", n == 1)
text, verdict, n = go("BAD 1", "good again")
check("retry can recover", (text, verdict) == ("good again", "ok"), verdict)
text, verdict, n = go("slow")
check("timeout falls back", text == "DRAFT" and verdict.startswith("error:"), verdict)
check("timeout run leaves one trace line", n == 1)
text, verdict, n = go("500")
check("HTTP 500 falls back", (text, verdict) == ("DRAFT", "error:HTTPError"), verdict)
check("error run leaves one trace line", n == 1)
rec = [json.loads(l) for l in lines()]
check("trace carries spec, model, attempts, source, total",
      all({"ts", "spec", "model", "attempts", "source", "total_ms"} <= r.keys() for r in rec)
      and [r["source"] for r in rec] == ["model", "fallback", "model", "fallback", "fallback"], [r["source"] for r in rec])
s = harness.summary(trace)["t"]
check("summary counts calls, first try, fallbacks", s["calls"] == 5 and s["first_try"] == 0.2 and s["fallback"] == 0.6
      and s["reasons"].get("bad") == 4, str(s))
st = harness.status()
check("status has the card shape", list(st) == ["key", "title", "state", "summary", "facts"] and len(st["facts"]) <= 8
      and json.dumps(st) and st["key"] == "harness")

harness.TRACE = trace.parent / "unwritable" / "x" / "\0bad"  # a trace failure must not break the call
replies[:] = ["ok text"]
text, verdict = harness.run(spec, "u", URL, "mock")
check("trace failure does not break a call", (text, verdict) == ("ok text", "ok"), verdict)
harness.TRACE = trace

m, frames = game.load()
n0 = len(lines())
out, _ = booth.run(m, frames, booth.load_params(), colour.Writer(m, live=False))
fill = sum(l["kind"] == "fill" for l in out)
check("regression: booth with draft-only Writer gives 1329 lines, 730 fill", (len(out), fill) == (1329, 730), f"{len(out)}/{fill}")
check("draft-only Writer writes no traces", len(lines()) == n0)
mock.shutdown()
sys.exit(0 if all(checks) else 1)
