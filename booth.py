"""The commentary booth: second by second, who speaks and about what.

    python booth.py            tune the booth against the human commentator and print the comparison
    python booth.py --show     also print two minutes of output

Two voices share one microphone:
  play-by-play  follows the ball: names on the receive, actions, restarts, shots. More of it the closer the ball is to goal.
  colour        talks when the ball is dead or far from goal: a verdict on what just happened, how the match is going,
                background on the people involved, the stakes (colour.py).
The microphone is never left open for long: when play-by-play has nothing, colour fills.
How often each speaks in each phase is not hand-set. `tune()` adjusts it until the booth's names per minute and words per
minute match what the human commentator of the same match did (measured from the broadcast captions).
"""
from __future__ import annotations

import bisect
import json
import re
import sys
from collections import defaultdict

import engine
import game
from colour import Colour, Writer, short

def speak_time(n_words: int) -> float:
    """Seconds the browser voice needs for a line, measured in Chrome with the voice the overlay prefers (Google UK English
    Male at rate 1.25): 1 word 0.9 s, 3 words 1.6 s, 5 words 1.8 s, 8 words 2.8 s, 10 words 3.4 s, 14 words 4.4 s, 20 words 5.4 s.
    The older local Windows voices need about 0.7 s more to start; with those the overlay drops a line rather than fall behind."""
    return 0.6 + 0.28 * n_words
RUNS = (1, 1)  # follow-on sentences after the first of a run. Pairs match the human pause distribution best of six patterns tried
PHASES = ["build-up", "progression", "final-third attack", "counter-attack", "chance", "set piece", "dead ball"]
FILL_OK = {"build-up", "progression", "set piece", "dead ball"}  # colour may start here; near goal the ball owns the microphone
PARAMS_FILE = engine.ROOT / "booth_params.json"
# name_gap: seconds between named calls. fill_after: seconds of open microphone before colour starts. rich: fuller wording.
START = {p: {"name_gap": 8.0, "fill_after": 3.0, "rich": False, "named_colour": 0.3} for p in PHASES}
EVENT_KINDS = {"goal", "pen_goal", "pen_miss", "so_goal", "so_miss", "shot_saved", "shot_post", "shot_off", "shot_blocked", "penalty_awarded",
               "red_card", "yellow_card", "foul", "handball", "substitution", "offside", "kick_off", "restart", "break", "shootout_over", "corner"}


def zone(loc: list) -> str | None:
    """Where on the pitch, in the words a commentator uses; None where the place is not worth a word."""
    x, y = loc[0], loc[1]
    side = "left" if y < 40 else "right"
    if x >= 102 and 18 <= y <= 62:
        return "in the box"
    if x >= 110:
        return "by the byline"
    if 84 <= x < 102 and 22 <= y <= 58:
        return "on the edge of the area"
    if x >= 80 and (y < 20 or y > 60):
        return f"wide on the {side}"
    return None


