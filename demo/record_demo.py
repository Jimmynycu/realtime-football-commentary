"""How the demo video is made: the system's lines over the real broadcast, recorded inside the YouTube tab.

    python demo/record_demo.py lines 2058 2182      match seconds 34:18 to 36:22 -> demo/lines.json + demo/audio/*.wav
    python demo/record_demo.py receive              waits for the recording (keep it running during the next step)
    (in the YouTube tab: paste the three lines at the bottom of this docstring into the console and click the button it
     adds; a local tab hands over the recorder and the clip data. Run recordDemo(window.__cfg). When "Save recording"
     appears, click it: a local tab takes the recording and writes demo/raw.webm)
    python demo/record_demo.py mux demo/raw.webm "<window.__rec.marks as JSON>"    -> demo/demo.mp4

Why a window: a page on youtube.com may not send anything to this machine without a browser permission, and an automated
tab may not download. But it may open a local page and hand it the recording, and that page may save to its own server.

The clip is a side-by-side: the same picture and the same clock for the human commentator of the broadcast and for the
system. Two lanes scroll under the video, one block for every stretch of speech and a tag for every player named; a panel
counts words and names for both and times each one's call of a goal; the sound takes turns between the broadcast's own
(the commentator) and the system's voices.

A screen recorder would also capture the browser chrome, the account and the recommendations, and browser speech cannot
be recorded at all. So record.js draws the video frame and everything else onto a canvas and records that, with the
broadcast's sound; each line appears when the video's own clock reaches its time, exactly as in the live overlay. The voices are added here:
every line is rendered ahead of time with the Windows voices (David for play-by-play, Zira for colour; the live overlay
uses the browser's voices) and laid on the sound track at the same times, with the same rule for cutting in.

The broadcast itself is never downloaded; only the passage shown is recorded.

The three lines for the console (they add a "Load recorder" button; a click on it fetches record.js and lines.json through
a local tab, since the page itself may not read from localhost):

    b = Object.assign(document.createElement("button"), {textContent: "Load recorder", style: "position:fixed;z-index:99999;top:90px;left:40px;padding:18px"});
    b.onclick = () => { open("http://localhost:8799/send"); addEventListener("message", e => { if (e.origin === "http://localhost:8799" && e.data.code) { (0, eval)(trustedTypes.createPolicy("rec" + Date.now(), {createScript: s => s}).createScript(e.data.code)); window.__cfg = e.data.cfg; b.remove(); } }); };
    document.body.append(b);
"""
import hashlib
import json
import re
import subprocess
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import booth  # noqa: E402
import colour  # noqa: E402
import engine  # noqa: E402
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
    # the word a commentator says for a player -> how to show it ("maria" -> "Di María")
    shown = {engine.fold(n).split()[-1]: colour.short(n) for n in m.names.values()} | {"mcallister": "MacAllister", "mbape": "Mbappé", "embape": "Mbappé"}
    names = booth.names_set(m)
    for l in booth.run(m, frames, booth.load_params(), booth.Writer(m, live=False))[0]:
        if a <= l["t"] <= b:
            p = max(q for q in m.period_start if m.period_start[q] <= l["t"])
            name = hashlib.md5(l["text"].encode()).hexdigest()[:12] + ".wav"
            out.append({"t": round(kick[p] + l["t"] - m.period_start[p], 2), "text": l["text"], "voice": l["voice"], "sal": l["prio"],
                        "cuts": l["cuts"], "file": name, "dur": round(l["dur"], 2), "n": l["n"],
                        "names": [d for f, d in shown.items() if f in re.findall(r"[a-z]+", engine.fold(l["text"]))]})
            # ponytail: rate 4 keeps nearly every line inside booth.speak_time for these two voices; re-measure if the voices change
            jobs.append({"file": str(AUDIO / name), "text": l["text"].replace("—", ", "), "rate": 4,
                         "voice": "Microsoft Zira Desktop" if l["voice"] == "analyst" else "Microsoft David Desktop"})
    (AUDIO / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    (AUDIO / "speak.ps1").write_text(PS, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(AUDIO / "speak.ps1"), str(AUDIO / "jobs.json")], check=True)
    start = kick[1] + a - m.period_start[1] if a < m.period_start.get(2, 1e9) else out[0]["t"] - 2
    to_video = lambda t: round(kick[1] + t - m.period_start[1], 2)  # ponytail: first-half windows only; other periods need their own kick-off
    # The human side of the comparison: WHEN the commentator speaks and WHICH players he names, on the video's clock.
    # His words are not written out: the broadcast transcript is not ours to republish, and the comparison does not need it.
    human = [[to_video(t), int(w in names), shown.get(w) if w in names else None] for t, w in booth.human_words(m) if a <= t <= b]
    goals = []
    for e in m.events:
        if a <= e["t"] <= b and e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal":
            who, shot = colour.short(m.name(e["player"])), to_video(e["t"])
            goals.append({"shot": shot, "who": who, "ai": next((l["t"] for l in out if l["t"] >= shot - 1 and l["text"].startswith("GOAL")), None),
                          "human": next((t for t, flag, n in human if t >= shot - 1 and n == who), None)})
    # One pass, the half-minute around the goal, with the system's voice from the first seconds: the result is on screen at
    # once, and the human is there throughout as a lane. (Add a ("human") pass before it to replay the same seconds with
    # the broadcast's own sound first; that is a fairer test by ear and twice as long to watch.)
    passes = [[start + 50, start + 79, "ai"]]
    (HERE / "lines.json").write_text(json.dumps({"lines": out, "human": human, "goals": goals, "passes": passes}, ensure_ascii=False), encoding="utf-8")
    print(f"human: {len(human)} words, {sum(h[1] for h in human)} names; goals: {goals}")
    print(f"{len(out)} lines -> demo/lines.json, voiced into demo/audio")


