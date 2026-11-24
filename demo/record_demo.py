"""How the demo video is made: the system's lines over the real broadcast, recorded inside the YouTube tab.

    python demo/record_demo.py lines 2058 2182      match seconds 34:18 to 36:22 -> demo/lines.json + demo/audio/*.wav
    python demo/record_demo.py receive              waits for the recording (keep it running during the next step)
    (in the YouTube tab: paste demo/record.js into the console, run recordDemo(<contents of lines.json>), and when the
     "Save recording" button appears, click it: a small local window opens, takes the recording and writes demo/raw.webm)
    python demo/record_demo.py mux demo/raw.webm    -> demo/demo.mp4

Why a window: a page on youtube.com may not send anything to this machine without a browser permission, and an automated
tab may not download. But it may open a local page and hand it the recording, and that page may save to its own server.

A screen recorder would also capture the browser chrome, the account and the recommendations, and browser speech cannot
be recorded at all. So record.js draws only the video frame and the caption onto a canvas and records that; each caption
appears when the video's own clock reaches the line's time, exactly as in the live overlay. The voices are added here:
every line is rendered ahead of time with the Windows voices (David for play-by-play, Zira for colour; the live overlay
uses the browser's voices) and laid on the sound track at the same times, with the same rule for cutting in.

The broadcast itself is never downloaded; only the two minutes shown are recorded, with its own sound muted.
"""
import hashlib
import json
import subprocess
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import booth  # noqa: E402
import game  # noqa: E402

AUDIO = HERE / "audio"
PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($l in (Get-Content -Raw -Encoding UTF8 $args[0] | ConvertFrom-Json)) {
  $s.SelectVoice($l.voice); $s.Rate = $l.rate; $s.SetOutputToWaveFile($l.file); $s.Speak($l.text)
}
$s.SetOutputToNull()
"""


def lines(a: float, b: float) -> None:
    m, frames = game.load()
    video = json.loads((HERE.parent / "video.json").read_text(encoding="utf-8"))
    kick = {int(p): v for p, v in video["kickoff"].items()}
    AUDIO.mkdir(exist_ok=True)
    out, jobs = [], []
    for l in booth.run(m, frames, booth.load_params(), booth.Writer(m, live=False))[0]:
        if a <= l["t"] <= b:
            p = max(q for q in m.period_start if m.period_start[q] <= l["t"])
            name = hashlib.md5(l["text"].encode()).hexdigest()[:12] + ".wav"
            out.append({"t": round(kick[p] + l["t"] - m.period_start[p], 2), "text": l["text"], "voice": l["voice"], "sal": l["prio"],
                        "cuts": l["cuts"], "file": name})
            # ponytail: rate 4 keeps nearly every line inside booth.speak_time for these two voices; re-measure if the voices change
            jobs.append({"file": str(AUDIO / name), "text": l["text"].replace("—", ", "), "rate": 4,
                         "voice": "Microsoft Zira Desktop" if l["voice"] == "analyst" else "Microsoft David Desktop"})
    (AUDIO / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    (AUDIO / "speak.ps1").write_text(PS, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(AUDIO / "speak.ps1"), str(AUDIO / "jobs.json")], check=True)
    start = kick[1] + a - m.period_start[1] if a < m.period_start.get(2, 1e9) else out[0]["t"] - 2
    (HERE / "lines.json").write_text(json.dumps({"videoStart": round(start, 2), "seconds": round(b - a + 2), "lines": out}, ensure_ascii=False), encoding="utf-8")
    print(f"{len(out)} lines -> demo/lines.json, voiced into demo/audio")


def mux(raw: str) -> None:
    """Lay each line's audio on the sound track where its caption appears. One microphone: a line that cuts in stops the
    one being said; otherwise at most one line waits its turn, and a third is dropped (the overlay's rules)."""
    cfg = json.loads((HERE / "lines.json").read_text(encoding="utf-8"))
    placed, pending = [], None  # [start, file, play_for]

    def length(f):
        with wave.open(str(AUDIO / f)) as w:
            return w.getnframes() / w.getframerate()

    def start(t, l):
        placed.append([t, l["file"], length(l["file"])])

    for l in cfg["lines"]:
        t = l["t"] - cfg["videoStart"]
        if pending and placed[-1][0] + placed[-1][2] <= t:  # the waiting line got its turn before this one arrived
            start(placed[-1][0] + placed[-1][2], pending)
            pending = None
        busy = placed and placed[-1][0] + placed[-1][2] > t
        if l["cuts"] or l["sal"] >= 7:
            pending = None
            if busy:
                placed[-1][2] = t - placed[-1][0]
            start(t, l)
        elif busy:
            pending = pending or l
        else:
            start(t, l)
    if pending:
        start(placed[-1][0] + placed[-1][2], pending)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", raw]
    graph = []
    for i, (t, f, d) in enumerate(placed):
        cmd += ["-i", str(AUDIO / f)]
        graph.append(f"[{i + 1}:a]atrim=0:{d:.2f},afade=t=out:st={max(0, d - .05):.2f}:d=0.05,adelay={int(t * 1000)}:all=1[a{i}]")
    graph.append("".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0,apad[a]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(HERE / "demo.mp4")]
    subprocess.run(cmd, check=True)
    print(f"{len(placed)} of {len(cfg['lines'])} lines on the sound track -> demo/demo.mp4")


PORT = 8799
RECEIVER = b"""<!doctype html><meta charset=utf-8><title>Recording receiver</title><body style="font:16px system-ui;padding:24px">
<p id=s>Waiting for the recording...</p><script>
addEventListener("message", async e => {
  if (e.origin !== "https://www.youtube.com" || !(e.data instanceof Blob)) return;
  s.textContent = "Saving " + (e.data.size / 1e6).toFixed(1) + " MB...";
  const r = await fetch("/save", { method: "POST", body: e.data });
  s.textContent = r.ok ? "Saved to demo/raw.webm. This window can be closed." : "Could not save: " + r.status;
});
opener && opener.postMessage("receiver-ready", "https://www.youtube.com");
</script>"""


class Receiver(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/receive" else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(RECEIVER if self.path == "/receive" else b"")

    def do_POST(self):  # only this server's own page may save, and only one bounded file
        n = int(self.headers.get("Content-Length", "0"))
        ok = self.path == "/save" and self.headers.get("Origin") in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}") and 0 < n < 400_000_000
        if ok:
            (HERE / "raw.webm").write_bytes(self.rfile.read(n))
            print(f"saved demo/raw.webm ({n / 1e6:.1f} MB)", flush=True)
        self.send_response(200 if ok else 403)
        self.end_headers()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    if sys.argv[1] == "receive":
        print(f"receiver on http://localhost:{PORT}/receive", flush=True)
        HTTPServer(("127.0.0.1", PORT), Receiver).serve_forever()
    lines(float(sys.argv[2]), float(sys.argv[3])) if sys.argv[1] == "lines" else mux(sys.argv[2])