class Booth:
    def __init__(self, m: engine.Match, frames: dict, params: dict | None = None, writer: Writer | None = None):
        self.m, self.g, self.st = m, game.Game(m, frames), engine.State(m)
        self.colour, self.writer = Colour(m, self.st), writer
        self.p = params or load_params()
        self.lines: list[dict] = []
        self.cur = None
        self.free_at = self.last_name = self.last_ball = self.clock = self.last_word = self.last_free = -1e9
        self.burst = self.bursts = 0  # colour comes in runs of one or two sentences, then a breath, the way people talk
        self.phase, self.prev_phase, self.minute, self.owed, self.last_pair = "dead ball", None, 0, 0.0, None

    # ---- the microphone
    def say(self, t: float, text: str, kind: str, prio: int, voice: str = "pbp") -> bool:
        cut = False
        if self.cur and t < self.cur["end"]:
            cuts = prio >= 8 or prio >= self.cur["prio"] + 3 or (self.cur["kind"] == "fill" and prio >= 3)
            if not cuts:
                return False
            self.cur["words"] = self.cur["words"][:int(len(self.cur["words"]) * max(0.0, t - self.cur["t"]) / self.cur["dur"])]
            self.cur["end"] = t
            if not self.cur["words"]:
                self.lines.remove(self.cur)
            cut = True
        words = text.split()
        self.cur = {"t": t, "text": text, "kind": kind, "prio": prio, "voice": voice, "phase": self.now_phase(t),
                    "dur": speak_time(len(words)), "end": t + speak_time(len(words)), "words": words, "n": len(words),
                    "minute": self.minute, "cuts": cut or prio >= 8}
        self.lines.append(self.cur)
        self.free_at = self.last_word = self.cur["end"]
        return True

    def now_phase(self, t: float) -> str:
        return "dead ball" if t - self.last_ball > 4.0 else self.phase

    def named(self, t: float, text: str, kind: str, prio: int) -> None:
        """A call that names a player: rationed per phase, because that is what the human rations."""
        if t - self.last_name >= self.p[self.now_phase(t)]["name_gap"] and self.say(t, text, kind, prio):
            self.last_name = t

    def fill_until(self, t: float) -> None:
        """Advance the clock to t, letting colour take the microphone whenever it has been open too long."""
        if self.clock < 0:  # first event: nothing to fill before kick-off
            self.clock = self.free_at = self.last_word = t
        tau = self.clock
        while tau < t:
            tau = min(t, tau + 0.5)
            ph = self.now_phase(tau)
            flowing = (self.burst > 0 or self.colour.flowing()) and ph in ("dead ball", "set piece")
            if ph in FILL_OK and tau - self.free_at >= (0.4 if flowing else self.p[ph]["fill_after"]):
                in_play = ph in ("build-up", "progression")  # here names belong to the ball; colour stays general
                self.owed += 0.0 if in_play else self.p[ph].get("named_colour", 0.3)  # share of colour lines about a player
                want = self.owed >= 1.0
                item = self.colour.next(tau, self.minute, want, strict=in_play)
                if item:
                    text, variant = item
                    self.owed -= want
                    if self.writer and text not in self.colour.verbatim:  # a model re-voices the draft; three checks keep it to the draft
                        text = self.writer.write(text, f"{self.minute} minutes played, {self.st.scoreline()}.", variant)
                    self.say(tau, text, "fill", 1, "analyst")
                    if flowing:
                        self.burst = max(0, self.burst - 1)
                    else:
                        self.bursts += 1
                        self.burst = RUNS[self.bursts % len(RUNS)]
                elif tau - self.last_word >= 8.0 and tau - self.colour.last_score >= 180.0 and self.minute:
                    self.colour.last_score = tau  # never leave the microphone open for long: come back to the score, but not twice a minute
                    self.say(tau, f"{self.st.scoreline()}, {self.minute} minutes played.", "fill", 1, "analyst")
                else:
                    self.free_at = tau  # nothing new to say yet: look again after another pause
        self.clock = t

    # ---- one event in
    def feed(self, e: dict) -> None:
        t, typ = e["t"], e["type"]["name"]
        self.fill_until(t)
        self.minute = e["minute"]
        self.st.apply(e)
        fact = engine.describe(self.m, self.st, e)
        s = self.g.step(e)
        self.colour.observe(e, s, self.prev_phase)
        if s:
            self.phase, self.last_ball = s["phase"], t
        ph = self.now_phase(t)
        rich = self.p[ph]["rich"]
        who = short(self.m.name(e.get("player")))
        if fact and fact["kind"] in EVENT_KINDS:  # laws-of-the-game events: always voiced, big ones cut in
            if fact["kind"] not in ("goal", "pen_goal", "substitution"):  # a commentator says "Messi", and keeps the full name for the goal
                fact = {**fact, "facts": {k: short(v) if k in ("player", "assist", "recipient", "keeper") and isinstance(v, str) else v
                                          for k, v in fact["facts"].items()}}
            self.say(t, engine.render(fact), "event", max(3, fact["sal"]))
        elif s and s["on_ball"]:
            pic, p, to = s["picture"], s["pass"], short(s["recipient"])
            fm = pic.get("free_man")
            where = fm and ("the far side" if fm["far_side"] else f"the {fm['side']}")
            if s["phase"] == "counter-attack" and self.prev_phase != "counter-attack":
                self.say(t, f"{s['team']} break! {who} leads it" + (f", space on {where}!" if fm else "."), "break", 6)
                self.last_name = t
            elif typ == "Pass" and "outcome" not in p and to:
                end = p["end_location"]
                if s["restart"]:
                    if s["restart"] != "corner" and (s["restart"] != "goal_kick" or rich):
                        label = {"free_kick": "Free kick", "throw_in": "Throw", "goal_kick": "Goal kick", "kick_off": "Kick-off"}[s["restart"]]
                        self.named(t, f"{label}, {who} to {to}." if rich else f"{label}, {who}.", "restart", 3)
                elif fm and end[0] >= 72 and t - self.last_free >= 15 and min(pic["mates"], key=lambda q: game.dist(q, end)) == fm["pos"]:
                    self.last_free = t  # measured: this signal alone does not predict a shot, so it is called only near goal, not shouted
                    if fm["kind"] == "range":
                        self.say(t, f"{to} free on {where}!", "free_man", 6)  # the point comes first: the shot may be two seconds away
                        self.last_name = t
                    else:
                        self.named(t, f"{to}, in space on {where}.", "free_man", 3)
                elif p.get("cross"):
                    self.named(t, f"{who} crosses for {to}!", "cross", 4)
                elif p.get("through_ball") or s["lines_broken"] >= 3:
                    self.named(t, f"{who} threads it through to {to}!", "through", 4)
                elif abs(end[1] - s["y"]) >= 35:
                    self.named(t, f"{who} switches it out to {to}.", "switch", 3)
                elif rich:
                    pair = frozenset((who, to))
                    dx, dy = end[0] - s["x"], end[1] - s["y"]
                    how = ("forward to" if dx >= 15 else "back to" if dx <= -10
                           else ("wide to" if abs(end[1] - 40) > abs(s["y"] - 40) else "inside to") if abs(dy) >= 15 else "to")
                    self.named(t, f"{to}." if pair == self.last_pair else f"{who}, {how} {to}.", "pass", 2)
                    self.last_pair = pair
            elif typ == "Pass" and p.get("outcome", {}).get("name") == "Out":
                self.named(t, f"{who} puts it out of play.", "out", 3)
            elif typ == "Ball Receipt*" and "outcome" not in e.get("ball_receipt", {}):
                z = zone(e["location"]) if rich and e.get("location") else None
                self.named(t, f"{who}, {z}." if z else f"{who}.", "name", 2)  # the basic unit of play-by-play: the name, on the receive
            elif typ == "Carry" and game.dist(e["location"], e["carry"]["end_location"]) >= 12:
                self.named(t, f"{who} drives forward." if rich else f"{who} carries it on.", "carry", 2)
            elif typ == "Dribble" and e["dribble"]["outcome"]["name"] == "Complete":
                past = next((short(self.m.name(self.m.by_id[r].get("player"))) for r in e.get("related_events", [])
                             if r in self.m.by_id and self.m.by_id[r]["type"]["name"] == "Dribbled Past"), None)
                self.named(t, f"{who} goes past {past}!" if past else f"{who} beats the defender!", "dribble", 3)
            elif typ == "Pass" and p.get("outcome", {}).get("name") == "Incomplete" and s["x"] >= 60:
                self.named(t, f"{who}'s pass is cut out.", "lost", 2)
            elif typ in ("Dispossessed", "Miscontrol") and who:
                self.named(t, f"{who} is robbed of it." if typ == "Dispossessed" else f"{who} cannot hold it.", "lost", 2)
        elif s and typ == "Duel" and e.get("duel", {}).get("type", {}).get("name") == "Tackle" and who \
                and e["duel"].get("outcome", {}).get("name", "").startswith(("Won", "Success")):
            self.named(t, f"{who} wins the tackle.", "defend", 2)
        elif s and typ in ("Interception", "Ball Recovery", "Clearance", "Block") and who:
            verb = {"Interception": "cuts it out", "Ball Recovery": "wins it back", "Clearance": "clears", "Block": "gets a block in"}[typ]
            self.named(t, f"{who} {verb}.", "defend", 2)
        if s:
            self.prev_phase = s["phase"]


