// AI commentary overlay for the real YouTube broadcast.
// Reads the <video> clock, maps it to match time, and streams commentary from the local server.
// No innerHTML anywhere: YouTube enforces Trusted Types.
(() => {
  if (window.__aiCommentary) return;
  window.__aiCommentary = true;
  const API = "http://localhost:8765";
  const RATE = 1.5;  // speech rate; booth.py SPEAK must match the words per second this produces
  const h = (tag, props = {}, ...kids) => {
    const e = Object.assign(document.createElement(tag), props);
    e.append(...kids);
    return e;
  };
  const CSS = `
    :host { all: initial; position: fixed; top: 56px; right: 0; bottom: 0; width: 370px; z-index: 2300; font: 14px/1.4 system-ui, "Segoe UI", sans-serif; color: #e8eef5; }
    .wrap { height: 100%; display: flex; flex-direction: column; gap: 10px; padding: 10px; background: #0b1016f2; border-left: 1px solid #243142; box-sizing: border-box; }
    .wrap.min { height: auto; } .wrap.min > :not(.top) { display: none; }
    .top { display: flex; align-items: center; gap: 8px; font-weight: 700; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #8a99ab; flex: none; } .dot.live { background: #ff5d5d; box-shadow: 0 0 8px #ff5d5d; }
    .status { color: #8a99ab; font-weight: 400; font-size: 12px; flex: 1; }
    button, select, input[type=number] { background: #141c26; color: inherit; border: 1px solid #243142; border-radius: 6px; padding: 4px 8px; font: inherit; }
    input[type=number] { width: 56px; } button { cursor: pointer; }
    .row { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; font-size: 12.5px; }
    h4 { margin: 0; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #8a99ab; font-weight: 600; }
    .feed { flex: 1; min-height: 120px; overflow-y: auto; display: flex; flex-direction: column; gap: 7px; }
    .ln { border-left: 3px solid #243142; padding: 2px 0 2px 9px; }
    .ln.hot { border-color: #ff5d5d; font-weight: 700; font-size: 16px; }
    .ln.analyst { border-color: #5fd38a; background: #12231b; }
    .ln small { display: block; color: #8a99ab; font-weight: 400; font-size: 11px; } .fb { color: #ffb347; }
    .stat { display: grid; grid-template-columns: 40px 1fr 40px; gap: 6px; align-items: center; font-variant-numeric: tabular-nums; font-size: 12.5px; }
    .stat .n { grid-column: 1 / -1; text-align: center; color: #8a99ab; font-size: 11px; margin-bottom: -4px; } .stat .r { text-align: right; }
    .bar { height: 6px; border-radius: 3px; background: #ffb347; overflow: hidden; } .bar i { display: block; height: 100%; background: #4cc3ff; }
    canvas { width: 100%; height: 54px; display: block; }
    .ops { display: grid; grid-template-columns: repeat(5, 1fr); text-align: center; font-size: 10.5px; color: #8a99ab; }
    .ops b { display: block; font-size: 15px; color: #e8eef5; font-variant-numeric: tabular-nums; }
  `;
  const ROWS = [["possession_pct", "Possession %"], ["shots", "Shots"], ["xg", "Expected goals"], ["pass_pct", "Pass accuracy %"]];
  let meta, P, V, nudge = 0, es = null, live = null, capTimer;
  let mom = [], lats = [], lines = 0, fallbacks = 0, resyncs = 0;
  const ui = {};

  const video = () => document.querySelector("#movie_player video");
  const onMatchPage = () => new URLSearchParams(location.search).get("v") === meta.video.id;

  // ---- clock mapping: broadcast video time <-> continuous match time
  function videoToMatch(v) {
    let cur = null;
    for (const p of P) if (v >= V[p.p] + nudge) cur = p;
    if (!cur) return { live: false, why: "Pre-match" };
    const ts = v - (V[cur.p] + nudge);
    if (ts > cur.len + 1) return { live: false, why: cur.p === P.at(-1).p ? "Full time" : "Break in play" };
    return { live: true, t: cur.start + ts };
  }
  function matchToVideo(t) {
    let cur = P[0];
    for (const p of P) if (t >= p.start) cur = p;
    return V[cur.p] + nudge + (t - cur.start);
  }

  function build() {
    const host = h("div"), root = host.attachShadow({ mode: "open" });
    ui.host = host;
    ui.dot = h("i", { className: "dot" });
    ui.status = h("span", { className: "status", textContent: "starting…" });
    ui.wrap = h("div", { className: "wrap" });
    const min = h("button", { textContent: "–", title: "Collapse / expand", onclick: () => ui.wrap.classList.toggle("min") });
    ui.jump = h("select", { title: "Jump to a key moment" });
    for (const m of meta.moments) ui.jump.add(new Option(m.label, m.t));
    ui.jump.onchange = () => { const v = video(); v.currentTime = matchToVideo(parseFloat(ui.jump.value)); v.play(); };
    ui.voice = h("input", { type: "checkbox", checked: true, onchange: () => ui.voice.checked || speechSynthesis.cancel() });
    ui.mute = h("input", { type: "checkbox", checked: true, onchange: () => { const v = video(); if (v) v.muted = ui.mute.checked; } });
    ui.nudge = h("input", { type: "number", step: "0.5", value: "0", title: "Seconds added to the video-to-match mapping; positive = commentary later",
                            onchange: () => { nudge = parseFloat(ui.nudge.value) || 0; } });
    ui.feed = h("div", { className: "feed" });
    ui.stats = {};
    const stats = ROWS.map(([k, n]) => {
      const s = ui.stats[k] = { h: h("span", { textContent: "0" }), a: h("span", { className: "r", textContent: "0" }), b: h("i") };
      return h("div", { className: "stat" }, h("span", { className: "n", textContent: n }), s.h, h("div", { className: "bar" }, s.b), s.a);
    });
    ui.mom = h("canvas", { width: 700, height: 108 });
    ui.ops = {};
    const ops = [["lines", "on air"], ["drop", "superseded"], ["fb", "fallbacks"], ["lat", "p50 ms"], ["sync", "resyncs"]]
      .map(([k, n]) => h("div", {}, ui.ops[k] = h("b", { textContent: k === "lat" ? "–" : "0" }), n));
    ui.wrap.append(
      h("div", { className: "top" }, ui.dot, "AI commentary", ui.status, min),
      h("div", { className: "row" }, h("label", {}, "Jump ", ui.jump), h("label", {}, ui.voice, " AI voice"), h("label", {}, ui.mute, " Mute broadcast"), h("label", {}, "Sync ", ui.nudge, " s")),
      h("h4", { textContent: `${meta.home} v ${meta.away} · ${meta.llm || "template commentator"}` }), ui.feed,
      h("h4", { textContent: "Match stats" }), ...stats,
      h("h4", { textContent: "Momentum per minute" }), ui.mom,
      h("div", { className: "ops" }, ...ops));
    root.append(h("style", { textContent: CSS }), ui.wrap);
    // Browsers only allow speech after a user gesture: the first click anywhere on the page (e.g. Play) unlocks it.
    document.addEventListener("click", () => speechSynthesis.speak(new SpeechSynthesisUtterance("")), { once: true, capture: true });
    document.body.append(host);
    // Lower-third caption lives inside the player so it is on the real picture, also in fullscreen.
    ui.cap = h("div", { style: "position:absolute;left:4%;right:4%;bottom:14%;text-align:center;pointer-events:none;z-index:60;opacity:0;transition:opacity .25s" },
      ui.capText = h("span", { style: "display:inline-block;background:rgba(4,8,12,.86);color:#fff;padding:6px 16px;border-radius:6px;font:650 clamp(15px,1.6vw,26px)/1.3 system-ui,sans-serif;border-left:5px solid #4cc3ff" }));
  }

  function status(on, text) { ui.dot.className = on ? "dot live" : "dot"; ui.status.textContent = text; }
  function connect(t, rate) {
    if (es) es.close();
    live = { t0: t, w0: performance.now(), rate };
    es = new EventSource(`${API}/stream?speed=${rate}&t=${t.toFixed(2)}`);
    es.onmessage = ev => handle(JSON.parse(ev.data));
    es.onerror = () => { es && es.close(); es = null; };  // never auto-replay from a stale t
  }
  function disconnect() {
    if (!es) return;
    es.close(); es = null; live = null; speechSynthesis.cancel();
  }

  // ---- the real video is the clock: follow play / pause / seek / speed / ads
  function tick() {
    const here = onMatchPage();
    ui.host.style.display = here ? "" : "none";
    const v = here && video(), mp = document.querySelector("#movie_player");
    if (!v || !mp) return disconnect();
    if (ui.cap.parentNode !== mp) mp.append(ui.cap);
    if (mp.classList.contains("ad-showing")) { disconnect(); return status(false, "ad playing"); }
    if (v.paused || v.readyState < 3) { disconnect(); return status(false, v.paused ? "paused" : "buffering"); }
    const m = videoToMatch(v.currentTime), rate = v.playbackRate;
    if (!m.live) { disconnect(); return status(false, m.why); }
    const expected = live ? live.t0 + (performance.now() - live.w0) / 1000 * live.rate : NaN;
    if (!es || live.rate !== rate || Math.abs(expected - m.t) > 1.5) {   // first play, seek, stall, pause or speed change
      ui.ops.sync.textContent = resyncs++;
      connect(m.t, rate);
    }
    status(true, `LIVE ${rate}× · drift ${(Number.isFinite(expected) ? expected - m.t : 0).toFixed(1)} s`);
  }

  function handle(m) {
    if (m.type === "init") { mom = m.mom; drawMom(); }
    else if (m.type === "end") { es.close(); es = null; }
    else if (m.type === "state") {
      ui.ops.drop.textContent = m.dropped;
      const [hs, as] = [m.stats[meta.home], m.stats[meta.away]];
      for (const [k] of ROWS) {
        ui.stats[k].h.textContent = hs[k]; ui.stats[k].a.textContent = as[k];
        ui.stats[k].b.style.width = (hs[k] + as[k] ? 100 * hs[k] / (hs[k] + as[k]) : 50) + "%";
      }
      const [b, hv, av] = m.mom; mom[b] = [hv, av]; drawMom();
    } else if (m.type === "line") onLine(m);
  }
  function onLine(l) {
    lines++; lats.push(l.ms); if (l.source.startsWith("fallback")) fallbacks++;
    ui.ops.lines.textContent = lines; ui.ops.fb.textContent = fallbacks;
    ui.ops.lat.textContent = [...lats].sort((x, y) => x - y)[lats.length >> 1];
    const cls = l.voice === "analyst" ? "analyst" : l.sal >= 9 ? "hot" : "";
    ui.feed.prepend(h("div", { className: "ln " + cls }, l.text,
      h("small", {}, `${l.minute}' · ${l.voice === "analyst" ? "ANALYST" : "PLAY-BY-PLAY"} · salience ${l.sal} · `,
        h("span", { className: l.source.startsWith("fallback") ? "fb" : "", textContent: `${l.source} · ${l.ms} ms` }))));
    while (ui.feed.childElementCount > 80) ui.feed.lastElementChild.remove();
    ui.capText.textContent = l.text;
    ui.capText.style.borderLeftColor = cls === "analyst" ? "#5fd38a" : cls === "hot" ? "#ff5d5d" : "#4cc3ff";
    ui.cap.style.opacity = 1;
    clearTimeout(capTimer); capTimer = setTimeout(() => ui.cap.style.opacity = 0, l.voice === "analyst" ? 9000 : 5000);
    if (!ui.voice.checked) return;
    const cut = l.cuts || l.sal >= 7;
    if (cut) speechSynthesis.cancel();                    // the booth decided this line interrupts what is being said
    else if (speechSynthesis.pending) return;             // at most one line waiting: audio must not drift behind the picture
    const u = new SpeechSynthesisUtterance(l.text);
    // Two different people: a British male play-by-play voice and a female co-commentator, when the browser has them.
    // Measured in Chrome: the Google voices at 1.25 finish inside booth.speak_time (1.3 + 0.24 n s), as the local ones do at 1.5.
    const en = speechSynthesis.getVoices().filter(v => v.lang.startsWith("en"));
    const want = l.voice === "analyst" ? ["Google UK English Female", "Google US English"] : ["Google UK English Male"];
    u.voice = want.map(n => en.find(v => v.name === n)).find(Boolean) || en[l.voice === "analyst" ? Math.min(1, en.length - 1) : 0] || null;
    const google = u.voice && !u.voice.localService;
    u.lang = "en-GB"; u.rate = google ? 1.25 : RATE; u.pitch = google ? 1 : l.voice === "analyst" ? 0.85 : 1.1;
    // Measured in Chrome: with the network voices, speak() in the same task as cancel() never starts and blocks every later
    // line (the goal call went silent, and so did the minute after it). So speak a beat later, and if a line that should
    // start at once has not started in two seconds, clear the queue so the next one can.
    setTimeout(() => {
      const idle = !speechSynthesis.speaking;
      speechSynthesis.speak(u);
      if (idle) { const g = setTimeout(() => speechSynthesis.cancel(), 2000); u.onstart = u.onerror = () => clearTimeout(g); }
    }, cut ? 150 : 0);
  }
  function drawMom() {
    const c = ui.mom, g = c.getContext("2d"), W = c.width, H = c.height, mid = H / 2;
    g.clearRect(0, 0, W, H);
    const n = Math.max(150, mom.length), w = W / n, max = Math.max(4, ...mom.flat().filter(Number.isFinite));
    mom.forEach((r, i) => {
      if (!r) return;
      g.fillStyle = "#4cc3ff"; g.fillRect(i * w, mid - (r[0] / max) * mid, w - 1, (r[0] / max) * mid);
      g.fillStyle = "#ffb347"; g.fillRect(i * w, mid, w - 1, (r[1] / max) * mid);
    });
  }

  (async () => {
    try { meta = await (await fetch(API + "/meta")).json(); }
    catch { return console.warn("[AI commentary] local server not reachable at " + API + " — run: python server.py"); }
    if (!meta.video) return;
    P = meta.periods; V = meta.video.kickoff;
    build();
    const v = video(); if (v && onMatchPage()) v.muted = true;
    setInterval(tick, 400);
  })();
})();
