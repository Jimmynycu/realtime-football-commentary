"""The colour voice: what a co-commentator says when the ball is not asking for words.

Measured on the real broadcast, a human's dead-ball talk is not statistics: only 18% of the utterances contain a number.
It is a verdict on what just happened, the state of the contest, background on the people involved, and the stakes.
So this module produces, in that order of preference:

  verdict   a judgement on the last action, derived from the game model (was it a big chance? a tactical foul?)
  reading   how the match is going, said as a conclusion rather than a number ("France cannot get out")
  notes     background from the preparation sheet (prep_notes.json), about whoever was just involved
  stakes    what the score and the clock mean

Every sentence is a plain draft that is true by construction. `Writer` lets a language model re-voice the draft the way a
person would say it; the guardrail compares the result with the draft and falls back to the draft if anything was added.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import defaultdict, deque

import engine
import harness

NOTES_FILE = engine.ROOT / "prep_notes.json"
CACHE_FILE = engine.ROOT / "data" / "colour_cache.json"


ALIAS = {"Randal Kolo": "Kolo Muani"}


def short(name: str | None) -> str | None:
    if not name:
        return None
    parts = name.split()
    return ALIAS.get(name) or (" ".join(parts[1:]) if len(parts) > 1 else name)


def role(position: str) -> str:
    return "gk" if "Goalkeeper" in position else "def" if "Back" in position else "mid" if "Midfield" in position else "fwd"


WON = ("Won", "Success", "Success In Play", "Success Out")
# how a player's night is going, said when that player is involved and the count passes another step
FORM = (("wins", 4, "{who} keeps winning the ball back for {team}."),
        ("clear", 4, "{who} has been there to clear the danger again and again."),
        ("created", 2, "{who} is the one creating the chances for {team}."),
        ("crosses", 4, "{who} keeps putting crosses into the box."),
        ("lost", 4, "{who} has given the ball away a few times now."),
        ("carries", 6, "{who} keeps carrying the ball forward for {team}."),
        ("saves", 2, "{who} has been kept busy in that goal."))
# the replay: how the last passage ended, and what led up to it. Several ways of saying each, used in rotation.
REPLAY = {
    "out_to": ["{who} was looking for {to}, and overhit it.", "Too much on that from {who}; {to} had no chance of reaching it.",
               "{who} tried to pick out {to}, but it ran away from everybody."],
    "out": ["{who} put too much on that one.", "{who} will be annoyed with that; nobody near it.", "Just a little too ambitious from {who}."],
    "clear": ["{who} took no chances and put it out.", "Safety first from {who}.", "{who} was not going to take any risks there."],
    "dribble": ["{who} had done really well to get away from {to} before that.", "Lovely from {who} to go past {to} in the build-up."],
    "carry": ["{who} had carried it a long way before that.", "A strong run from {who} to get them up the pitch."],
    "switch": ["It was the big switch from {who} out to {to} that opened it up.", "{who} had changed the play, out to {to}."],
    "through": ["{who} had tried to slide {to} in behind.", "The idea from {who} was right, looking for the run of {to}."],
    "cross": ["{who} got the cross in, looking for {to}.", "The ball in from {who} was aimed at {to}."],
    "win": ["It all started with {who} winning the ball back.", "{who} did well to win that back in the first place.",
            "Good work from {who} to start that off."],
    "pair": ["{team} had worked it through {who} and {to}.", "{who} and {to} were both involved in that move.",
             "It had come through {who} and then {to}."],
}
THEME = {**dict.fromkeys(("noshot", "chances", "ball", "tilt", "hold", "siege", "mom", "xg", "control"), ("on top", 90.0)),
         **{f[0]: ("form:" + f[0], 300.0) for f in FORM}, "quiet": ("form:quiet", 300.0)}


def theme(key: str) -> tuple | None:
    parts = key.split(":")
    return THEME.get(parts[1] if parts[0] == "once" and len(parts) > 1 else parts[0])


class Colour:
    def __init__(self, m: engine.Match, st: engine.State):
        self.m, self.st = m, st
        prep = json.loads(NOTES_FILE.read_text(encoding="utf-8")) if NOTES_FILE.exists() else {}
        notes, self.coach = prep.get("notes", []), prep.get("coach", {})
        # Background is written by a person and is full of specifics; measured, rewording it is where the model does its damage
        # ("scored twice and twice" became "four goals in this tournament"). It goes out as written.
        self.verbatim = {n["text"] for n in notes}
        self.notes = defaultdict(list)
        for n in notes:
            self.notes[n["about"]].append(n["text"])
        self.pl = defaultdict(lambda: defaultdict(int))   # short name -> stat -> count
        self.info: dict[str, dict] = {}                   # short name -> {full, team, role}
        self.tm = {t: defaultdict(int) for t in (m.home, m.away)}
        self.per_min = defaultdict(lambda: defaultdict(int))
        self.roll = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self.last_shot_min: dict[str, int] = {}
        self.fresh: list[tuple] = []      # (time, named sentence or None, general sentence or None)
        self.recent: list[str] = []       # players involved lately, most recent last
        self.said: dict[str, float] = {}  # key -> when it was last said
        self.uses = defaultdict(int)      # key -> how many times it has been said
        self.free = None                  # (player, where) when the last pass went to an unmarked man
        self.turn = 0
        self.last_score = -1e9            # when the score was last read out (the booth's fallback shares this clock)
        self.goals: list[tuple] = []      # (minute, scorer, team)
        self.booked: set[str] = set()
        self.off: set[str] = set()
        self.log: deque = deque(maxlen=14)  # (t, team, kind, who, other): the recent passage, for the replay
        self.rot = defaultdict(int)        # gone off: can still be booked on the bench, but is no longer part of the play
        self.best: dict[str, tuple] = {}  # team -> (xg, who) of the best chance that did not go in
        self.poss = {"id": None, "team": None, "passes": 0, "chain": [], "t0": 0.0, "shot": False, "counter": False, "corner": False, "pressed": 0, "switches": 0, "gk": 0, "deep": 0, "last": 0.0}

    # ---------------------------------------------------------------- watching the match

    def _involve(self, who: str) -> None:
        if who in self.recent:
            self.recent.remove(who)
        self.recent.append(who)
        del self.recent[:-8]

    def _end_possession(self, t: float) -> None:
        p = self.poss
        names = list(dict.fromkeys(p["chain"]))
        if p["counter"] and p["team"]:
            self.tm[p["team"]]["counters"] += 1
        if p["corner"] and t - p["corner"] <= 15:
            self.fresh.append((t, None, "Nothing comes of the corner, dealt with at the first attempt.", 15))
        elif p["counter"] and not p["shot"]:
            self.fresh.append((t, None, "The break came to nothing in the end."))
        elif p["passes"] >= 10 and not p["shot"]:
            self.fresh.append((t, f"Patient from {p['team']}: {', '.join(names[-4:-1])} and {names[-1]} all involved." if len(names) >= 4 else None,
                               f"Patient stuff from {p['team']}, keeping it and waiting for the opening."))

    def _say_replay(self, t: float, kind: str, life: float = 25, **names) -> None:
        if all(names.get(k) for k in ("who",) + (("to",) if "{to}" in REPLAY[kind][0] else ())):
            self.rot[kind] += 1
            self.fresh.append((t, REPLAY[kind][self.rot[kind] % len(REPLAY[kind])].format(**names), None, life))

    def _buildup(self, t: float, team: str, skip: str | None = None) -> None:
        """One line on what led up to the incident: the most notable thing that side did in the twenty seconds before."""
        mine = [x for x in self.log if x[1] == team and t - x[0] <= 20 and x[0] < t]
        for kind in ("dribble", "carry", "switch", "through", "cross", "win"):
            hit = next((x for x in reversed(mine) if x[2] == kind and x[3] != skip), None)
            if hit:
                return self._say_replay(t, kind, who=hit[3], to=hit[4])
        passers = list(dict.fromkeys(x[3] for x in mine if x[2] == "pass" and x[3] != skip))
        if len(passers) >= 2:
            self._say_replay(t, "pair", who=passers[-2], to=passers[-1], team=team)

    def _replay_goal(self, t: float, who: str, maker: str | None, xg: float, team: str, mn: int) -> None:
        """A goal gets talked through like a replay: the finish, how it was built, the defending, what it means.
        Pushed in reverse because the newest item is said first."""
        other = self.m.away if team == self.m.home else self.m.home
        mine, theirs = self.st.score[self.st.side(team)], self.st.score[self.st.side(other)]
        names = list(dict.fromkeys(self.poss["chain"]))
        meaning = (f"All square again, and this final has a completely different feel." if mine == theirs
                   else f"{team} are back in front." if mine == theirs + 1 and theirs > 0
                   else f"The breakthrough, and {other} have to come out and play now." if (mine, theirs) == (1, 0)
                   else f"{team} are back in this, and suddenly it is a different game." if mine < theirs
                   else f"{team} have a real grip on this final now.")
        self.fresh.clear()  # whatever was waiting to be said about the play before the goal is old news now
        self.fresh.append((t, None, meaning, 90))
        self.goals.append((mn, who, team))
        self.said["theme:on top"] = t + 20  # the goal is its own statement of who is on top; do not pile readings onto it
        full = self.info.get(who, {}).get("full")
        for n in [n for n in self.notes.get(full, []) if "note:" + n not in self.said][:1]:  # the scorer's story belongs in the replay
            self.said["note:" + n] = t
            self.fresh.append((t, n, None, 90))
        if self.free and self.free[0] == who:
            self.fresh.append((t, f"Look at the space. Nobody picked up {who} at all, completely free on {self.free[1]}.",
                               "The marking was nowhere to be seen.", 80))
        if len(names) >= 4:
            self.fresh.append((t, f"The move started with {names[-4]}, and went through {names[-3]} and {names[-2]}.", "A lovely team goal, that.", 70))
        finish = (f"{who} was never going to miss from there." if xg >= .5 else f"{who} made that look easy, and it was anything but."
                  if xg < .15 else f"A clinical finish from {who}.")
        self.fresh.append((t, finish + (f" And what a ball from {maker}." if maker else ""), "That goal changes the whole feel of this final.", 60))

    def observe(self, e: dict, s: dict | None, prev_phase: str | None) -> None:
        typ, t, team, loc, mn = e["type"]["name"], e["t"], e["team"]["name"], e.get("location"), e["minute"]
        if s and typ == "Pass" and s["picture"].get("free_man") and s.get("recipient"):
            fm = s["picture"]["free_man"]
            self.free = (short(s["recipient"]), "the far side" if fm["far_side"] else f"the {fm['side']}")
        elif typ == "Pass":
            self.free = None
        tm = self.tm[team]
        if e["possession"] != self.poss["id"]:
            self._end_possession(t)
            self.poss = {"id": e["possession"], "team": e["possession_team"]["name"], "passes": 0, "chain": [], "t0": t,
                         "shot": False, "counter": False, "corner": False, "pressed": 0, "switches": 0, "gk": 0, "deep": 0, "last": self.poss["last"]}
        p = self.poss
        on_ball = team == p["team"]
        if on_ball and typ in ("Pass", "Carry", "Ball Receipt*"):
            p["last"] = t
            p["deep"] += bool(loc and loc[0] < 45)
        elif typ == "Pressure" and not on_ball:
            p["pressed"] += 1
        if s and s["phase"] == "counter-attack":
            p["counter"] = True
        full = self.m.name(e.get("player"))
        who = short(full)
        if who and who not in self.info and who not in self.off and "position" in e:
            self.info[who] = {"full": full, "team": team, "role": role(e["position"]["name"])}
        st = self.pl[who] if who else defaultdict(int)
        if e.get("out") and who and typ in ("Pass", "Clearance"):  # the ball has gone out: how, and what came before
            rec = short(self.m.name(e["pass"].get("recipient"))) if typ == "Pass" else None
            self._buildup(t, p["team"] or team, skip=who)
            self._say_replay(t, "clear" if typ == "Clearance" else "out_to" if rec else "out", who=who, to=rec)
        if typ in ("Dispossessed", "Miscontrol"):
            st["lost"] += 1
        elif typ == "Clearance":
            st["clear"] += 1
        elif typ == "Ball Recovery" and "recovery_failure" not in e.get("ball_recovery", {}):
            st["wins"] += 1
            self.log.append((t, team, "win", who, None))
        elif typ in ("Interception", "Duel") and e[typ.lower()].get("outcome", {}).get("name") in WON:
            st["wins"] += 1
            self.log.append((t, team, "win", who, None))
        elif typ == "Goal Keeper" and e["goalkeeper"]["type"]["name"] == "Shot Saved":
            st["saves"] += 1
        elif typ == "Carry" and loc and e["carry"]["end_location"][0] - loc[0] >= 10:
            st["carries"] += 1
            if e["carry"]["end_location"][0] - loc[0] >= 15:
                self.log.append((t, team, "carry", who, None))
        elif typ == "Half Start" and e["period"] in (2, 3, 4) and self._free(f"once:half:{e['period']}", t):
            self.said[f"once:half:{e['period']}"] = t
            d = self.st.score[0] - self.st.score[1]
            trail = self.m.away if d > 0 else self.m.home
            self.fresh.append((t, None, {2: f"{trail} have it all to do in this second half." if d else "All to play for in this second half.",
                                         3: "Thirty more minutes to settle the World Cup final.",
                                         4: "Fifteen minutes from a penalty shoot-out, unless somebody can find a goal."}[e["period"]], 60))

        if typ == "Pass":
            ok = "outcome" not in e["pass"]
            st["passes"] += 1
            st["passes_ok"] += ok
            if ok:
                self.per_min[mn][team] += 1
                p["passes"] += on_ball
            kind = e["pass"].get("type", {}).get("name")
            st["created"] += bool(e["pass"].get("shot_assist") or e["pass"].get("goal_assist"))
            if ok and who:
                rec = short(self.m.name(e["pass"].get("recipient")))
                self.log.append((t, team, "cross" if e["pass"].get("cross") else "through" if e["pass"].get("through_ball") else
                                 "switch" if abs(e["pass"]["end_location"][1] - loc[1]) >= 35 else "pass", who, rec))
            if ok and on_ball:
                p["switches"] += abs(e["pass"]["end_location"][1] - loc[1]) >= 35
                p["gk"] += "Goalkeeper" in e["pass"].get("recipient", {}).get("name", "") or e["pass"]["end_location"][0] < 12
            if e["pass"].get("cross"):
                st["crosses"] += 1
                tm["crosses"] += 1
            if kind == "Corner":
                p["corner"] = t
                tm["corners"] += 1
                if tm["corners"] >= 3:
                    self.fresh.append((t, None, f"Another corner for {team}; that is {tm['corners']} now.", 15))
            elif kind == "Goal Kick" and t - self.said.get("gk:" + team, -1e9) >= 600:  # how a side restarts says how it wants to play
                self.said["gk:" + team] = t
                long = e["pass"].get("length", 0) >= 40
                self.fresh.append((t, None, f"{team} go long from the goal kick; no interest in playing out." if long
                                   else f"{team} play it short from the back, happy to build from deep.", 25))
            elif kind == "Throw-in" and loc and loc[0] >= 95:
                self.fresh.append((t, None, f"A throw deep in the final third for {team}; a chance to load the box.", 20))
            if e["pass"].get("outcome", {}).get("name") == "Pass Offside":
                tm["offside"] += 1
                rec = short(self.m.name(e["pass"].get("recipient")))
                self.fresh.append((t, f"{rec} had gone just too early." if rec else None, "The flag is up; the run was just a fraction early."))
        elif typ == "Ball Receipt*" and "outcome" not in e.get("ball_receipt", {}) and who:
            st["touches"] += 1
            if on_ball:
                p["chain"].append(who)
            self._involve(who)
        elif typ == "Shot" and e["period"] < 5:
            sh, out, xg = e["shot"], e["shot"]["outcome"]["name"], e["shot"]["statsbomb_xg"]
            st["shots"] += 1
            if p["corner"] and t - p["corner"] <= 12:
                self.fresh.append((t, None, "A good delivery from the corner; it led to a chance.", 20))
            p["corner"] = False
            p["shot"] = True
            self.last_shot_min[team] = mn
            self.roll["shots"][mn][team] += 1
            tm["box_shots"] += loc[0] >= 102 and 18 <= loc[1] <= 62
            self._involve(who)
            maker = short(self.m.name(self.m.by_id.get(sh.get("key_pass_id", ""), {}).get("player")))
            if out != "Goal" and xg >= max(.2, self.best.get(team, (0,))[0]):
                self.best[team] = (xg, who)
            if out == "Goal":
                tm["goal_min"] = mn
                self._replay_goal(t, who, maker, xg, team, mn)
            if out != "Goal":
                self._buildup(t, team, skip=who)
                far = ((120 - loc[0]) ** 2 + (40 - loc[1]) ** 2) ** .5
                where = "from close range" if far <= 8 else "from inside the box" if far <= 18 else "from the edge of the area" if far <= 25 else "from distance"
                part = sh.get("body_part", {}).get("name", "")
                self.fresh.append((t, f"A header from {who}, {where}." if part == "Head" else
                                   f"{who} hit that with the {part.lower()}, {where}." if part.endswith("Foot") else f"{who} with the effort, {where}.", None, 30))
            if out != "Goal" and maker:
                self.fresh.append((t, f"{maker} created that for {who}.", None, 30))
            if out == "Goal":
                pass
            elif out.startswith("Saved"):
                if xg >= .3:
                    self.fresh.append((t, None, "What a stop that was. The goalkeeper stood up for as long as possible.", 60))
                self.fresh.append((t, f"{who} will feel that should have gone in." if xg >= .3 else f"{who} could not get enough on the shot.",
                                   "A big chance, and a big save." if xg >= .3 else "Comfortable enough for the goalkeeper."))
            elif out == "Blocked":
                self.fresh.append((t, f"{who} got the shot away, but there was a body in the way.", "Brave defending to get in the way of that."))
            elif out == "Post":
                self.fresh.append((t, f"So close for {who}.", "The woodwork comes to the rescue."))
            else:
                self.fresh.append((t, f"{who} had to hit the target from there." if xg >= .2 else f"Ambitious from {who}.",
                                   "That was a real opening wasted." if xg >= .2 else "That shot never really troubled the goalkeeper."))
        elif typ == "Foul Committed" and who:
            st["fouls"] += 1
            self.roll["fouls"][mn][team] += 1
            self._involve(who)
            fc = e.get("foul_committed", {})
            victim = next((short(self.m.name(self.m.by_id[r].get("player"))) for r in e.get("related_events", [])
                           if r in self.m.by_id and self.m.by_id[r]["type"]["name"] == "Foul Won"), None)
            if not on_ball:
                self._buildup(t, p["team"] or team)
            ax, ay = (120 - loc[0], 80 - loc[1]) if loc else (0, 40)  # where the free kick is, from the side that won it
            other = self.m.away if team == self.m.home else self.m.home
            if fc.get("penalty") or not loc:
                pass
            elif ax >= 84 and 22 <= ay <= 58:
                self.fresh.append((t, None, f"This is in shooting range for {other}.", 25))
            elif ax >= 78:
                self.fresh.append((t, None, f"{other} can put this one into the box.", 25))
            elif not on_ball and prev_phase in ("progression", "final-third attack"):
                self.fresh.append((t, None, f"That stopped {other} just as the attack was building.", 25))
            if fc.get("type", {}).get("name") == "Handball" and not fc.get("penalty"):
                pass  # the play-by-play has said it; nothing to judge
            elif fc.get("penalty"):
                other_gk = next((w for w, i in self.info.items() if i["team"] == team and i["role"] == "gk"), None)
                self.fresh.append((t, None, "The wait must feel endless for the man with the ball in his hands.", 80))
                self.fresh.append((t, f"{other_gk} is taking his time on that line, doing everything to put the taker off." if other_gk else None,
                                   "The goalkeeper is taking his time, doing everything to put the taker off.", 80))
                self.fresh.append((t, None, "A penalty in a World Cup final. It does not get any bigger than this.", 80))
                self.fresh.append((t, None, "A huge moment in this final.", 45))
                self.fresh.append((t, f"{victim} got the wrong side of {who}, and the challenge came in." if victim and fc.get("type", {}).get("name") != "Handball" else None,
                                   "No hesitation from the referee; straight to the spot.", 45))
            elif fc.get("card"):
                self.booked.add(who)
                self.fresh.append((t, f"{who} can have no complaints about the card.", "The referee had seen enough."))
            elif not on_ball and prev_phase == "counter-attack":
                self.fresh.append((t, f"{who} knew exactly what that was; the break was on.", "A foul to stop the counter-attack. Cynical, and effective."))
            elif loc and loc[0] < 35:
                self.fresh.append((t, f"{who} has given the free kick away in a dangerous place.", "That is a dangerous place to give a free kick away."))
            else:
                self.fresh.append((t, f"That was a foul by {who} on {victim}." if victim else None, "Just a coming together in the middle of the pitch."))
        elif typ == "Foul Won" and who:
            st["fouled"] += 1
        elif typ == "Dribble" and e["dribble"]["outcome"]["name"] == "Complete" and who:
            st["dribbles"] += 1
            past = next((short(self.m.name(self.m.by_id[r].get("player"))) for r in e.get("related_events", [])
                         if r in self.m.by_id and self.m.by_id[r]["type"]["name"] == "Dribbled Past"), None)
            if past:
                self.log.append((t, team, "dribble", who, past))
        elif typ in ("Dispossessed", "Miscontrol") and who and loc and loc[0] < 35:
            self.fresh.append((t, f"{who} was caught in possession in a dangerous area.", "Careless, and so close to their own goal."))
        elif typ == "Clearance":
            self.roll["clear"][mn][team] += 1
        elif typ == "Offside":
            tm["offside"] += 1
            self.fresh.append((t, None, "The flag is up; the run was just a fraction early."))
        elif typ == "Injury Stoppage":
            self.fresh.append((t, None, "A pause while a player gets treatment, and a chance for everyone to take on water.", 30))
        elif typ == "Substitution":
            on = self.m.name(e["substitution"]["replacement"])
            self.off.add(who)
            for n in [n for n in self.notes.get(full, []) if "note:" + n not in self.said][:1]:  # a word on the man going off
                self.said["note:" + n] = t
                self.fresh.append((t, n, None, 60))
            self.info.pop(who, None)
            if who in self.recent:
                self.recent.remove(who)
            d = self.st.score[self.st.side(team)] - self.st.score[1 - self.st.side(team)]
            if mn < 45 and self.coach.get(team):
                self.fresh.append((t, None, f"A change before half-time. That tells you what {self.coach[team]} thinks of this first half.", 60))
            self.fresh.append((t, None, f"{team} are chasing this and need fresh legs." if d < 0 else f"{team} looking to see this out with fresh legs."
                               if d > 0 else f"A change for {team}, looking for something different.", 40))
            note = self.notes.get(on)
            if note:
                self.said["note:" + note[0]] = t
                self.fresh.append((t, note[0], None, 60))
        if loc and on_ball and typ in ("Pass", "Carry", "Shot", "Dribble"):
            if loc[0] >= 80:
                tm["final"] += 1
                tm["left" if loc[1] < 27 else "right" if loc[1] > 53 else "middle"] += 1
                self.roll["final"][mn][team] += 1
        if loc and typ in ("Ball Recovery", "Interception") and loc[0] >= 80:
            tm["high_wins"] += 1

    # ---------------------------------------------------------------- shelves

    def _reading(self, minute: int) -> list[tuple[str, str]]:
        """How the match is going, as conclusions. Numbers only where the number is the point."""
        h, a = self.m.home, self.m.away
        s = self.st.snapshot()
        out = []
        for team, other in ((h, a), (a, h)):
            me, them, c = s[team], s[other], self.tm[team]
            if minute >= 20 and them["shots"] == 0 and me["shots"] >= 3:
                out.append((f"noshot:{other}", f"{other} have not managed a single shot yet."))
            elif me["shots"] >= 3 * max(them["shots"], 1) and me["shots"] >= 6:
                out.append((f"chances:{team}", f"All the chances are falling to {team}."))
            if me["possession_pct"] >= 60 and minute >= 15:
                out.append((f"ball:{team}", f"{team} are seeing most of the ball, and {other} are having to be patient."))
            tilt = c["final"] + self.tm[other]["final"]
            if tilt >= 40 and c["final"] >= .7 * tilt:
                out.append((f"tilt:{team}", f"Almost all of this is being played in {other}'s half."))
            if c["final"] >= 30:
                side = max(("left", "middle", "right"), key=lambda k: c[k])
                if c[side] >= .45 * c["final"]:
                    out.append((f"side:{team}:{side}", f"{team} keep attacking down the {side}; that is where it is working for them." if side != "middle"
                                else f"{team} are trying to play through the middle every time."))
            if c["high_wins"] >= 4:
                out.append((f"press:{team}:{c['high_wins'] // 3}", f"{team} keep winning the ball back high up the pitch."))
            if me["pass_pct"] < 72 and minute >= 20:
                out.append((f"sloppy:{team}", f"{team} are giving the ball away far too easily."))
            if team in self.best:
                out.append((f"once:best:{team}:{self.best[team][1]}", f"The best chance {team} have had fell to {self.best[team][1]}, and it went begging."))
            if c["crosses"] >= 8 and me["shots"] <= c["crosses"] // 4:
                out.append((f"once:crosses:{team}:{c['crosses'] // 8}", f"{team} have put a lot of crosses in without finding anybody."))
            if c["counters"] >= 2:
                out.append((f"once:counters:{team}:{c['counters'] // 2}", f"{team} look dangerous every time they break."))
            gk = next((w for w, i in self.info.items() if i["team"] == team and i["role"] == "gk"), None)
            if gk and minute >= 25 and self.pl[gk]["saves"] == 0:
                out.append((f"once:idle:{gk}:{minute // 30}", f"{gk} has not had a save to make."))
            if c["offside"] >= 3:
                out.append((f"offside:{team}", f"{team} keep getting caught offside."))
            dry = minute - self.last_shot_min.get(team, 0)
            if dry >= 15 and team in self.last_shot_min:
                out.append((f"dry:{team}:{dry // 10}", f"{team} have gone {dry} minutes without a shot."))
            five = sum(self.per_min[k][team] for k in range(minute - 5, minute))
            five_o = sum(self.per_min[k][other] for k in range(minute - 5, minute))
            if five >= 30 and five >= 4 * max(five_o, 1):
                out.append((f"hold:{team}:{minute // 5}", f"{other} simply cannot get hold of the ball at the moment."))
            box = sum(self.roll["final"][k][team] for k in range(minute - 5, minute + 1))
            if box >= 25:
                out.append((f"siege:{team}:{minute // 5}", f"{team} are camped in the final third now."))
        last, before = self.st.mom[-10:], self.st.mom[-20:-10]
        ph, pa = sum(r[0] for r in last), sum(r[1] for r in last)
        if abs(ph - pa) >= 3:
            now = h if ph > pa else a
            bh, ba = sum(r[0] for r in before), sum(r[1] for r in before)
            turned = before and (bh - ba) * (ph - pa) < 0 and abs(bh - ba) >= 2
            out.append((f"mom:{now}:{minute // 10}", f"This has turned. The last ten minutes have belonged to {now}." if turned
                        else f"The last ten minutes have belonged to {now}."))
        xg_gap = s[h]["xg"] - s[a]["xg"]
        if abs(xg_gap) >= 1.0:
            lead = h if xg_gap > 0 else a
            level = self.st.score[0] == self.st.score[1]
            out.append((f"xg:{lead}:{level}", f"On the balance of chances {lead} should be in front, and yet it is level." if level
                        else f"On the balance of chances {lead} have been the better side."))
        f10 = sum(self.roll["fouls"][k][t] for k in range(minute - 10, minute + 1) for t in (h, a))
        if f10 >= 5:
            out.append((f"scrappy:{minute // 10}", "It is getting scrappy; the fouls are coming more and more often."))
        shots = s[h]["shots"] + s[a]["shots"]
        if shots >= 6:
            inside = self.tm[h]["box_shots"] + self.tm[a]["box_shots"]
            out.append((f"box:{inside * 3 // shots}", "The defences are holding their shape; most of the shooting has been from distance."
                        if inside < .4 * shots else "Most of the shots have come from inside the box; these are real chances."))
        return out

    def _pattern(self, t: float) -> list[tuple[str, str]]:
        """What this possession looks like, while it is still going on."""
        p = self.poss
        if not p["team"] or t - p["last"] > 4:
            return []
        team, other = p["team"], self.m.away if p["team"] == self.m.home else self.m.home
        out = []
        if p["pressed"] >= 4:
            out.append((f"pat:press:{other}", f"{other} are hunting the ball, trying to force the mistake."))
        if p["switches"] >= 2:
            out.append((f"pat:switch:{team}", f"From one side to the other for {team}, looking for the gap."))
        if p["gk"] and p["passes"] >= 4:
            out.append((f"pat:gk:{team}", f"All the way back to the goalkeeper; {team} will start again."))
        if p["passes"] >= 12:
            out.append((f"pat:long:{team}", f"That is a long spell of possession for {team}."))
        if p["deep"] >= 8 and p["pressed"] <= 1:
            out.append((f"pat:deep:{team}", f"{other} are letting them have it in there, and keeping their shape."))
        if p["passes"] >= 7:
            out.append((f"pat:keep:{team}", f"{team} happy to keep the ball for now, in no hurry at all."))
        return out

    def _stakes(self, minute: int) -> list[tuple[str, str]]:
        h, a = self.m.home, self.m.away
        d = self.st.score[0] - self.st.score[1]
        lead, trail = (h, a) if d > 0 else (a, h)
        out = [(f"score:{minute // 6}", f"{minute} minutes played, {self.st.scoreline()}.")] if minute >= 5 else []
        last = self.goals[-1][0] if self.goals else minute
        if minute - last >= 6:  # the story so far, for anyone just joining
            by = defaultdict(list)
            for _, who, team in self.goals:
                by[team].append(who)
            out.append((f"recap:{len(self.goals)}:{minute // 25}", " ".join(
                f"{' and '.join(dict.fromkeys(w))} with the {'goals' if len(w) > 1 else 'goal'} for {team}." for team, w in by.items())))
        for lo, hi, key, text in ((40, 45, "ht", "Not long until half-time now."), (58, 63, "hour", "An hour gone in this final."),
                                  (84, 90, "last5", "Into the closing minutes of normal time."),
                                  (101, 105, "et-ht", "Coming up to half-time in extra time."),
                                  (114, 121, "pens", "Penalties are only minutes away now, unless somebody finds a winner." if d == 0
                                   else f"{trail} are running out of time to save this.")):
            if lo <= minute < hi:
                out.append(("once:clock:" + key, text))
        cl, ct = self.coach.get(lead), self.coach.get(trail)
        for ok, key, text in (  # opinion, not fact: what anyone watching would be wondering
                (d == 0 and minute < 30, "cagey", "A cagey start would be no surprise, with this much at stake."),
                (abs(d) >= 2 and 30 <= minute < 45 and ct, "talk", f"{ct} has a big team talk to give at half-time."),
                (abs(d) >= 2 and minute < 75, "switch", f"{lead} must not switch off; one goal changes everything in a final."),
                (abs(d) == 1 and 55 <= minute < 80 and cl, "stick", f"Does {cl} stick or twist now? Protect the lead, or go for another?"),
                (d != 0 and 55 <= minute < 85 and ct, "gamble", f"{ct} has to gamble at some point."),
                (d == 0 and minute >= 75, "mistake", "Nobody wants to be the one who makes the mistake that loses a World Cup final."),
                (minute >= 95, "legs", "Who has anything left in their legs? That might decide this.")):
            if ok:
                out.append(("once:muse:" + key, text))
        if minute < 12:
            out.append(("early", "Early days, and both sides are still feeling their way into this final."))
        elif d == 0 and 35 <= minute < 45:
            out.append(("ht-level", "Nothing in it as we come towards half-time."))
        elif d == 0 and 78 <= minute < 90:
            out.append(("late-level", "The next goal could decide the World Cup final."))
        elif abs(d) >= 2 and minute >= 30:
            out.append((f"control:{minute // 20}", f"{lead} are in complete control of this final." if minute < 70
                        else f"{trail} need something extraordinary now."))
        elif abs(d) == 1 and minute >= 70:
            out.append((f"chase:{minute // 8}", f"{trail} need a goal, and time is against them."))
        elif abs(d) == 1 and minute >= 25:
            out.append((f"edge:{minute // 20}", f"{lead} have the lead, but there is nothing comfortable about one goal in a final."))
        if minute >= 90:
            out.append((f"et:{minute // 10}", "Tired legs everywhere now. This is extra time in a World Cup final."))
        return out

    def _people(self, minute: int) -> list[tuple[str, str]]:
        """About whoever was just involved: background first, then how their night is going."""
        out = []
        for who in reversed(self.recent):
            info, st = self.info.get(who), self.pl[who]
            if not info:
                continue
            out += [("note:" + n, n) for n in self.notes.get(info["full"], [])]
            for stat, step, text in FORM:
                if st[stat] >= step:
                    out.append((f"once:{stat}:{who}:{st[stat] // step}", text.format(who=who, team=info["team"])))
            if who in self.booked:
                out.append((f"once:booked:{who}", f"{who} is on a yellow card, remember, and has to be careful."))
            if st["fouled"] >= 3:
                out.append((f"kicked:{who}", f"{who} has been fouled again and again tonight."))
            if st["shots"] >= 3:
                out.append((f"shooter:{who}", f"{who} keeps finding shooting positions."))
            if st["dribbles"] >= 3:
                out.append((f"runner:{who}", f"{who} is causing real problems running with the ball."))
            if st["fouls"] >= 3:
                out.append((f"fouler:{who}", f"{who} is walking a tightrope with those fouls."))
            if st["passes"] >= 30 and st["passes_ok"] >= .92 * st["passes"]:
                out.append((f"tidy:{who}", f"{who} has hardly misplaced a pass all night."))
        if self.info:
            top = max(self.info, key=lambda w: self.pl[w]["touches"])
            if self.pl[top]["touches"] >= 30:
                out.append((f"hub:{top}", f"Everything is going through {top} at the back." if self.info[top]["role"] == "def"
                            else f"{top} is running this game; nobody has seen more of the ball."))
            if minute >= 30:
                quiet = [w for w, i in self.info.items() if i["role"] == "fwd" and self.pl[w]["touches"] <= minute // 6]
                out += [(f"quiet:{w}:{minute // 20}", f"{w} has hardly had a kick so far.") for w in quiet[:2]]
        return out

    def _general(self, minute: int) -> list[tuple[str, str]]:
        reading, stakes = self._reading(minute), self._stakes(minute)
        notes = [("note:" + n, n) for about in ("match", self.m.home, self.m.away) for n in self.notes.get(about, [])]
        k = self.turn
        notes = notes[k % len(notes):] + notes[:k % len(notes)] if notes else []
        reading = reading[k % len(reading):] + reading[:k % len(reading)] if reading else []
        mixed = []  # interleave so consecutive slots change subject: reading, background, stakes, reading...
        for i in range(max(len(reading), len(notes), len(stakes))):
            mixed += reading[i:i + 1] + notes[i:i + 1] + stakes[i:i + 1]
        return mixed

    def _free(self, key: str, t: float) -> bool:
        # background is said once; a reading of the match may come round again after six minutes (in new words, see Writer)
        if key.startswith("score:"):
            return t - self.last_score >= 240.0
        if key.startswith("note:") and t - self.said.get("theme:note", -1e9) < 45 and t - self.poss["last"] < 20:
            return False
        th = theme(key)
        if th and t - self.said.get("theme:" + th[0], -1e9) < th[1]:
            return False
        return t - self.said.get(key, -1e9) >= (1e9 if key.startswith("once:") else 2700.0 if key.startswith("note:")  # the big storylines do come round again, late on
                                                 else 420.0 if key.startswith("pat:") else 900.0)

    def flowing(self) -> bool:
        """A big moment is being talked through (a goal, a penalty): the sentences follow one another without a breath."""
        return any(len(f) > 3 and f[3] >= 60 for f in self.fresh)

    def next(self, t: float, minute: int, named: bool, strict: bool = False) -> tuple[str, int] | None:
        """The next thing worth saying, and how many times that subject has come up before (the writer varies the wording).
        named=True prefers a sentence about a player; strict never crosses over to the other kind."""
        self.fresh = [f for f in self.fresh if t - f[0] <= (f[3] if len(f) > 3 else 40)]
        for f in list(reversed(self.fresh)):  # a verdict on what just happened comes before anything else
            replay = len(f) > 3 and f[3] >= 60  # talking a big moment through: the version with the names is the point
            text = (f[1] or f[2]) if replay else (f[1] or (None if strict else f[2])) if named else f[2]
            if text and t - self.said.get("f:" + text, -1e9) < 300:
                self.fresh.remove(f)  # the same verdict twice in five minutes is a tic, not commentary
                continue
            if text:
                self.fresh.remove(f)
                self.said["f:" + text] = t
                self.uses[text] += 1  # the same verdict on a later incident gets different words
                return text, self.uses[text] - 1
        general = self._pattern(t) + self._general(minute)
        order = (self._people(minute), general) if named else (general, self._people(minute))
        for shelf in order[:1] if strict else order:
            for key, text in shelf:
                if self._free(key, t) and t - self.said.get("f:" + text, -1e9) >= 480:  # a new key is not a new sentence
                    self.said[key] = self.said["f:" + text] = t
                    if key.startswith("note:"):
                        self.said["theme:note"] = t
                    if theme(key):
                        self.said["theme:" + theme(key)[0]] = t
                    if key.startswith("score:"):
                        self.last_score = t
                    self.turn += 1
                    self.uses[key] += 1
                    return text, self.uses[key] - 1
        return None


# ---------------------------------------------------------------- the writer: a model re-voices the draft

SYSTEM = ("You are the co-commentator on a live football broadcast. Say the DRAFT the way you would say it on air: natural, "
          "conversational, one or two short sentences, at most 24 words. Keep the meaning exactly: whoever is doing well stays "
          "doing well, whoever is struggling stays struggling. Keep every name and every number in the draft exactly as written. "
          "Add no new facts, names, numbers, positions or events. Output only what you would say.")
SHOTS = [("Situation: 20 minutes played, Riverton 0-0 Eastfield.\nDraft: Eastfield are giving the ball away far too easily.",
          "Too many loose passes from Eastfield. They just can't keep hold of it."),
         ("Situation: 55 minutes played, Riverton 1-0 Eastfield.\nDraft: Okafor will feel that should have gone in.",
          "Oh, Okafor will be kicking himself. That really should have been a goal."),
         ("Situation: 70 minutes played, Riverton 1-0 Eastfield.\nDraft: Ruiz is 34, the senior man in that Riverton defence.",
          "And that's the experience of Ruiz for you, 34 now, the senior man in that Riverton defence.")]
JUDGE = ("You check a football co-commentator's rewording of a draft line. Answer with one word. SAME: the rewording says what "
         "the draft says (more colourful is fine). DIFFERENT: it reverses the meaning, changes who did what, or claims something "
         "the draft does not say.")
JUDGE_SHOTS = [
    ("Draft: Eastfield keep getting caught offside.\nRewording: Eastfield are struggling to defend offside traps again.", "DIFFERENT"),
    ("Draft: Okafor will feel that should have gone in.\nRewording: Oh, Okafor will be kicking himself. That really should have been a goal.", "SAME"),
    ("Draft: Riverton keep attacking down the left; that is where it is working for them.\nRewording: Riverton are struggling down the left flank.", "DIFFERENT"),
    ("Draft: Almost all of this is being played in Eastfield's half.\nRewording: Eastfield just can't get out of their own half.", "SAME"),
    ("Draft: Ruiz has hardly misplaced a pass all night.\nRewording: Ruiz continues to dominate midfield, not a pass out of place.", "DIFFERENT"),
    ("Draft: Eastfield are giving the ball away far too easily.\nRewording: Too many loose passes from Eastfield. They just can't keep hold of it.", "SAME")]
try:  # the prompt chosen by measurement (tuning/prompts.py); the constants above stay the baseline it is compared with
    _tuned = json.loads((engine.ROOT / "tuning" / "prompts.json").read_text(encoding="utf-8"))
    _tuned = _tuned["active_prompt"] if _tuned["active"] != "baseline" else {}
except (OSError, ValueError, KeyError):
    _tuned = {}
WRITE_SPEC = harness.Spec("colour", _tuned.get("system", SYSTEM), [tuple(x) for x in _tuned.get("shots", SHOTS)], max_tokens=70,
                          temperature=_tuned.get("temperature", 0.8), attempts=3, soft=("opening",))
JUDGE_SPEC = harness.Spec("colour_judge", JUDGE, JUDGE_SHOTS, max_tokens=3, temperature=0, attempts=1)
QUOTES = "’‘“”—–…"


def opening(text: str) -> str:
    return " ".join(text.lower().replace(",", "").split()[:2])


class Writer:
    """Draft in, spoken line out. Cached on disk so the match can be replayed without the model running.

    A rewrite goes on air only if it passes three independent checks: the lexical guardrail (no new name, number or event),
    a same-meaning judge (measured: it catches about half of the rewrites that changed the meaning, and those were about
    one in seven before it), and an opening that has not been used in the last thirty lines. Up to three attempts; the
    draft itself is the fallback."""

    def __init__(self, m: engine.Match, live: bool):
        self.m = m
        self.url, self.model = engine.llm_config() if live else ("", "")
        self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        self.stats = defaultdict(int)
        self.openings: deque[str] = deque(maxlen=30)

    def _judge(self, draft: str, out: str) -> str:
        """The same-meaning judge, its own traced spec so its latency shows up next to the writer's."""
        word, verdict = harness.run(JUDGE_SPEC, f"Draft: {draft}\nRewording: {out}", self.url, self.model)
        if verdict.startswith("error"):
            raise harness.ModelError(verdict)  # a dead judge aborts the line, as a dead writer does
        return "" if word.upper().startswith("SAME") else "meaning"

    def _wrong(self, draft: str, out: str) -> str:
        """Why this rewrite may not go on air, or '' if it may."""
        # Only the draft licenses names and numbers. The situation is context: a model that reads "Argentina 2-0 France"
        # there will otherwise decide for itself which team committed the foul.
        ok, why = engine.grounded(out, {"draft": draft}, self.m.vocab, draft)
        if not ok:
            return why.split(":")[0]
        if "\n" in out or any(ord(ch) > 0x24F and ch not in QUOTES for ch in out):
            return "script"  # the model drifted into another language or started a second paragraph
        if not 3 <= len(out.split()) <= 28:
            return "length"
        return self._judge(draft, out)

    def write(self, draft: str, situation: str, variant: int = 0) -> str:
        """variant > 0 means this subject has come up before: same facts, different words."""
        key = hashlib.md5(f"{draft}#{variant}".encode()).hexdigest()
        if key in self.cache or not self.url:
            self.stats["cached" if key in self.cache else "draft"] += 1
            text = self.cache.get(key, draft)
            self.openings.append(opening(text))
            return text
        avoid = sorted({o.split()[0].capitalize() for o in list(self.openings)[-5:] if o})
        user = (f"Situation: {situation}\nDraft: {draft}"
                + ("\n(You have made this point earlier in the match. Make it again in different words.)" if variant else "")
                + (f"\n(Do not begin with: {', '.join(avoid)}.)" if avoid else ""))
        stale = lambda out: "opening" if opening(out) in self.openings else ""  # noqa: E731
        spec = dataclasses.replace(WRITE_SPEC, checks=[lambda out: self._wrong(draft, out), stale], fallback=draft)
        def tried(why: str) -> None:  # a stale opening is still a faithful try, as before
            self.stats["try:" + (why if why not in ("", "opening") else "ok")] += 1

        text, verdict = harness.run(spec, user, self.url, self.model, on_try=tried)
        verdict = {"ok": "llm", "spare:opening": "llm:stale opening"}.get(verdict, "rejected" if verdict.startswith("fallback") else verdict)
        self.stats[verdict] += 1
        self.openings.append(opening(text))
        if not verdict.startswith("error"):
            self.cache[key] = text
            if len(self.cache) % 50 == 0:  # the full match is a thousand calls: do not lose them to an interruption
                self.save()
        return text

    def save(self) -> None:
        CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")