def load_params() -> dict:
    if PARAMS_FILE.exists():
        return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    return {p: dict(v) for p, v in START.items()}


def run(m: engine.Match, frames: dict, params: dict, writer: Writer | None = None) -> tuple[list[dict], list[tuple]]:
    """Whole match (not the shootout) through the booth. Returns the lines and the phase segments (start, end, phase)."""
    b = Booth(m, frames, params, writer)
    states = []
    for e in m.events:
        if e["period"] > 4:
            break
        b.feed(e)
        if e.get("location") and e["type"]["name"] not in ("Pressure",):
            states.append((e["t"], e["period"], b.phase))
    segs = []
    for (t0, p0, ph), (t1, p1, _) in zip(states, states[1:]):
        gap = t1 - t0
        if p0 != p1 or gap < 0:
            continue
        segs.append((t0, t0 + min(gap, 4.0), ph))
        if gap > 4.0:
            segs.append((t0 + 4.0, t1, "dead ball"))
    return b.lines, segs


# ---------------------------------------------------------------- measure against the human, tune, report

CAPTIONS = engine.DATA / "captions_RgqKdplLIk4.json"  # the broadcast's auto-captions: local only, never redistributed
TIMING = engine.DATA / "human_timing.json"             # derived, in the repo: [match second, was it a player's name] per word
BASELINE = engine.DATA / "human_baseline.json"         # derived, in the repo: what the human's dead-ball talk is made of


