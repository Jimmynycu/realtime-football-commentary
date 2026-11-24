"""Free local LLM behind an OpenAI-compatible API (POST /v1/chat/completions).

    start_llm.cmd          -> http://127.0.0.1:8081/v1   (server.py picks it up automatically)

Model: Qwen2.5-Instruct, 4-bit GGUF (Apache-2.0; 1.5B = 1.1 GB or 7B = 4.7 GB), run by llama.cpp on the GPU.
Only this file needs a non-stdlib package (llama-cpp-python); the commentary pipeline does not.
"""
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from llama_cpp import Llama

DIR = Path(__file__).parent / "models"
SMALL = "qwen2.5-1.5b-instruct-q4_k_m.gguf"  # 1.1 GB, ~0.2 s per line
# LLM_GGUF picks a file in models/; default is the largest one present (e.g. the 7B model), else download SMALL.
found = sorted(DIR.glob("*.gguf"), key=lambda f: f.stat().st_size)
NAME = os.environ.get("LLM_GGUF") or (found[-1].name if found else SMALL)
MODEL = DIR / NAME
PORT = int(os.environ.get("PORT", "8081"))

if not MODEL.exists():
    DIR.mkdir(exist_ok=True)
    print("downloading", SMALL, "(~1.1 GB, once)...")
    NAME, MODEL = SMALL, DIR / SMALL
    urllib.request.urlretrieve(f"https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/{SMALL}", MODEL)

llm = Llama(str(MODEL), n_gpu_layers=-1, n_ctx=2048, chat_format="chatml", verbose=False)
lock = threading.Lock()  # one generation at a time: the model is not re-entrant


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"object": "list", "data": [{"id": NAME, "object": "model"}]})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            with lock:
                out = llm.create_chat_completion(messages=req["messages"], temperature=float(req.get("temperature", 0.7)),
                                                 max_tokens=min(int(req.get("max_tokens", 80)), 200))
            self._json(200, out)
        except (KeyError, ValueError, TypeError) as ex:
            self._json(400, {"error": {"message": str(ex)}})

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = False  # fail instead of silently sharing the port on Windows
    print(f"local LLM ready: http://127.0.0.1:{PORT}/v1  ({NAME})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
