"""The football model: everything starts from the ball.

    ball status  (Laws of the Game: in play, or which restart and for whom)
      -> possession (which team, who)
        -> where (third, channel, penalty area)
          -> phase of play (build-up, progression, final third, chance, counter-attack, set piece)
            -> off-ball picture (free man, numbers, line broken)  = what makes the moment matter
              -> what a commentator says now, and how urgently

This file knows football, not video. It runs on any source that can say where the ball is and who is near it:
today the event + 360 data (perfect perception), later a vision model. `python game.py` prints a real move.
"""
from __future__ import annotations

import json
import math

import engine

GOAL = (120.0, 40.0)  # pitch is 120 x 80, the team in possession always attacks towards x = 120
RESTARTS = {"Corner": "corner", "Free Kick": "free_kick", "Throw-in": "throw_in", "Goal Kick": "goal_kick", "Kick Off": "kick_off"}


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def third(x: float) -> str:
    return "own third" if x < 40 else "middle third" if x < 80 else "final third"


def channel(y: float) -> str:
    return "left" if y < 27 else "right" if y > 53 else "centre"


def in_box(p) -> bool:
    return p[0] >= 102 and 18 <= p[1] <= 62


# ---------------------------------------------------------------- off-ball picture (needs player positions)

def picture(e: dict, frame: list[dict] | None) -> dict:
    """What the attacking side has around and ahead of the ball. `frame` is the 360 freeze frame of this event."""
    if not frame or not e.get("location"):
        return {}
    ball = e["location"][:2]
    mates = [p["location"] for p in frame if p["teammate"] and not p["actor"] and not p["keeper"]]
    opps = [p["location"] for p in frame if not p["teammate"] and not p["keeper"]]
    out = {}
    # free man: a team-mate level with or ahead of the ball whom nobody is marking.
    #   "range": within 30 yards of goal with no opponent inside 5 yards  -> a chance is on
    #   "space": anywhere in the attacking half with no opponent inside 10 yards -> the pass is on
    ahead = [p for p in mates if p[0] >= ball[0] - 5 and p[0] >= 55]
    gap = lambda p: min((dist(p, o) for o in opps), default=99.0)
    free = [(0, dist(p, GOAL), p) for p in ahead if dist(p, GOAL) <= 30 and gap(p) >= 5] or \
           [(1, dist(p, GOAL), p) for p in ahead if gap(p) >= 10]
    if free:
        kind, d, p = min(free)
        out["free_man"] = {"pos": p, "at": [round(p[0]), round(p[1])], "kind": "range" if kind == 0 else "space", "to_goal": round(d),
                           "side": "left" if p[1] < 36 else "right" if p[1] > 44 else "centre", "in_box": in_box(p),
                           "far_side": (p[1] - 40) * (ball[1] - 40) < 0 and abs(p[1] - ball[1]) >= 20, "marker_gap": round(gap(p))}
        out["mates"] = mates
    # numbers: attackers level with or ahead of the ball vs defenders goal-side of the ball
    att = 1 + sum(p[0] >= ball[0] - 2 for p in mates)
    dfn = sum(o[0] >= ball[0] - 2 for o in opps)
    if ball[0] >= 60 and dfn:
        out["numbers"] = (att, dfn)
    return out


def lines_broken(e: dict, frame: list[dict] | None) -> int:
    """Opponents taken out of the game by this pass: those between its start and end, within 12 yards of its path."""
    p = e.get("pass")
    if not p or not frame or "outcome" in p:
        return 0
    a, b = e["location"][:2], p["end_location"][:2]
    if b[0] - a[0] < 10:
        return 0
    n = 0
    for o in (q["location"] for q in frame if not q["teammate"] and not q["keeper"]):
        if a[0] + 1 < o[0] < b[0] - 1:
            t = (o[0] - a[0]) / (b[0] - a[0])
            n += abs(a[1] + t * (b[1] - a[1]) - o[1]) <= 12
    return n


# ---------------------------------------------------------------- the model