def human_words(m: engine.Match) -> list[tuple[float, str]]:
    """Caption words of the real broadcast, on the same match clock as the events.
    Without the transcript, the published timing stands in: same clock, same name flags, no words."""
    if not CAPTIONS.exists():
        a_name = min(names_set(m))
        return [(t, a_name if named else "_") for t, named in json.loads(TIMING.read_text(encoding="utf-8"))]
    video = json.loads((engine.ROOT / "video.json").read_text(encoding="utf-8"))
    kick = {int(p): v for p, v in video["kickoff"].items()}
    out = []
    for ev in json.loads(CAPTIONS.read_text(encoding="utf-8"))["events"]:
        for seg in ev.get("segs", []) or []:
            vt = (ev["tStartMs"] + seg.get("tOffsetMs", 0)) / 1000
            p = max((q for q in kick if q <= 4 and vt >= kick[q]), default=None)
            if p and vt - kick[p] <= m.period_len[p]:
                out += [(m.period_start[p] + vt - kick[p], w) for w in re.findall(r"[a-z']+", engine.fold(seg.get("utf8", "")))]
    return sorted(out)


def booth_words(lines: list[dict]) -> list[tuple[float, str]]:
    return sorted((l["t"] + i * l["dur"] / l["n"], w) for l in lines for i, w in enumerate(re.findall(r"[a-z']+", engine.fold(" ".join(l["words"])))))


def profile(words: list[tuple[float, str]], segs: list[tuple], names: set[str]) -> dict:
    """Words/min and names/min per phase, plus how the silences are distributed."""
    starts = [s[0] for s in segs]
    dur, spoken, named = defaultdict(float), defaultdict(int), defaultdict(int)
    for a, b, ph in segs:
        dur[ph] += b - a
    inside = []
    for t, w in words:
        i = bisect.bisect(starts, t) - 1
        if i >= 0 and segs[i][0] <= t < segs[i][1]:
            spoken[segs[i][2]] += 1
            named[segs[i][2]] += w in names
            inside.append(t)
    total = sum(dur.values())
    gaps = sorted(b - a for a, b in zip(inside, inside[1:]) if b - a < 300)
    return {"phase": {ph: {"wpm": 60 * spoken[ph] / dur[ph], "npm": 60 * named[ph] / dur[ph], "share": dur[ph] / total} for ph in PHASES if dur[ph]},
            "wpm": 60 * len(inside) / total, "npm": 60 * sum(named.values()) / total,
            "gaps_3": 60 * sum(g > 3 for g in gaps) / total, "gaps_10": 60 * sum(g > 10 for g in gaps) / total,
            "p99": gaps[int(len(gaps) * .99)], "longest": gaps[-1]}