def mux(raw: str, marks: list) -> None:
    """Lay each line's audio on the sound track where its caption appears. One microphone: a line that cuts in stops the
    one being said; otherwise at most one line waits its turn, and a third is dropped (the overlay's rules)."""
    cfg = json.loads((HERE / "lines.json").read_text(encoding="utf-8"))
    placed, pending = [], None  # [start, file, play_for]

    def length(f):
        with wave.open(str(AUDIO / f)) as w:
            return w.getnframes() / w.getframerate()

    def start(t, l):
        placed.append([t, l["file"], length(l["file"])])

    # marks: [second in the recording, video from, video to, who] for each take, as record.js measured them
    timed = [(clip + l["t"] - v0, l) for clip, v0, v1, who in marks if who == "ai" for l in cfg["lines"] if v0 <= l["t"] < v1 - 1]
    for t, l in timed:
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
    # The recording carries the broadcast's own sound. The two take turns as lines.json says: the commentator at full
    # level while the system is silent, then the system over the stadium at a tenth.
    human = "+".join(f"between(t,{clip - 2.2:.2f},{clip + v1 - v0:.2f})" for clip, v0, v1, who in marks if who == "human") or "0"
    graph.append("".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0,apad[ai]")
    graph.append(f"[0:a]volume='if({human},1,0.1)':eval=frame[bc]")
    graph.append("[bc][ai]amix=inputs=2:normalize=0:duration=first[a]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p",
            "-r", "30", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(HERE / "demo.mp4")]
    subprocess.run(cmd, check=True)
    print(f"{len(placed)} of {len(timed)} lines on the sound track -> demo/demo.mp4")


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


def sender() -> bytes:
    """A page that hands the recorder and the clip data to the YouTube tab that opened it (the reverse of /receive)."""
    cfg = json.loads((HERE / "lines.json").read_text(encoding="utf-8"))
    load = json.dumps({"code": (HERE / "record.js").read_text(encoding="utf-8"), "cfg": cfg}).replace("</", "<\\/")
    return ("<!doctype html><meta charset=utf-8><title>Recorder sender</title><body style='font:16px system-ui;padding:24px'>"
            "<p>Recorder sent. This tab can be closed.</p><script>opener && opener.postMessage(" + load + ", 'https://www.youtube.com');</script>").encode()


class Receiver(BaseHTTPRequestHandler):
    def do_GET(self):
        body = RECEIVER if self.path == "/receive" else sender() if self.path == "/send" else None
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body or b"")

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
    lines(float(sys.argv[2]), float(sys.argv[3])) if sys.argv[1] == "lines" else mux(sys.argv[2], json.loads(sys.argv[3]))
