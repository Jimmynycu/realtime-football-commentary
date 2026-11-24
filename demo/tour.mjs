// Records a real, scripted tour of the dashboard over CDP -> demo/part_b.mp4. Node stdlib only.
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const FR = path.join(here, "tour_frames");
const OUT = path.join(here, "part_b.mp4");
const URL_ = "http://localhost:8765/dashboard";
const sleep = ms => new Promise(r => setTimeout(r, ms));
fs.rmSync(FR, { recursive: true, force: true }); fs.mkdirSync(FR, { recursive: true });

// ---- steps ---------------------------------------------------------------
const STEPS = [
  { cap: "This is the control room. Five big buttons, nothing to learn." },
  { cap: "Press Test it. It tests itself and says yes or no." },
  { cap: "It shows the AI next to a real commentator." },
  { cap: "Press Make them: a match story, player scores, best moments and subtitles." },
  { cap: "Type a name. See what the AI said about that player." },
  { cap: "Behind it, every part of the system reports its own state." },
  { cap: "Every number is measured against the human commentator." },
];

// ---- voice-over WAVs -------------------------------------------------------
const wavLen = [];
STEPS.forEach((s, i) => {
  const wav = path.join(FR, `vo_${i}.wav`), txt = path.join(FR, `vo_${i}.txt`);
  fs.writeFileSync(txt, s.cap);
  const ps = `Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SelectVoice('Microsoft Zira Desktop'); $s.Rate=1; $s.SetOutputToWaveFile('${wav}'); $s.Speak((Get-Content -Raw '${txt}')); $s.Dispose()`;
  execFileSync("powershell", ["-NoProfile", "-Command", ps]);
  wavLen.push(+execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav]).toString().trim());
});
console.log("wav lengths", wavLen.map(x => x.toFixed(2)).join(" "));

// ---- chrome ----------------------------------------------------------------
const chromePath = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"].find(fs.existsSync);
const prof = fs.mkdtempSync(path.join(os.tmpdir(), "tourprof-"));
const chrome = spawn(chromePath, ["--headless=new", "--remote-debugging-port=9333", `--user-data-dir=${prof}`, "--window-size=1280,720", "--hide-scrollbars", "--mute-audio", "about:blank"], { stdio: "ignore" });
let ws, id = 0; const pending = new Map();
async function connect() {
  for (let i = 0; i < 60; i++) {
    try {
      const t = (await (await fetch("http://127.0.0.1:9333/json")).json()).find(x => x.type === "page");
      if (t) { ws = new WebSocket(t.webSocketDebuggerUrl); await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; }); break; }
    } catch {}
    await sleep(250);
  }
  ws.onmessage = e => { const m = JSON.parse(e.data); const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
}
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval"); return r.result.value; };

const PAGE_JS = `
(() => {
  const css = document.createElement("style");
  css.textContent = \`
  #tp{position:fixed;left:900px;top:420px;width:22px;height:22px;border-radius:50%;background:#fff;border:2px solid #111;box-sizing:border-box;z-index:2147483647;pointer-events:none;transition:left .6s ease-in-out,top .6s ease-in-out;box-shadow:0 2px 6px #0008}
  #tp i{position:absolute;inset:-2px;border-radius:50%;border:3px solid #4aa3ff;opacity:0}
  #tp.click i{animation:rip .5s ease-out}
  @keyframes rip{0%{transform:scale(1);opacity:.9}100%{transform:scale(3.2);opacity:0}}
  #tc{position:fixed;left:0;right:0;bottom:0;padding:18px 24px;background:rgba(10,14,20,.88);color:#fff;font:600 26px/1.3 system-ui,Segoe UI,sans-serif;text-align:center;z-index:2147483646;pointer-events:none}\`;
  document.head.appendChild(css);
  const p = document.createElement("div"); p.id = "tp"; p.innerHTML = "<i></i>";
  const c = document.createElement("div"); c.id = "tc";
  document.documentElement.append(p, c);
  document.body.style.paddingBottom = "120px";
  window.__cap = t => { c.textContent = t; };
  window.__center = sel => { const r = document.querySelector(sel).getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  window.__move = (x, y) => { p.style.left = (x - 11) + "px"; p.style.top = (y - 11) + "px"; };
  window.__ripple = () => { p.classList.remove("click"); void p.offsetWidth; p.classList.add("click"); };
  window.__scroll = (sel, block) => new Promise(res => {
    const r = document.querySelector(sel).getBoundingClientRect(), vh = innerHeight - 100;
    const target = Math.max(0, scrollY + (block === "center" ? r.top - (vh - r.height) / 2 : r.top - 100));
    const y0 = scrollY, t0 = performance.now(), D = 900;
    (function f(n) { const k = Math.min(1, (n - t0) / D), e = k < .5 ? 2*k*k : 1 - Math.pow(-2*k + 2, 2) / 2; scrollTo(0, y0 + (target - y0) * e); k < 1 ? requestAnimationFrame(f) : res(); })(t0);
  });
  window.__wait = (sel, ms) => new Promise((res, rej) => { const t0 = Date.now(); (function f() { const e = document.querySelector(sel); if (e) return res(true); Date.now() - t0 > ms ? rej(new Error("timeout " + sel)) : setTimeout(f, 100); })(); });
})()`;