class Game:
    """Reduces a stream of ball events to: status, possession, where, phase, danger, and what is worth saying."""

    def __init__(self, m: engine.Match, frames: dict[str, list] | None = None):
        self.m, self.frames = m, frames or {}
        self.poss_id, self.poss_start_x, self.poss_start_t, self.max_x = None, 0.0, 0.0, 0.0

    def step(self, e: dict) -> dict | None:
        typ = e["type"]["name"]
        loc = e.get("location")
        if not loc or typ in ("Pressure", "Starting XI", "Half Start", "Half End", "Substitution", "Tactical Shift"):
            return None
        on_ball = e["team"]["name"] == e["possession_team"]["name"]
        frame = self.frames.get(e["id"])
        if e["possession"] != self.poss_id:  # new possession: remember where and when it began
            self.poss_id, self.poss_start_x, self.poss_start_t, self.max_x = e["possession"], loc[0] if on_ball else 120 - loc[0], e["t"], 0.0
        x = loc[0] if on_ball else 120 - loc[0]  # ball position in the attacking side's frame
        self.max_x = max(self.max_x, x)
        p = e.get("pass", {})
        restart = RESTARTS.get(p.get("type", {}).get("name")) or ("penalty" if e.get("shot", {}).get("type", {}).get("name") == "Penalty" else None)

        # phase: decided by the ball, in this order
        gained = x - self.poss_start_x
        elapsed = e["t"] - self.poss_start_t
        fast = elapsed > 0 and gained >= 30 and gained / max(elapsed, 1.0) >= 4.0  # 30+ yards at 4+ yards per second
        if restart:
            phase = "set piece"
        elif typ == "Shot" or (on_ball and in_box(loc)):
            phase = "chance"
        elif fast and x >= 50:
            phase = "counter-attack"
        else:
            phase = {"own third": "build-up", "middle third": "progression", "final third": "final-third attack"}[third(x)]

        pic = picture(e, frame) if on_ball and typ in ("Pass", "Carry", "Ball Receipt*", "Dribble", "Shot") else {}
        broken = lines_broken(e, frame) if on_ball and typ == "Pass" else 0
        # danger 0..1: how close this is to a goal. Drives urgency: what to say, and how fast.
        danger = {"build-up": .05, "progression": .15, "final-third attack": .4, "counter-attack": .5, "set piece": .2, "chance": .75}[phase]
        if restart in ("corner", "penalty") or (restart == "free_kick" and x >= 85):
            danger = max(danger, .9 if restart == "penalty" else .5)
        if pic.get("free_man"):
            danger += .25 if pic["free_man"]["kind"] == "range" else .1
        if typ == "Shot":
            danger = 1.0
        return {"t": e["t"], "clock": f'{e["minute"]:02d}:{e["second"]:02d}', "type": typ, "team": e["possession_team"]["name"],
                "player": self.m.name(e.get("player")), "on_ball": on_ball, "x": round(x), "y": round(loc[1] if on_ball else 80 - loc[1]),
                "restart": restart, "phase": phase, "danger": round(min(danger, 1.0), 2), "picture": pic, "lines_broken": broken,
                "recipient": self.m.name(p.get("recipient")), "pass": p, "shot": e.get("shot")}

    # ---- what to say: the commentator's rules, by phase
    def call(self, s: dict, prev_phase: str | None) -> str | None:
        """One state -> a line, or None when the right thing is to stay quiet. Order of the rules = priority."""
        who, to, team, pic = s["player"], s["recipient"], s["team"], s["picture"]
        side = lambda y: channel(y)
        if s["type"] == "Shot":
            out = s["shot"]["outcome"]["name"]
            return {"Goal": f"{who}... GOAL! {team} score!", "Saved": f"{who} shoots, saved!", "Blocked": f"{who} shoots, blocked!",
                    "Post": f"{who} hits the post!"}.get(out, f"{who} shoots, wide!")
        if not s["on_ball"]:
            return None
        if s["restart"] and s["type"] == "Pass":
            return {"corner": f"Corner to {team}, {who} to take.", "free_kick": f"Free kick, {team}. {who} over it." if s["x"] >= 70 else None,
                    "throw_in": f"{team} throw." if s["x"] >= 80 else None, "goal_kick": None, "kick_off": f"{team} get us going."}[s["restart"]]
        fm = pic.get("free_man")
        where = fm and ("the far side" if fm["far_side"] else f"the {fm['side']}")
        if s["phase"] == "counter-attack" and prev_phase != "counter-attack":
            return f"{team} break! {who} leads it" + (f", and there is space on {where}!" if fm else ".")
        if s["type"] == "Pass" and "outcome" not in s["pass"] and to:
            end = s["pass"]["end_location"]
            # the pass goes to the free man when he is the team-mate nearest to where the ball is played
            if fm and min(pic["mates"], key=lambda p: dist(p, end)) == fm["pos"]:
                return f"{to} is all alone on {where}! {who} picks the pass!"
            if s["pass"].get("cross"):
                return f"{who} crosses for {to}!"
            if s["pass"].get("through_ball") or s["lines_broken"] >= 3:
                return f"{who} splits them, {to} is through!" if end[0] >= 95 else f"{who} breaks the lines to find {to}."
            if abs(end[1] - s["y"]) >= 35:
                return f"{who} switches it to {to} on the {side(end[1])}."
            if s["phase"] in ("final-third attack", "chance", "counter-attack"):
                return f"{who}, to {to}."
            if s["phase"] == "progression":
                return f"{to}."  # just the name, on the receive
        return None  # build-up and everything else: the ball is not asking for words


def load(match_id: int = engine.MATCH_ID) -> tuple[engine.Match, dict]:
    m = engine.Match(match_id)
    try:  # fetched on first run like the events; a match without 360 data simply has no frames
        raw = engine._fetch(f"three-sixty/{match_id}.json", f"three-sixty_{match_id}.json")
    except OSError:
        raw = []
    return m, {f["event_uuid"]: f["freeze_frame"] for f in raw}


if __name__ == "__main__":
    m, frames = load()
    g, prev = Game(m, frames), None
    goal = next(e for e in m.events if e["type"]["name"] == "Shot" and e["shot"]["outcome"]["name"] == "Goal" and e["minute"] == 35)
    for e in m.events:
        s = g.step(e)
        if not s:
            continue
        if e["possession"] == goal["possession"]:
            line = g.call(s, prev)
            fm = s["picture"].get("free_man")
            extra = (f' free: {fm["kind"]}, {fm["side"]}, marker {fm["marker_gap"]}yd' if fm else "") + (f' breaks {s["lines_broken"]}' if s["lines_broken"] else "")
            if line or e["type"]["name"] != "Ball Receipt*":
                print(f'{e["timestamp"][3:10]} {s["type"]:<13} x={s["x"]:<3} {s["phase"]:<18} danger {s["danger"]:.2f}{extra:<34} | {line or "(quiet)"}')
        prev = s["phase"]