def tune(m, frames, human, names, rounds: int = 14, log=print, writer: Writer | None = None, start: dict | None = None) -> dict:
    """The loop: run the match, compare each phase with the human, nudge that phase's two knobs, repeat until close."""
    params = {p: dict(v) for p, v in (start or START).items()}
    for r in range(rounds):
        lines, segs = run(m, frames, params, writer)
        mine, ref = profile(booth_words(lines), segs, names), profile(human, segs, names)
        worst = 0.0
        for ph in mine["phase"]:
            a, b = mine["phase"][ph], ref["phase"][ph]
            rn, rw = a["npm"] / max(b["npm"], .1), a["wpm"] / max(b["wpm"], 1)
            worst = max(worst, abs(rn - 1), abs(rw - 1))
            params[ph]["name_gap"] = round(min(40.0, max(0.5, params[ph]["name_gap"] * min(1.6, max(.6, rn)))), 2)  # too many names -> wait longer
            if ph in FILL_OK:
                params[ph]["fill_after"] = round(min(30.0, max(0.3, params[ph]["fill_after"] * min(1.6, max(.6, rw)))), 2)
                if ph in ("dead ball", "set piece"):  # here names come from colour, not from the ball
                    params[ph]["named_colour"] = round(min(1.0, max(0.0, params[ph]["named_colour"] / min(1.6, max(.6, rn)))), 2)
            elif rw < .8:
                params[ph]["rich"] = True  # near goal there is no colour: fuller wording is the only way to add words
        log(f"round {r + 1:>2}: words/min {mine['wpm']:.0f} (human {ref['wpm']:.0f}) | names/min {mine['npm']:.1f} (human {ref['npm']:.1f}) | "
            f"worst phase off by {100 * worst:.0f}% | silences over 10 s: {mine['gaps_10']:.2f}/min")
        if worst <= .15:
            break
    return params


def names_set(m: engine.Match) -> set[str]:
    return {w for n in m.names.values() for w in re.findall(r"[a-z]{4,}", engine.fold(n))[-1:]} | {"mcallister", "mbape", "embape"}


NUMBER = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|hundred)\b", re.I)


def texture(texts: list[str], names: set[str]) -> dict:
    """What the talk is made of: the 'statistics ticker' symptoms, measured the same way for the human and the booth."""
    n = max(len(texts), 1)
    opens = [" ".join(t.lower().split()[:2]) for t in texts]
    return {"lines": len(texts), "words": round(sum(len(t.split()) for t in texts) / n, 1),
            "number": round(100 * sum(bool(NUMBER.search(t)) for t in texts) / n),
            "name": round(100 * sum(any(w in names for w in re.findall(r"[a-z]+", engine.fold(t))) for t in texts) / n),
            "openings": round(100 * len(set(opens)) / n)}


def human_texture(m: engine.Match, segs: list[tuple], names: set[str]) -> dict:
    return texture(human_dead_ball(m, segs), names) if CAPTIONS.exists() else json.loads(BASELINE.read_text(encoding="utf-8"))["texture"]


def human_dead_ball(m: engine.Match, segs: list[tuple]) -> list[str]:
    """The human's utterances that start while the ball is dead (caption words grouped by pauses over one second)."""
    video = json.loads((engine.ROOT / "video.json").read_text(encoding="utf-8"))
    kick = {int(p): v for p, v in video["kickoff"].items()}
    raw = []
    for ev in json.loads(CAPTIONS.read_text(encoding="utf-8"))["events"]:
        for seg in ev.get("segs", []) or []:
            txt = seg.get("utf8", "").strip()
            vt = (ev["tStartMs"] + seg.get("tOffsetMs", 0)) / 1000
            p = max((q for q in kick if q <= 4 and vt >= kick[q]), default=None)
            if txt and not txt.startswith("[") and p and vt - kick[p] <= m.period_len[p]:
                raw.append((m.period_start[p] + vt - kick[p], txt))
    raw.sort()
    utts, cur = [], raw[:1]
    for a, b in zip(raw, raw[1:]):
        if b[0] - a[0] > 1.0 or ">>" in b[1]:
            utts.append(cur)
            cur = []
        cur.append(b)
    starts = [x[0] for x in segs]
    out = []
    for u in utts + [cur]:
        i = bisect.bisect(starts, u[0][0]) - 1
        text = " ".join(x[1] for x in u).replace(">>", "").strip()
        if i >= 0 and segs[i][0] <= u[0][0] < segs[i][1] and segs[i][2] == "dead ball" and len(text.split()) >= 3:
            out.append(text)
    return out