// ---- capture loop ------------------------------------------------------------
let running = false, base = 0; const frames = [];
async function capture() {
  let n = 0;
  while (running) {
    const t = performance.now();
    const r = await send("Page.captureScreenshot", { format: "jpeg", quality: 85 });
    const name = `f${String(n++).padStart(5, "0")}.jpg`;
    fs.writeFileSync(path.join(FR, name), Buffer.from(r.data, "base64"));
    frames.push({ name, t });
    const wait = 100 - (performance.now() - t); if (wait > 0) await sleep(wait);
  }
}
const now = () => (performance.now() - base) / 1000;

async function pointTo(sel) {
  const [x, y] = await ev(`__center(${JSON.stringify(sel)})`);
  await ev(`__move(${x}, ${y})`); await sleep(750);
}
async function click(sel) {
  await pointTo(sel); await ev(`__ripple()`); await sleep(250);
  await ev(`document.querySelector(${JSON.stringify(sel)}).click()`);
}

let D;
const starts = [];
try {
  await connect();
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: URL_ });
  await sleep(1500);
  await ev(`__x=0`).catch(() => {});
  await ev(`new Promise(r => { const f = () => document.getElementById("s-cmp-out") && !/Loading/.test(document.getElementById("s-cmp-out").textContent) ? r(1) : setTimeout(f, 200); f(); })`);
  await ev(PAGE_JS);
  await ev(`document.body.style.zoom = 1.5; scrollTo(0,0)`);
  await sleep(300);

  running = true; const cap = capture();
  while (!frames.length) await sleep(10);
  base = frames[0].t;

  const step = async (i, fn, hold) => {
    starts[i] = now();
    await ev(`__cap(${JSON.stringify(STEPS[i].cap)})`);
    await fn();
    await sleep(hold * 1000);
    const rest = starts[i] + wavLen[i] + 0.5 - now(); if (rest > 0) await sleep(rest * 1000);
  };

  await step(0, async () => {}, 3);
  await step(1, async () => {
    await ev(`__scroll("#s-test", "center")`); await sleep(200);
    await click("#s-test");
    await ev(`__wait("#s-test-out .verdict", 120000)`);
    await ev(`__scroll("#s-test-out", "center")`);
  }, 2);
  await step(2, async () => { await ev(`__scroll("#room-start section:nth-of-type(3)", "start")`); }, 2.5);
  await step(3, async () => {
    await ev(`__scroll("#s-make", "center")`);
    await click("#s-make");
    await ev(`__wait("#s-make-out .links a, #s-make-out:not(:empty)", 120000)`);
    await ev(`new Promise(r => { const f = () => document.querySelector("#s-make-out .links") || /wrong/.test(document.getElementById("s-make-out").textContent) ? r(1) : setTimeout(f, 100); f(); })`);
    await ev(`__scroll("#s-make-out", "center")`);
  }, 2.5);
  await step(4, async () => {
    await ev(`__scroll("#room-start section:nth-of-type(5)", "start")`); await sleep(300);
    await pointTo("#s-q"); await ev(`document.getElementById("s-q").focus()`);
    for (let k = 1; k <= 5; k++) { await ev(`(()=>{const e=document.getElementById("s-q");e.value=${JSON.stringify("Messi".slice(0, k))};e.dispatchEvent(new Event("input",{bubbles:true}))})()`); await sleep(220); }
    await click("#s-find");
    await ev(`__wait("#s-find-out .hit", 20000)`);
    await ev(`__scroll("#room-start section:nth-of-type(5)", "start")`);
  }, 3);
  await step(5, async () => {
    await ev(`scrollTo(0,0)`); await sleep(100);
    await click("#tab-system");
    await ev(`document.body.style.zoom = 1.15`);
    await ev(`__wait("#parts-line .part", 20000)`);
  }, 3);
  await step(6, async () => {
    await click("#tab-eval");
    await ev(`document.body.style.zoom = 1.15`);
    await ev(`__wait("#score-tiles .tile", 20000)`);
  }, 3.5);

  await sleep(300);
  running = false; await cap;
  const last = frames[frames.length - 1];
  D = (last.t - base) / 1000 + 0.1;
} finally {
  running = false;
  try { ws?.close(); } catch {}
  chrome.kill();
}

// ---- assemble ----------------------------------------------------------------
let list = "";
frames.forEach((f, i) => { const d = i + 1 < frames.length ? (frames[i + 1].t - f.t) / 1000 : 0.1; list += `file '${f.name}'\nduration ${d.toFixed(4)}\n`; });
list += `file '${frames[frames.length - 1].name}'\n`;
fs.writeFileSync(path.join(FR, "list.txt"), list);
const inputs = ["-f", "concat", "-safe", "0", "-i", path.join(FR, "list.txt")];
STEPS.forEach((_, i) => inputs.push("-i", path.join(FR, `vo_${i}.wav`)));
const fc = STEPS.map((_, i) => `[${i + 1}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay=${Math.round(starts[i] * 1000)}|${Math.round(starts[i] * 1000)}[a${i}]`).join(";")
  + `;${STEPS.map((_, i) => `[a${i}]`).join("")}amix=inputs=${STEPS.length}:normalize=0:duration=longest,apad[aout]`;
execFileSync("ffmpeg", ["-y", "-v", "error", ...inputs, "-filter_complex", fc,
  "-map", "0:v", "-map", "[aout]", "-vf", "fps=30,scale=1280:720,format=yuv420p", "-fps_mode", "cfr",
  "-c:v", "libx264", "-crf", "22", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
  "-t", D.toFixed(3), "-movflags", "+faststart", OUT], { stdio: "inherit" });
console.log("step starts", starts.map(s => s.toFixed(1)).join(" "), "duration", D.toFixed(2), "frames", frames.length);
