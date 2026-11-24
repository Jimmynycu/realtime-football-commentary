// Records the demo inside the YouTube tab: the video frame and the caption on a canvas, into MediaRecorder, saved as a
// file. Nothing but the video picture is captured. See record_demo.py. Usage:
//   recordDemo(<contents of demo/lines.json>)   then click "Save recording"
window.recordDemo = async ({ lines, videoStart, seconds, receiver = "http://localhost:8799" }) => {
  const state = window.__rec = { phase: "seeking", shown: 0, frames: 0 };
  const v = document.querySelector("video"), W = 1280, H = 720;
  const c = Object.assign(document.createElement("canvas"), { width: W, height: H }), g = c.getContext("2d");
  const wrap = (text, max) => { const out = [""]; for (const w of text.split(" ")) { if (g.measureText(out.at(-1) + " " + w).width > max && out.at(-1)) out.push(w); else out[out.length - 1] = (out.at(-1) + " " + w).trim(); } return out; };
  let next = 0, line = null;
  const draw = () => {
    const t = v.currentTime;  // the video is the clock: a caption appears when the picture reaches its time
    while (next < lines.length && lines[next].t <= t) { line = lines[next++]; state.shown++; }
    g.drawImage(v, 0, 0, W, H); state.frames++;
    g.font = "600 17px system-ui, sans-serif"; g.textBaseline = "middle";
    const tag = "AI commentary · broadcast audio muted", tw = g.measureText(tag).width;
    g.fillStyle = "rgba(8,12,18,.72)"; g.fillRect(W - tw - 44, 24, tw + 24, 34); g.fillStyle = "#e8eef5"; g.fillText(tag, W - tw - 32, 41);
    if (line && t - line.t < (line.voice === "analyst" ? 9 : 5)) {
      g.font = "600 30px system-ui, sans-serif"; const rows = wrap(line.text, W - 260), w = Math.max(...rows.map(r => g.measureText(r).width)) + 48, h = rows.length * 40 + 22, x = (W - w) / 2, y = H - 56 - h;
      g.fillStyle = "rgba(8,12,18,.86)"; g.fillRect(x, y, w, h);
      g.fillStyle = line.voice === "analyst" ? "#5fd38a" : line.sal >= 9 ? "#ff5d5d" : "#4cc3ff"; g.fillRect(x, y, 6, h);
      g.fillStyle = "#fff"; rows.forEach((r, i) => g.fillText(r, x + 28, y + 31 + i * 40));
      g.font = "600 14px system-ui, sans-serif"; g.fillStyle = "#cfd8e3"; const who = line.voice === "analyst" ? "CO-COMMENTATOR" : "PLAY-BY-PLAY";
      g.fillStyle = "rgba(8,12,18,.86)"; g.fillRect(x, y - 26, g.measureText(who).width + 20, 26); g.fillStyle = "#cfd8e3"; g.fillText(who, x + 10, y - 12);
    }
  };
  // Draw on a plain timer. Measured in a covered browser window over 3 s: the page's video-frame callback fired 3 times,
  // setInterval 90 times; a worker timer is refused by YouTube's content security policy.
  const ticker = setInterval(() => state.phase === "recording" && draw(), 33);
  document.getElementById("movie_player")?.setPlaybackQualityRange?.("hd720", "hd720");  // a small or covered player is otherwise served 480p
  v.pause(); v.muted = true; v.currentTime = videoStart;
  await new Promise(r => v.addEventListener("seeked", r, { once: true }));
  const rec = new MediaRecorder(c.captureStream(30), { mimeType: "video/webm;codecs=vp9", videoBitsPerSecond: 6e6 }), chunks = [];
  rec.ondataavailable = e => chunks.push(e.data);
  g.drawImage(v, 0, 0, W, H);
  await v.play(); state.phase = "recording"; rec.start(1000);
  await new Promise(r => { const tick = () => v.currentTime >= videoStart + seconds || v.ended ? r() : setTimeout(tick, 200); tick(); });
  v.pause(); state.phase = "saving"; clearInterval(ticker); state.size = v.videoWidth + "x" + v.videoHeight;
  await new Promise(r => { rec.onstop = r; rec.stop(); });
  const blob = new Blob(chunks, { type: "video/webm" });
  state.bytes = blob.size;
  // Hand the recording to a local window (record_demo.py receive). A page here may not post to this machine without a
  // browser permission, and an automated tab may not download; opening a window on a click and passing it the blob may.
  const save = Object.assign(document.createElement("button"), { textContent: "Save recording", id: "save-recording" });
  save.style.cssText = "position:fixed;z-index:99999;top:90px;left:40px;padding:18px 28px;background:#1c4f78;color:#fff;font:600 18px system-ui;border:0;border-radius:8px;cursor:pointer";
  save.onclick = () => { const w = window.open(receiver + "/receive", "_blank");  // a tab, not a popup: an automated browser closed the popup at once
    addEventListener("message", e => { if (e.origin === receiver && e.data === "receiver-ready") { w.postMessage(blob, receiver); state.sent = true; } }); };
  document.getElementById("save-recording")?.remove(); document.body.append(save);
  state.phase = "done";
  return state;
};
