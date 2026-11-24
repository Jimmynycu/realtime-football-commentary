// Vision mode overlay: works on ANY YouTube football video, live or recorded.
// Grabs the frame that is on screen, posts it to the local vision server, shows what the model reads and says.
// No event data and no audio are used. No innerHTML: YouTube enforces Trusted Types.
(() => {
  if (window.__aiLive) return;
  window.__aiLive = true;
  const API = "http://localhost:8770";
  const h = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
  const CSS = `
    :host { all: initial; position: fixed; top: 56px; right: 0; bottom: 0; width: 370px; z-index: 2300; font: 14px/1.4 system-ui, "Segoe UI", sans-serif; color: #e8eef5; }
    .wrap { height: 100%; display: flex; flex-direction: column; gap: 10px; padding: 10px; background: #0b1016f2; border-left: 1px solid #243142; box-sizing: border-box; }
    .top { display: flex; align-items: center; gap: 8px; font-weight: 700; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #8a99ab; flex: none; } .dot.live { background: #ff5d5d; box-shadow: 0 0 8px #ff5d5d; }
    .status { color: #8a99ab; font-weight: 400; font-size: 12px; flex: 1; }
    h4 { margin: 0; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #8a99ab; font-weight: 600; }
    .row { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; font-size: 12.5px; }
    .chips { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
    .chip { background: #141c26; border: 1px solid #243142; border-radius: 6px; padding: 5px 8px; min-width: 0; }
    .chip span { display: block; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: #8a99ab; }
    .chip b { font-size: 14px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .raw { font: 12px/1.45 ui-monospace, Consolas, monospace; color: #9fb3c8; background: #141c26; border-radius: 6px; padding: 7px 9px; min-height: 52px; overflow-wrap: anywhere; }
    .feed { flex: 1; min-height: 100px; overflow-y: auto; display: flex; flex-direction: column; gap: 7px; }
    .ln { border-left: 3px solid #243142; padding: 2px 0 2px 9px; }
    .ln.hot { border-color: #ff5d5d; font-weight: 700; font-size: 16px; }
    .ln small { display: block; color: #8a99ab; font-weight: 400; font-size: 11px; }
    .ops { display: grid; grid-template-columns: repeat(4, 1fr); text-align: center; font-size: 10.5px; color: #8a99ab; }
    .ops b { display: block; font-size: 15px; color: #e8eef5; font-variant-numeric: tabular-nums; }
  `;
  const ui = {}, lags = [], cycles = [];
  let es = null, busy = false, sent = 0, lines = 0, capTimer;
  const canvas = Object.assign(document.createElement("canvas"), { width: 1280, height: 720 }), ctx = canvas.getContext("2d");
  const video = () => document.querySelector("#movie_player video");
  const mid = a => [...a].sort((x, y) => x - y)[a.length >> 1];

  function build(meta) {
    const host = h("div"), root = host.attachShadow({ mode: "open" });
    ui.dot = h("i", { className: "dot" });
    ui.status = h("span", { className: "status", textContent: "waiting for video" });
    ui.voice = h("input", { type: "checkbox", checked: true, onchange: () => ui.voice.checked || speechSynthesis.cancel() });
    ui.mute = h("input", { type: "checkbox", checked: true, onchange: () => { const v = video(); if (v) v.muted = ui.mute.checked; } });
    ui.chip = {};
    const chips = [["clock", "clock"], ["score", "score"], ["view", "view"], ["goal", "goal in view"], ["event", "sees"], ["ms", "cycle ms"]]
      .map(([k, n]) => h("div", { className: "chip" }, h("span", { textContent: n }), ui.chip[k] = h("b", { textContent: "–" })));
    ui.raw = h("div", { className: "raw", textContent: "model output appears here" });
    ui.feed = h("div", { className: "feed" });
    ui.ops = {};
    const ops = [["frames", "frames sent"], ["lines", "lines"], ["lag", "lag s (p50)"], ["sup", "superseded"]]
      .map(([k, n]) => h("div", {}, ui.ops[k] = h("b", { textContent: "0" }), n));
    root.append(h("style", { textContent: CSS }), h("div", { className: "wrap" },
      h("div", { className: "top" }, ui.dot, "AI vision commentary", ui.status),
      h("div", { className: "row" }, h("label", {}, ui.voice, " AI voice"), h("label", {}, ui.mute, " Mute video"), h("span", { style: "color:#8a99ab", textContent: meta.model })),
      h("h4", { textContent: "What the model reads from the picture" }), h("div", { className: "chips" }, ...chips), ui.raw,
      h("h4", { textContent: "Commentary (from the picture only)" }), ui.feed,
      h("div", { className: "ops" }, ...ops)));
    document.addEventListener("click", () => speechSynthesis.speak(new SpeechSynthesisUtterance("")), { once: true, capture: true });
    document.body.append(host);
    ui.cap = h("div", { style: "position:absolute;left:4%;right:4%;bottom:14%;text-align:center;pointer-events:none;z-index:60;opacity:0;transition:opacity .25s" },
      ui.capText = h("span", { style: "display:inline-block;background:rgba(4,8,12,.86);color:#fff;padding:6px 16px;border-radius:6px;font:650 clamp(15px,1.6vw,26px)/1.3 system-ui,sans-serif;border-left:5px solid #4cc3ff" }));
  }

  // ---- capture: send the frame on screen; never queue (the server keeps only the newest one)
  async function tick() {
    const v = video(), mp = document.querySelector("#movie_player");
    if (!v || !mp) return;
    if (ui.cap.parentNode !== mp) mp.append(ui.cap);
    const playing = !v.paused && v.readyState >= 3 && !mp.classList.contains("ad-showing");
    ui.dot.className = playing ? "dot live" : "dot";
    if (!playing) { ui.status.textContent = v.paused ? "paused" : "waiting"; return; }
    if (v.muted !== ui.mute.checked) v.muted = ui.mute.checked;
    if (!es) connect();
    if (busy) return;
    busy = true;
    try {
      ctx.drawImage(v, 0, 0, 1280, 720);
      const blob = await new Promise(r => canvas.toBlob(r, "image/jpeg", 0.8));
      const src = new URLSearchParams(location.search).get("v") || location.pathname;
      await fetch(`${API}/frame?t=${v.currentTime.toFixed(2)}&src=${encodeURIComponent(src)}`, { method: "POST", body: blob });
      ui.ops.frames.textContent = ++sent;
      ui.status.textContent = "LIVE · reading the picture";
    } catch { ui.status.textContent = "vision server not reachable"; }
    busy = false;
  }
  function connect() {
    es = new EventSource(API + "/stream");
    es.onmessage = ev => handle(JSON.parse(ev.data));
    es.onerror = () => { es.close(); es = null; };
  }
  function handle(m) {
    const v = video();
    if (m.type === "obs") {
      for (const k of ["clock", "score", "view", "goal", "event"]) ui.chip[k].textContent = m[k] ?? "–";
      cycles.push(m.ms); if (cycles.length > 40) cycles.shift();
      ui.chip.ms.textContent = mid(cycles);
      ui.raw.textContent = Object.entries(m.raw).map(([k, t]) => k + ": " + t).join("\n");
      ui.ops.sup.textContent = m.superseded;
    } else if (m.type === "line") {
      const lag = v ? Math.max(0, v.currentTime - m.frame_t) : 0;
      lags.push(lag); ui.ops.lag.textContent = mid(lags).toFixed(1); ui.ops.lines.textContent = ++lines;
      ui.feed.prepend(h("div", { className: "ln" + (m.sal >= 8 ? " hot" : "") }, m.text,
        h("small", { textContent: `${m.kind.slice(2)} · salience ${m.sal} · ${lag.toFixed(1)} s after the frame` })));
      while (ui.feed.childElementCount > 80) ui.feed.lastElementChild.remove();
      (window.__liveLog = window.__liveLog || []).push([+m.frame_t, +lag.toFixed(2), m.kind, m.text]);
      ui.capText.textContent = m.text;
      ui.capText.style.borderLeftColor = m.sal >= 8 ? "#ff5d5d" : "#4cc3ff";
      ui.cap.style.opacity = 1;
      clearTimeout(capTimer); capTimer = setTimeout(() => ui.cap.style.opacity = 0, 5000);
      if (!ui.voice.checked) return;
      if (m.sal >= 7) speechSynthesis.cancel(); else if (speechSynthesis.speaking || speechSynthesis.pending) return;
      const u = new SpeechSynthesisUtterance(m.text);
      const en = speechSynthesis.getVoices().filter(x => x.lang.startsWith("en"));
      if (en.length) u.voice = en[0];
      u.lang = "en-GB"; u.rate = 1.2; u.pitch = 1.1;
      speechSynthesis.speak(u);
    }
  }

  (async () => {
    let meta;
    try { meta = await (await fetch(API + "/meta")).json(); }
    catch { return console.warn("[AI vision] local vision server not reachable at " + API + " — run start_vision.cmd"); }
    build(meta);
    setInterval(tick, 300);
  })();
})();
