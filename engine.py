"""Live football commentary engine.

Pipeline:  event feed -> State (reducer) -> describe (salience + facts) -> Director (airtime budget)
           -> Commentator (LLM with grounding check, template fallback).

Everything here is pure and clock-free except Commentator.line(), so the same code runs live
(server.py sleeps until each event is due) and offline in virtual time (eval.py).
Stdlib only. Data: StatsBomb Open Data (free, attribution required).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
COMP, SEASON, MATCH_ID = 43, 106, 3869685  # FIFA World Cup 2022 final, Argentina v France

NUM = r"\d+(?:\.\d+)?"


def fold(s: str) -> str:
    """Lower-case and strip accents so 'Mbappé' matches 'mbappe'."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _fetch(rel: str, name: str):
    path = DATA / name
    if not path.exists():  # first run only; afterwards the demo works offline
        DATA.mkdir(exist_ok=True)
        urllib.request.urlretrieve(f"{SRC}/{rel}", path)
    return json.loads(path.read_text(encoding="utf-8"))


def _secs(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


class Match:
    """Source adapter. In production this is the only class that changes (live feed instead of file)."""

    def __init__(self, match_id: int = MATCH_ID):
        meta = next(m for m in _fetch(f"matches/{COMP}/{SEASON}.json", f"matches_{COMP}_{SEASON}.json")
                    if m["match_id"] == match_id)
        self.home = meta["home_team"]["home_team_name"]
        self.away = meta["away_team"]["away_team_name"]
        self.truth = (meta["home_score"], meta["away_score"])  # ground truth for eval
        self.title = f'{meta["competition"]["competition_name"]} {meta["season"]["season_name"]} {meta["competition_stage"]["name"]}'
        lineups = _fetch(f"lineups/{match_id}.json", f"lineups_{match_id}.json")
        self.names = {p["player_id"]: p["player_nickname"] or p["player_name"] for t in lineups for p in t["lineup"]}
        self.events = _fetch(f"events/{match_id}.json", f"events_{match_id}.json")
        self.by_id = {e["id"]: e for e in self.events}
        # Period clocks restart at 0, so build one continuous match time `t` (seconds).
        ends: dict[int, float] = defaultdict(float)
        for e in self.events:
            ends[e["period"]] = max(ends[e["period"]], _secs(e["timestamp"]))
        offset, acc = {}, 0.0
        for p in sorted(ends):
            offset[p], acc = acc, acc + ends[p]
        for e in self.events:
            e["t"] = offset[e["period"]] + _secs(e["timestamp"])
        self.duration = acc
        self.period_start, self.period_len = offset, dict(ends)  # used to sync with broadcast video
        # Every name token of this match: the grounding check uses it to spot a wrong player/team.
        self.vocab = {w for n in [*self.names.values(), self.home, self.away] for w in re.findall(r"[a-z]{4,}", fold(n))}

    def name(self, player: dict | None) -> str | None:
        return self.names.get(player["id"], player["name"]) if player else None


class State:
    """Match state reducer: score, clock, ball, rolling team stats, per-minute pressure."""

    def __init__(self, m: Match):
        self.m = m
        self.score, self.shootout = [0, 0], [0, 0]
        self.period, self.clock, self.ball, self.side_on_ball = 1, "00:00", None, 0
        self.stats = {t: defaultdict(float) for t in (m.home, m.away)}
        self.mom: list[list[float]] = []  # [home, away] pressure per match minute

    def side(self, team: str) -> int:
        return 0 if team == self.m.home else 1

    def scoreline(self) -> str:
        return f"{self.m.home} {self.score[0]}-{self.score[1]} {self.m.away}"

    def apply(self, e: dict) -> None:
        typ, team = e["type"]["name"], e["team"]["name"]
        s, i = self.stats[team], self.side(team)
        self.period, self.clock = e["period"], f'{e["minute"]:02d}:{e["second"]:02d}'
        loc = e.get("location")
        if loc:  # StatsBomb coordinates are always "own team attacks right"; flip the away side for display
            self.ball = [loc[0], loc[1]] if i == 0 else [120 - loc[0], 80 - loc[1]]
            self.side_on_ball = i
        b = int(e["t"] // 60)
        while len(self.mom) <= b:
            self.mom.append([0.0, 0.0])
        if e["period"] == 5:  # shootout goals do not count towards the score or stats
            if typ == "Shot" and e["shot"]["outcome"]["name"] == "Goal":
                self.shootout[i] += 1
            return
        if typ in ("Pass", "Carry") and e["possession_team"]["name"] == team:
            s["poss"] += e.get("duration") or 0
        if loc and loc[0] >= 80 and typ in ("Pass", "Carry", "Dribble"):
            self.mom[b][i] += 0.2  # final-third action
        if typ == "Pass":
            s["passes"] += 1
            s["passes_ok"] += "outcome" not in e["pass"]
            s["corners"] += e["pass"].get("type", {}).get("name") == "Corner"
        elif typ == "Shot":
            sh, out = e["shot"], e["shot"]["outcome"]["name"]
            s["shots"] += 1
            s["xg"] += sh["statsbomb_xg"]
            s["on_target"] += out in ("Goal", "Saved", "Saved to Post")
            self.mom[b][i] += 1 + 10 * sh["statsbomb_xg"]
            if out == "Goal":
                self.score[i] += 1
        elif typ == "Own Goal Against":
            self.score[1 - i] += 1
        elif typ == "Foul Committed":
            s["fouls"] += 1
        card = (e.get("foul_committed") or e.get("bad_behaviour") or {}).get("card", {}).get("name", "")
        if card:
            s["yellow" if card == "Yellow Card" else "red"] += 1

    def snapshot(self) -> dict:
        h, a = self.stats[self.m.home], self.stats[self.m.away]
        hp = round(100 * h["poss"] / ((h["poss"] + a["poss"]) or 1))

        def row(s, poss):
            return {"possession_pct": poss, "shots": int(s["shots"]), "on_target": int(s["on_target"]),
                    "xg": round(s["xg"], 2), "pass_pct": round(100 * s["passes_ok"] / (s["passes"] or 1)),
                    "corners": int(s["corners"]), "fouls": int(s["fouls"]),
                    "yellow": int(s["yellow"]), "red": int(s["red"])}
        return {self.m.home: row(h, hp), self.m.away: row(a, 100 - hp)}


# ---------------------------------------------------------------- salience: what is worth saying?

PHASE = {1: "the first half", 2: "the second half", 3: "extra time", 4: "the second half of extra time",
         5: "the penalty shootout"}
BREAK = {1: "half-time", 2: "the end of normal time", 3: "half-time in extra time", 4: "the end of extra time"}


SCORE_KINDS = {"goal", "pen_goal", "pen_miss", "restart", "break"}


def describe(m: Match, st: State, e: dict) -> dict | None:
    """Map one raw event to {kind, sal (0-10), facts}. `facts` is the ONLY thing the LLM sees."""
    typ, team = e["type"]["name"], e["team"]["name"]
    x = (e.get("location") or [0, 0])[0]
    f = {"minute": e["minute"] + 1, "team": team, "player": m.name(e.get("player")), "score": st.scoreline()}
    opponent = m.away if team == m.home else m.home
    kind, sal = None, 0

    if typ in ("Half Start", "Half End"):
        if team != m.home:  # the feed carries one copy per team
            return None
        if typ == "Half Start":
            kind, sal = ("kick_off" if e["period"] == 1 else "restart"), 8
            f.update(home=m.home, away=m.away, phase=PHASE[e["period"]])
        elif e["period"] == 5:
            win = m.home if st.shootout[0] > st.shootout[1] else m.away
            kind, sal = "shootout_over", 10
            f.update(winner=win, shootout=f"{max(st.shootout)}-{min(st.shootout)}")
        else:
            kind, sal = "break", 9
            f.update(phase=BREAK[e["period"]])
    elif typ == "Shot":
        sh, out = e["shot"], e["shot"]["outcome"]["name"]
        pen = sh["type"]["name"] == "Penalty"
        f["xg"] = round(sh["statsbomb_xg"], 2)
        f["outcome"] = {"Off T": "off target", "Wayward": "off target", "Saved to Post": "saved"}.get(out, out.lower())
        keeper = next((m.by_id[r] for r in e.get("related_events", [])
                       if r in m.by_id and m.by_id[r]["type"]["name"] == "Goal Keeper"), None)
        if keeper and out != "Goal":
            f["keeper"] = m.name(keeper.get("player"))
        if e["period"] == 5:
            f["shootout"] = f"{m.home} {st.shootout[0]}-{st.shootout[1]} {m.away}"
            kind, sal = ("so_goal" if out == "Goal" else "so_miss"), 10
        elif out == "Goal":
            assist = m.by_id.get(sh.get("key_pass_id", ""))
            if assist:
                f["assist"] = m.name(assist.get("player"))
            kind, sal = ("pen_goal" if pen else "goal"), 10
        elif pen:
            kind, sal = "pen_miss", 9
        else:
            kind, sal = {"Saved": ("shot_saved", 7), "Saved to Post": ("shot_saved", 7), "Post": ("shot_post", 8),
                         "Blocked": ("shot_blocked", 5)}.get(out, ("shot_off", 5))
    elif typ in ("Foul Committed", "Bad Behaviour"):
        fc = e.get("foul_committed") or e.get("bad_behaviour") or {}
        card = fc.get("card", {}).get("name", "")
        f["opponent"] = opponent
        if card:
            f["card"] = card
            kind, sal = ("yellow_card", 6) if card == "Yellow Card" else ("red_card", 9)
        elif fc.get("penalty"):
            kind, sal = "penalty_awarded", 9
        elif typ == "Foul Committed":
            kind, sal = ("handball" if fc.get("type", {}).get("name") == "Handball" else "foul"), 3
    elif typ == "Substitution":
        f["replacement"] = m.name(e["substitution"]["replacement"])
        kind, sal = "substitution", 5
    elif typ == "Offside":
        kind, sal = "offside", 4
    elif typ == "Pass":
        p = e["pass"]
        f["recipient"] = m.name(p.get("recipient"))
        end = p.get("end_location", [0, 0])
        if p.get("outcome", {}).get("name") == "Pass Offside":
            f["player"], kind, sal = f["recipient"] or f["player"], "offside", 4
        elif p.get("type", {}).get("name") == "Corner":
            kind, sal = "corner", 4
        elif p.get("through_ball") and f["recipient"]:
            kind, sal = "through_ball", 4
        elif p.get("cross"):
            kind, sal = "cross", 4
        elif p.get("shot_assist") and f["recipient"]:
            kind, sal = "key_pass", 3
        elif "outcome" not in p and f["recipient"]:
            if end[0] >= 102 and 18 <= end[1] <= 62:
                kind, sal = "box_entry", 3
            else:
                kind, sal = ("buildup_final", 2) if x >= 80 else ("buildup", 1)
    elif typ == "Dribble" and e["dribble"]["outcome"]["name"] == "Complete" and x >= 60:
        kind, sal = ("nutmeg", 5) if e["dribble"].get("nutmeg") else ("dribble", 3)
    elif typ in ("Interception", "Ball Recovery") and x >= 70 and not e.get("ball_recovery", {}).get("recovery_failure"):
        kind, sal = "high_turnover", 3
    elif typ == "Clearance" and x <= 20:
        kind, sal = "clearance", 2
    elif typ == "Injury Stoppage":
        kind, sal = "stoppage", 2

    if not kind:
        return None
    if kind not in SCORE_KINDS:
        del f["score"]
    return {"kind": kind, "sal": sal, "voice": "pbp", "facts": {"event": kind, **{k: v for k, v in f.items() if v is not None}}}


def analysis(m: Match, st: State, e: dict, trigger: str) -> dict:
    """Analyst job: a stats snapshot the co-commentator may talk about."""
    last = st.mom[-10:]
    facts = {"event": "analysis", "trigger": trigger, "minute": e["minute"] + 1, "score": st.scoreline(),
             "stats": st.snapshot(),
             "pressure_last_10_min": {m.home: round(sum(r[0] for r in last), 1), m.away: round(sum(r[1] for r in last), 1)}}
    return {"kind": "analysis", "sal": 4, "voice": "analyst", "facts": facts}


# ---------------------------------------------------------------- director: airtime budget

class Director:
    """Decides WHEN to speak. All times are wall-clock seconds, so the budget holds at any replay speed.

    - sal >= PRIORITY speaks now and pre-empts whatever is queued (goal, card, big save).
    - Otherwise the most salient event since the last line waits for the next free slot (GAP).
    - A queued line older than STALE is dropped: late commentary is worse than none.
    - The analyst gets a slot every ANALYST_EVERY match seconds and after goals/breaks.
    """
    GAP, STALE, PRIORITY, ANALYST_EVERY = 4.0, 6.0, 7, 300.0

    def __init__(self, m: Match):
        self.m, self.last, self.pending, self.owed, self.next_analysis, self.dropped = m, -1e9, None, None, None, 0

    def _replace(self, new):
        self.dropped += self.pending is not None
        self.pending = new

    def offer(self, cand: dict | None, now: float, st: State, e: dict) -> list[dict]:
        out = []
        if self.next_analysis is None:
            self.next_analysis = e["t"] + self.ANALYST_EVERY
        if cand and cand["sal"] >= self.PRIORITY:
            out.append(cand)
            self._replace(None)
            self.last = now
            if cand["kind"] in ("goal", "pen_goal", "break"):
                self.owed = "after " + cand["kind"].replace("pen_", "")
        elif cand and (self.pending is None or cand["sal"] >= self.pending["sal"]):
            self._replace({**cand, "at": now})
        if e["t"] >= self.next_analysis and st.period < 5:
            self.owed, self.next_analysis = self.owed or "interval", e["t"] + self.ANALYST_EVERY
        if now - self.last >= self.GAP:
            if self.owed and st.period < 5 and (self.pending is None or self.pending["sal"] < 4):
                out.append(analysis(self.m, st, e, self.owed))
                self.owed = None
                self._replace(None)
                self.last = now + self.GAP  # analyst lines are longer: give them a double slot
            elif self.pending:
                if now - self.pending["at"] <= self.STALE:
                    out.append(self.pending)
                    self.last = now
                    self.pending = None
                else:
                    self._replace(None)
        return out


def play(m: Match, speed: float = 1.0, start_t: float = 0.0):
    """Yield (event, state, wall_time_due, jobs, director). No sleeping: the caller owns the clock."""
    st, d = State(m), Director(m)
    for e in m.events:
        st.apply(e)
        if e["t"] < start_t:  # fast-forward: build state silently
            continue
        now = max(0.0, e["t"] - start_t) / speed  # wall time is measured from start_t, not from the next event
        yield e, st, now, d.offer(describe(m, st, e), now, st, e), d


# ---------------------------------------------------------------- commentator: LLM + guardrail + fallback

T = {
    "kick_off": ["We're under way: {home} against {away}."],
    "restart": ["Back under way for {phase}. It's {score}."],
    "break": ["That's {phase}. {score}."],
    "shootout_over": ["It's all over! {winner} win the shootout {shootout}!"],
    "goal": ["GOAL! {player} scores for {team}, set up by {assist}! {score}.", "GOAL! {player} scores for {team}! {score}.",
             "{player}! It's in! {score}."],
    "pen_goal": ["GOAL! {player} converts the penalty. {score}.", "{player} from the spot, and scores! {score}."],
    "pen_miss": ["{player} from the spot, and it's {outcome}! It stays {score}."],
    "so_goal": ["{player} scores. Shootout: {shootout}.", "No mistake from {player}. Shootout: {shootout}."],
    "so_miss": ["{player} steps up, and it's {outcome}! Shootout: {shootout}."],
    "shot_saved": ["{player} shoots, and {keeper} saves!", "Good stop from {keeper} to deny {player}.", "{player} tests the goalkeeper."],
    "shot_post": ["{player} hits the woodwork! So close for {team}."],
    "shot_off": ["{player} lets fly, but it's off target.", "{player} has a go, and it's wide."],
    "shot_blocked": ["{player}'s shot is blocked.", "{player} shoots, but there's a body in the way."],
    "penalty_awarded": ["Penalty to {opponent}! {player} gives it away."],
    "red_card": ["Red card! {player} is sent off, and {team} are a player down."],
    "yellow_card": ["Yellow card for {player} of {team}.", "{player} goes into the book."],
    "foul": ["Free kick to {opponent} after a foul by {player}.", "{player} commits the foul. {opponent} free kick."],
    "handball": ["Handball by {player}. Free kick to {opponent}."],
    "substitution": ["Change for {team}: {replacement} on, {player} off."],
    "offside": ["The flag is up: {player} is offside."],
    "corner": ["Corner for {team}; {player} will take it.", "{player} over the corner for {team}."],
    "cross": ["{player} whips in a cross toward {recipient}.", "{player} swings it into the box."],
    "through_ball": ["{player} threads it through for {recipient}!"],
    "key_pass": ["{player} sets up {recipient} for the shot."],
    "box_entry": ["{player} finds {recipient} inside the box."],
    "dribble": ["{player} skips past the defender.", "Lovely feet from {player}."],
    "nutmeg": ["Nutmeg! {player} goes straight through the defender."],
    "high_turnover": ["{player} wins it back high up the pitch for {team}."],
    "clearance": ["{player} clears the danger for {team}."],
    "stoppage": ["A pause in play while a player gets treatment."],
    "buildup": ["{player} to {recipient}.", "{player} moves it on to {recipient}.", "{team} keep the ball; {player} to {recipient}."],
    "buildup_final": ["{team} probing; {player} finds {recipient}.", "{player} picks out {recipient} in the final third."],
}

# The LLM never writes from raw data. It restyles a verified draft (the template line), so its job is
# wording, not facts. Few-shot turns use names that are not in any match: a leak is caught by the guardrail.
SYSTEM = {
    "pbp": "You are a live TV football play-by-play commentator. Rewrite the DRAFT in a lively broadcast voice. "
           "Rules: one sentence, at most 18 words. Keep every name and number exactly as written. Say only what the "
           "draft says happened: never add a goal, shot, save, foul, score, place or opinion. Output the sentence only.",
    "analyst": "You are the co-commentator. Rewrite the DRAFT as one or two natural spoken sentences, at most 30 words. "
               "Keep every team name and number exactly as written, numbers as digits. Do not add other statistics, "
               "events or predictions. Output the sentences only.",
}
SHOTS = {
    "pbp": [("Draft: Dana Ruiz to Sam Okafor.", "Ruiz rolls it across to Sam Okafor."),
            ("Draft: Sam Okafor shoots, and Lee Park saves!", "Okafor lets fly, and Lee Park gets down well to save!"),
            ("Draft: GOAL! Dana Ruiz scores for Riverton! Riverton 1-0 Eastfield.",
             "GOAL! Dana Ruiz finds the net, and it is Riverton 1-0 Eastfield!")],
    "analyst": [("Draft: Riverton have had 61% of the ball. Shots: Riverton 5, Eastfield 1.",
                 "Riverton are running this: 61% of the ball, and 5 shots against 1 for Eastfield.")],
}


def render(job: dict) -> str:
    """Deterministic template line (variant chosen by hash, so evals are reproducible)."""
    f = job["facts"]
    if job["kind"] == "analysis":
        (h, hs), (a, as_) = f["stats"].items()
        pr = f["pressure_last_10_min"]
        lead = h if hs["possession_pct"] >= as_["possession_pct"] else a
        blank = [t for t, s in f["stats"].items() if s["shots"] == 0]
        insights = [
            f'{lead} have had {f["stats"][lead]["possession_pct"]}% of the ball.',
            f'{blank[0]} have yet to register a shot.' if len(blank) == 1 else f'Shots: {h} {hs["shots"]}, {a} {as_["shots"]}.',
            f'On expected goals it is {h} {hs["xg"]}, {a} {as_["xg"]}.',
            f'{h} are completing {hs["pass_pct"]}% of their passes, {a} {as_["pass_pct"]}%.',
            f'The last ten minutes belong to {h if pr[h] > pr[a] else a}.' if abs(pr[h] - pr[a]) >= 2
            else 'Nothing between the sides in the last ten minutes.',
        ]
        k = f["minute"] // 5  # rotate so consecutive analyst slots talk about different things
        return " ".join(insights[(k + j) % len(insights)] for j in range(2))
    opts = T[job["kind"]]
    start = 0 if job["kind"] == "goal" else int(hashlib.md5(json.dumps(f, sort_keys=True).encode()).hexdigest(), 16)
    for k in range(len(opts)):
        try:
            return opts[(start + k) % len(opts)].format_map(f)
        except KeyError:  # this variant needs a fact we do not have (e.g. no assist): try the next one
            continue
    return f'{f.get("team", "")}: {job["kind"].replace("_", " ")}.'


# Words that assert an event. A line may use one only if the facts or the draft assert it too.
CLAIMS = {
    "goal": r"\bgoals?\b|\bscor(e|es|ed|ing|er)\b|\bequali[sz]\w*|\bnets?\b", "penalty": r"\bpenalt\w*|\bspot\b",
    "shootout": r"\bshoot-?out", "save": r"\bsav(e|es|ed|ing)\b|\bdenie[sd]\b",
    "shot": r"\b(shot|shots|shoots?|shooting|strikes?|fires?)\b", "intercept": r"\bintercept\w*|\bsteals?\b",
    "foul": r"\bfoul\w*", "card": r"\b(card|booked|booking|sent off)\b", "offside": r"\boffside", "corner": r"\bcorner",
    "substitution": r"\bsubstitut\w*", "injury": r"\binjur\w*",
    "result": r"\b(win|wins|won|winner|victory|champions?|draw|draws|drawn|level|lead|leads|leading|ahead|behind|trail\w*)\b",
    "woodwork": r"\b(post|crossbar|woodwork)\b", "block": r"\bblock\w*", "miss": r"\bmiss(es|ed|ing)?\b",
    "clearance": r"\bclear(s|ed|ance)\b", "tackle": r"\btackl\w*", "header": r"\b(header|heads|headed|volley\w*)\b",
}


def grounded(text: str, facts: dict, vocab: set[str], draft: str = "") -> tuple[bool, str]:
    """Guardrail between the LLM and the air. Four cheap, deterministic checks:

    1. name     a player/team of this match that is not in the facts
    2. number   any number that is not in the facts
    3. proper   a capitalised word mid-sentence that is not in the facts (names from outside the match)
    4. claim    an event word (goal, save, foul, lead...) that neither the facts nor the draft assert

    ponytail: lexical, not semantic. It cannot judge tone or subtle wrong emphasis. Upgrade path: NLI/LLM-judge
    offline on sampled traces. Checks 3 and 4 were added after a real 1.5B model passed 1 and 2 with wrong verbs.
    """
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    allowed, t = fold(json.dumps(facts, ensure_ascii=False)).replace("_", " "), plain.lower()
    bad = sorted(w for w in set(re.findall(r"[a-z]{4,}", t)) if w in vocab and w not in allowed)
    if bad:
        return False, "name:" + bad[0]
    nums = {float(n) for n in re.findall(NUM, allowed)}
    bad = [n for n in re.findall(NUM, t) if float(n) not in nums]
    if bad:
        return False, "number:" + bad[0]
    bad = [w for w in re.findall(r"(?<![.!?] )(?<!^)\b[A-Z][A-Za-z]{2,}", plain) if w.lower() not in allowed]
    if bad:
        return False, "proper:" + bad[0]
    asserted = allowed + " " + fold(draft)
    bad = [k for k, rx in CLAIMS.items() if re.search(rx, t) and not re.search(rx, asserted)]
    if bad:
        return False, "claim:" + bad[0]
    return True, ""


def llm_config() -> tuple[str, str]:
    """(base_url, model). Explicit env wins; otherwise use the local model server if start_llm.cmd is running."""
    url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if url:
        return url, os.environ.get("LLM_MODEL", "")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/v1/models", timeout=0.3) as r:
            return "http://127.0.0.1:8081/v1", json.load(r)["data"][0]["id"]
    except (OSError, ValueError, KeyError, IndexError):
        return "", ""


class Commentator:
    """LLM is optional and never trusted: timeout, guardrail, template fallback.

    Any OpenAI-compatible endpoint works (LLM_BASE_URL, LLM_MODEL, optional LLM_API_KEY): the bundled local
    model server (free, auto-detected), a hosted free tier, or api.openai.com.
    """

    def __init__(self, m: Match):
        self.m = m
        self.url, self.model = llm_config()
        self.key = os.environ.get("LLM_API_KEY", "")
        self.timeout = float(os.environ.get("LLM_TIMEOUT", "2.5"))

    def _llm(self, job: dict, draft: str) -> str:
        shots = [{"role": r, "content": c} for u, a in SHOTS[job["voice"]] for r, c in (("user", u), ("assistant", a))]
        body = {"model": self.model, "max_tokens": 80, "temperature": 0.6, "messages": [
            {"role": "system", "content": SYSTEM[job["voice"]]}, *shots, {"role": "user", "content": "Draft: " + draft}]}
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        req = urllib.request.Request(f"{self.url}/chat/completions", json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip().strip('"')

    def line(self, job: dict) -> tuple[str, str, int]:
        """Return (text, source, latency_ms). source is 'template', 'llm' or 'fallback:<reason>'."""
        t0, draft, text, source = time.perf_counter(), render(job), None, "template"
        if self.url:
            try:
                text = self._llm(job, draft)
                ok, why = grounded(text, job["facts"], self.m.vocab, draft)
                source = "llm" if ok and text else f"fallback:{why or 'empty'}"
                text = text if source == "llm" else None
            except Exception as ex:  # timeout, HTTP error, bad JSON: the broadcast must not stop
                source = f"fallback:{type(ex).__name__}"
        return text or draft, source, round((time.perf_counter() - t0) * 1000)