if __name__ == "__main__":
    m, frames = game.load()
    human, names = human_words(m), names_set(m)
    if "--export-human" in sys.argv:  # refresh the two derived files from the local transcript
        TIMING.write_text(json.dumps([[round(t, 2), int(w in names)] for t, w in human]), encoding="utf-8")
        BASELINE.write_text(json.dumps({"texture": texture(human_dead_ball(m, run(m, frames, load_params())[1]), names)}), encoding="utf-8")
        sys.exit(f"wrote {TIMING.name} ({len(human)} words) and {BASELINE.name}")
    writer = Writer(m, live="--write" in sys.argv)
    if "--write" in sys.argv:
        if not writer.url:
            sys.exit("--write needs the local model: run start_llm.cmd first")
        params = tune(m, frames, human, names, rounds=3, writer=writer, start=load_params())  # the model writes; re-tune on what it says
        writer.save()
        print("writer:", dict(writer.stats))
    else:
        params = tune(m, frames, human, names, writer=writer if writer.cache else None)
    PARAMS_FILE.write_text(json.dumps(params, indent=1), encoding="utf-8")
    lines, segs = run(m, frames, params, writer if (writer.cache or writer.url) else None)
    if writer.url:
        writer.save()
    mine, ref = profile(booth_words(lines), segs, names), profile(human, segs, names)
    print(f'\n{"phase":<20}{"time":>6}{"words/min":>22}{"names/min":>22}   knobs')
    print(f'{"":<20}{"":>6}{"booth":>11}{"human":>11}{"booth":>11}{"human":>11}')
    for ph in PHASES:
        a, b, k = mine["phase"][ph], ref["phase"][ph], params[ph]
        print(f'{ph:<20}{100 * a["share"]:>5.0f}%{a["wpm"]:>11.0f}{b["wpm"]:>11.0f}{a["npm"]:>11.1f}{b["npm"]:>11.1f}   '
              f'name every {k["name_gap"]}s' + (f', colour after {k["fill_after"]}s' if ph in FILL_OK else (", rich" if k["rich"] else "")))
    print(f'{"whole match":<20}{"":>6}{mine["wpm"]:>11.0f}{ref["wpm"]:>11.0f}{mine["npm"]:>11.1f}{ref["npm"]:>11.1f}')
    print(f'\nsilence            booth    human\n  pauses > 3 s/min {mine["gaps_3"]:>6.1f}{ref["gaps_3"]:>9.1f}\n  pauses > 10 s/min{mine["gaps_10"]:>6.2f}{ref["gaps_10"]:>9.2f}'
          f'\n  99th percentile  {mine["p99"]:>5.1f}s{ref["p99"]:>8.1f}s\n  longest          {mine["longest"]:>5.1f}s{ref["longest"]:>8.1f}s')
    tb, th = texture([l["text"] for l in lines if l["kind"] == "fill"], names), human_texture(m, segs, names)
    print("\nwhat the colour talk is made of          booth    human")
    for key, label in (("number", "lines containing a number %"), ("name", "lines naming a player %"), ("openings", "distinct openings %"), ("words", "words per line")):
        print(f"  {label:<36}{tb[key]:>8}{th[key]:>9}")
    if "--show" in sys.argv:
        goal = next(e for e in m.events if e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal" and e["minute"] == 35)
        print("\n34:20 to 36:40:")
        for l in lines:
            if goal["t"] - 62 <= l["t"] <= goal["t"] + 78:
                mm, ss = divmod(int(l["t"] - m.period_start[1]), 60)
                print(f'  {mm:02d}:{ss:02d} [{l["phase"][:10]:<10}] {"  (colour) " if l["kind"] == "fill" else ""}{" ".join(l["words"])}{" --" if len(l["words"]) < l["n"] else ""}')
