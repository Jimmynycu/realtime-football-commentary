// Records the side-by-side demo inside the YouTube tab: the broadcast picture, the human commentator and the system on
// one clock. Everything is drawn on a canvas and recorded with the broadcast's own sound; nothing else on the screen is
// captured. See record_demo.py. Usage:  recordDemo(<contents of demo/lines.json>)  then click "Save recording".
// Each passage is played twice: first with the broadcast's own sound (the human), then again with the system's voice.
window.recordDemo = async ({ lines, human, goals, passes, receiver = "http://localhost:8799" }) => {
  const state = window.__rec = { phase: "seeking", frames: 0 };
  const v = document.querySelector("video"), W = 1280, H = 720, VW = 960, VH = 540, SPAN = 26, X0 = 150, X1 = W - 36;
  const c = Object.assign(document.createElement("canvas"), { width: W, height: H }), g = c.getContext("2d");
  const HUMAN = "#e0912a", AI = "#4cc3ff", COLOUR = "#5fd38a", HOT = "#ff5d5d", INK = "#e8eef5", DIM = "#8a99ab", BG = "#0b1016", PANEL = "#141c26";
  const font = (px, w = 600) => g.font = `${w} ${px}px system-ui, "Segoe UI", sans-serif`;
  const wrap = (text, max) => { const out = [""]; for (const w of text.split(" ")) { if (g.measureText(out.at(-1) + " " + w).width > max && out.at(-1)) out.push(w); else out[out.length - 1] = (out.at(-1) + " " + w).trim(); } return out; };
  // the commentator's stretches of speech: caption words less than 0.7 s apart are one block
  const blocks = []; for (const [t] of human) { const b = blocks.at(-1); if (b && t - b[1] <= 0.7) b[1] = t + 0.3; else blocks.push([t - 0.1, t + 0.3]); }
  const x = (t, now) => X1 - (now - t) * (X1 - X0) / SPAN;
  const tag = (text, tx, y, colour) => { font(13); const w = g.measureText(text).width + 12; g.fillStyle = BG; g.fillRect(tx - 2, y - 9, w + 4, 20); g.fillStyle = colour; g.fillRect(tx, y - 8, 3, 18); g.fillStyle = INK; g.fillText(text, tx + 8, y + 1); };

  const draw = () => {
    const now = v.currentTime, pass = state.pass || passes[0], from = pass[0]; state.frames++;
    g.fillStyle = BG; g.fillRect(0, 0, W, H); g.textBaseline = "middle";
    g.drawImage(v, 0, 0, VW, VH);
    const line = lines.filter(l => l.t <= now).at(-1);
    if (line && now - line.t < (line.voice === "analyst" ? 9 : 5)) {  // the system's line, on the picture, as in the live overlay
      font(24); const rows = wrap(line.text, VW - 160), w = Math.max(...rows.map(r => g.measureText(r).width)) + 40, h = rows.length * 32 + 18, bx = (VW - w) / 2, by = VH - 26 - h;
      g.fillStyle = "rgba(8,12,18,.86)"; g.fillRect(bx, by, w, h); g.fillStyle = line.voice === "analyst" ? COLOUR : line.sal >= 9 ? HOT : AI; g.fillRect(bx, by, 5, h);
      g.fillStyle = "#fff"; rows.forEach((r, i) => g.fillText(r, bx + 22, by + 9 + 16 + i * 32));
    }
    // ---- right panel: what is being compared, what is audible, the running count
    const px = VW + 18; g.fillStyle = PANEL; g.fillRect(VW, 0, W - VW, VH);
    font(19, 700); g.fillStyle = INK; g.fillText("Same picture, same clock", px, 30);
    font(13, 400); g.fillStyle = DIM; ["The broadcast's commentator and the", "system, on the 2022 World Cup final."].forEach((r, i) => g.fillText(r, px, 56 + i * 18));
    const who = pass[2];
    g.fillStyle = who === "human" ? HUMAN : AI; g.fillRect(px, 104, W - VW - 36, 62);
    font(11, 700); g.fillStyle = BG; g.fillText("NOW HEARING · " + (state.label || ""), px + 12, 120);
    font(18, 700); g.fillText(who === "human" ? "Human commentator" : "AI system", px + 12, 146);
    const hw = human.filter(h => h[0] >= from && h[0] <= now).length, hn = human.filter(h => h[0] >= from && h[0] <= now && h[1]).length;
    const mine = lines.filter(l => l.t >= from && l.t <= now);
    const aw = Math.round(mine.reduce((n, l) => n + l.n * Math.min(1, (now - l.t) / l.dur), 0)), an = mine.reduce((n, l) => n + l.names.length, 0);
    const pair = (label, a, b, y) => { font(12, 600); g.fillStyle = DIM; g.fillText(label.toUpperCase(), px, y);
      font(34, 700); g.fillStyle = HUMAN; g.fillText(a, px, y + 34); g.fillStyle = AI; g.fillText(b, px + 142, y + 34);
      font(12, 400); g.fillStyle = DIM; g.fillText("human", px, y + 60); g.fillText("AI system", px + 142, y + 60); };
    pair("Words in this passage", hw, aw, 200); pair("Players named in this passage", hn, an, 300);
    const goal = goals.find(k => now >= k.shot && k.shot >= from);
    if (goal) { g.fillStyle = BG; g.fillRect(px, 396, W - VW - 36, 126); font(12, 600); g.fillStyle = HOT; g.fillText(`GOAL · ${goal.who.toUpperCase()} NAMED AFTER THE SHOT`, px + 10, 414);
      const lag = t => t == null ? "not named" : (t - goal.shot >= 0 ? "+" : "") + (t - goal.shot).toFixed(1) + " s";
      font(26, 700); g.fillStyle = HUMAN; g.fillText(goal.human != null && now >= goal.human ? lag(goal.human) : "…", px + 10, 448); g.fillStyle = AI; g.fillText(goal.ai != null && now >= goal.ai ? lag(goal.ai) : "…", px + 152, 448);
      font(12, 400); g.fillStyle = DIM; g.fillText("human", px + 10, 474); g.fillText("AI system", px + 152, 474);
      font(11, 400); ["The system reads an event feed, which marks", "the goal at the shot; a live feed adds its delay."].forEach((r, i) => g.fillText(r, px + 10, 494 + i * 14)); }
    // ---- two lanes on one clock: a block for every stretch of speech, a tag for every player named
    const lane = (y, label, sub, colour) => { g.fillStyle = PANEL; g.fillRect(0, y, W, 74); font(14, 700); g.fillStyle = colour; g.fillText(label, 16, y + 28); font(11, 400); g.fillStyle = DIM; g.fillText(sub, 16, y + 48); };
    lane(VH + 8, "HUMAN", "speech · players named", HUMAN); lane(VH + 90, "AI SYSTEM", "speech · players named", AI);
    g.save(); g.beginPath(); g.rect(X0, VH, X1 - X0 + 4, H - VH); g.clip();
    for (const [a, b] of blocks) if (a <= now && b > now - SPAN) { g.fillStyle = HUMAN; g.fillRect(x(a, now), VH + 46, Math.max(3, x(Math.min(b, now), now) - x(a, now)), 22); }
    for (const [t, flag, name] of human) if (flag && t <= now && t > now - SPAN) tag(name || "name", x(t, now), VH + 26, HUMAN);
    for (const l of lines) if (l.t <= now && l.t + l.dur > now - SPAN) { g.fillStyle = l.voice === "analyst" ? COLOUR : l.sal >= 9 ? HOT : AI;
      g.fillRect(x(l.t, now), VH + 128, Math.max(3, x(Math.min(l.t + l.dur, now), now) - x(l.t, now)), 22);
      l.names.forEach((n, i) => l.t + i * 0.9 <= now && tag(n, x(l.t + i * 0.9, now), VH + 108, l.voice === "analyst" ? COLOUR : AI)); }
    g.restore();
    g.fillStyle = INK; g.fillRect(X1, VH + 8, 2, 156); font(11, 600); g.fillStyle = DIM; g.fillText("now", X1 - 30, VH + 172); g.fillText(`${SPAN} s ago`, X0, VH + 172);
    if (state.card) { font(30, 700); const w = g.measureText(state.card).width + 80; g.fillStyle = "rgba(8,12,18,.9)"; g.fillRect((VW - w) / 2, VH / 2 - 44, w, 88);
      g.fillStyle = who === "human" ? HUMAN : AI; g.fillRect((VW - w) / 2, VH / 2 - 44, 8, 88); g.fillStyle = "#fff"; g.fillText(state.card, (VW - w) / 2 + 44, VH / 2); }
    font(11, 400); g.fillStyle = DIM; g.fillText("Human lane: when the commentator speaks and whom he names, measured from the broadcast's captions. His words are not reproduced.", X0 + 70, VH + 172);
  };

  // Draw on a plain timer. Measured in a covered browser window over 3 s: the page's video-frame callback fired 3 times,
  // setInterval 90 times; a worker timer is refused by YouTube's content security policy.
  const ticker = setInterval(() => state.phase === "recording" && draw(), 33);
  document.getElementById("movie_player")?.setPlaybackQualityRange?.("hd720", "hd720");  // a small or covered player is otherwise served 480p
  // the broadcast's own sound, routed into the recording instead of the speakers (an element can be tapped only once)
  const ac = window.__ac ||= new AudioContext(), dest = ac.createMediaStreamDestination();
  (window.__src ||= ac.createMediaElementSource(v)).connect(dest);
  v.pause(); v.muted = false; v.volume = 1;
  const seek = t => new Promise(r => { v.addEventListener("seeked", r, { once: true }); v.currentTime = t; });
  const wait = ms => new Promise(r => setTimeout(r, ms));
  await seek(passes[0][0]);
  const rec = new MediaRecorder(new MediaStream([...c.captureStream(30).getVideoTracks(), ...dest.stream.getAudioTracks()]),
    { mimeType: "video/webm;codecs=vp9,opus", videoBitsPerSecond: 6e6 }), chunks = [];
  rec.ondataavailable = e => chunks.push(e.data);
  await ac.resume(); state.pass = passes[0]; state.marks = []; state.phase = "recording"; rec.start(1000);
  const t0 = performance.now();
  for (const [i, pass] of passes.entries()) {
    state.pass = pass; state.label = `PASSAGE ${Math.floor(i / 2) + 1} · TAKE ${i % 2 + 1} OF 2`;
    await seek(pass[0]);
    state.card = pass[2] === "human" ? "First: the broadcast's own commentator" : "The same seconds again: the AI system";
    await wait(2000); state.card = null;
    await v.play();
    state.marks.push([+((performance.now() - t0) / 1000).toFixed(2), pass[0], pass[1], pass[2]]);  // where this take starts in the recording: the sound mix needs it
    await new Promise(r => { const tick = () => v.currentTime >= pass[1] || v.ended ? r() : setTimeout(tick, 100); tick(); });
    v.pause();
  }
  await wait(600);
  state.phase = "saving"; clearInterval(ticker); state.size = v.videoWidth + "x" + v.videoHeight;
  await new Promise(r => { rec.onstop = r; rec.stop(); });
  const blob = new Blob(chunks, { type: "video/webm" });
  state.bytes = blob.size;
  // Hand the recording to a local tab (record_demo.py receive). A page here may not post to this machine without a
  // browser permission, and an automated tab may not download; opening a tab on a click and passing it the blob may.
  const save = Object.assign(document.createElement("button"), { textContent: "Save recording", id: "save-recording" });
  save.style.cssText = "position:fixed;z-index:99999;top:90px;left:40px;padding:18px 28px;background:#1c4f78;color:#fff;font:600 18px system-ui;border:0;border-radius:8px;cursor:pointer";
  save.onclick = () => { const w = window.open(receiver + "/receive", "_blank");  // a tab, not a popup: an automated browser closed the popup at once
    addEventListener("message", e => { if (e.origin === receiver && e.data === "receiver-ready") { w.postMessage(blob, receiver); state.sent = true; } }); };
  document.getElementById("save-recording")?.remove(); document.body.append(save);
  state.phase = "done";
  return state;
};
