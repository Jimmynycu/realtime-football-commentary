"""Four products from the one platform data set: subtitles, highlights, report, ratings (+ a search over all of it).

Why: the platform already knows what happened (events), what was said (lines on air) and when in the broadcast video
(video.json). Each product is a different cut of that same data, so none of them can contradict the broadcast.
Deterministic, no language model: every number and name below is read from the events or the lines.

    python products.py all | subtitles | highlights | report | ratings | search "Di María" | status

Conventions: minute = StatsBomb minute + 1 (a goal at 22:24 is "in the 23rd minute", as match reports print it).
Penalty-shootout kicks are not match goals, shots or xG anywhere here (same rule as engine.State).

ponytail: own goals do not occur in this match, so `goals()` reads only Shot/Goal events. Add 'Own Goal Against'
there before pointing this at another match.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from functools import lru_cache

import control
import engine
import game
from colour import short

ROOT = engine.ROOT
OUT = ROOT / "samples"
LEAD, TAIL = 20, 15  # seconds of video before / after a highlight moment


# ---------------------------------------------------------------- shared platform data

@lru_cache(maxsize=1)
def _plat():
    """(match, control room with the lines on air, video config); built once per process."""
    m = engine.Match()
    ctl = control.Control(m, game.load()[1])
    cfg = json.loads((ROOT / "video.json").read_text(encoding="utf-8"))
    return m, ctl, {"id": cfg["video_id"], "kick": {int(p): float(v) for p, v in cfg["kickoff"].items()}}


def vt(t: float) -> float:
    """Match seconds -> video seconds: the period's kick-off in the video plus time since that period started."""
    m, _, v = _plat()
    p = max(q for q in m.period_start if m.period_start[q] <= t)
    return v["kick"][p] + t - m.period_start[p]


def video_end() -> float:
    m, _, v = _plat()
    return v["kick"][5] + m.period_len[5]


def link(vsec: float) -> str:
    return f"https://www.youtube.com/watch?v={_plat()[2]['id']}&t={max(0, int(vsec))}s"


def on_air() -> list[dict]:
    """Lines on air in regulation and extra time, in order (the booth stops before the shootout)."""
    m, ctl, _ = _plat()
    return sorted((l for l in ctl.lines if l["t"] < m.period_start[5]), key=lambda l: l["t"])


def spoken(l: dict) -> str:
    """What was actually said: a line cut off by a more important one ends with a dash."""
    return " ".join(l["words"]) + (" —" if len(l["words"]) < l["n"] else "")


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 < n % 100 < 14 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


LIMIT = {1: 45, 2: 90, 3: 105, 4: 120}


def minute_of(e: dict) -> tuple[str, str]:
    """('23\'', 'in the 23rd minute'); stoppage time reads '45+2\'' / 'in stoppage time'."""
    mi = e["minute"] + 1
    if mi > LIMIT.get(e["period"], 999):
        return f"{LIMIT[e['period']]}+{mi - LIMIT[e['period']]}'", "in stoppage time"
    return f"{mi}'", f"in the {ordinal(mi)} minute" + (" (extra time)" if e["period"] >= 3 else "")


@lru_cache(maxsize=1)
def _clash() -> frozenset:
    """Surnames shared by two players of the match (the two Martinez of Argentina): those keep their first name."""
    seen = defaultdict(set)
    for n in _plat()[0].names.values():
        seen[short(n)].add(n)
    return frozenset(k for k, v in seen.items() if len(v) > 1)


def disp(full: str | None) -> str:
    """The name people use: surname, with colour.short's aliases (Kolo Muani), full name only where surnames clash."""
    s = short(full)
    return full if s in _clash() else (s or "?")


def nm(e: dict) -> str:
    return disp(_plat()[0].name(e.get("player")))


def goals() -> list[dict]:
    """Match goals (shootout excluded) with scorer, assist, running score and who led."""
    m = _plat()[0]
    score, out = [0, 0], []
    for e in m.events:
        if e["period"] > 4 or e["type"]["name"] != "Shot" or e["shot"]["outcome"]["name"] != "Goal":
            continue
        i = 0 if e["team"]["name"] == m.home else 1
        before = score[i] - score[1 - i]
        score[i] += 1
        a = m.by_id.get(e["shot"].get("key_pass_id", ""))
        out.append({"e": e, "team": e["team"]["name"], "who": nm(e), "pen": e["shot"]["type"]["name"] == "Penalty",
                    "assist": nm(a) if a else None, "before": before, "score": tuple(score), "side": i,
                    "label": minute_of(e)[0], "when": minute_of(e)[1], "xg": e["shot"]["statsbomb_xg"]})
    return out


# ---------------------------------------------------------------- 1. subtitles

def _stamp(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


VOICES = {"pbp": "Play-by-play", "analyst": "Analyst"}


def subtitles() -> str:
    """WebVTT of the commentary on the video clock. Cue end = start + the line's duration, clamped to the next start
    so cues never overlap; a cue that would be empty after clamping is dropped by merging into a 0.2 s minimum."""
    lines = on_air()
    cues = ["WEBVTT", "", "NOTE Commentary from the live-commentary platform, synced to the FIFA broadcast video."]
    for i, l in enumerate(lines):
        a = vt(l["t"])
        b = a + max(l.get("dur") or 0, 0.2)
        if i + 1 < len(lines):
            b = min(b, vt(lines[i + 1]["t"]))
        text = spoken(l).replace("&", "&amp;").replace("<", "&lt;")
        cues += ["", f"{i + 1}", f"{_stamp(a)} --> {_stamp(max(b, a))}", f"<v {VOICES.get(l['voice'], l['voice'])}>{text}"]
    OUT.mkdir(exist_ok=True)
    (OUT / "commentary.vtt").write_text("\n".join(cues) + "\n", encoding="utf-8")
    return str(OUT / "commentary.vtt")


# ---------------------------------------------------------------- 2. highlights

def _shots() -> list[dict]:
    m = _plat()[0]
    return [e for e in m.events if e["period"] < 5 and e["type"]["name"] == "Shot"]


def highlights(n: int = 8, write: bool = True) -> list[dict]:
    """The n clips worth cutting: every goal (penalties included), then the biggest non-goal chances by xG, at least
    45 s away from a clip already picked (a save and its rebound are one moment). Output is chronological."""
    m, ctl, _ = _plat()
    picked = [(e, "goal") for e in (g["e"] for g in goals())]
    for e in sorted(_shots(), key=lambda s: -s["shot"]["statsbomb_xg"]):
        if len(picked) >= n or e["shot"]["outcome"]["name"] == "Goal":
            continue
        if all(abs(e["t"] - p["t"]) > 45 for p, _ in picked):
            picked.append((e, "chance"))
    picked = sorted(picked, key=lambda p: p[0]["t"])
    st, score, out = engine.State(m), None, []
    wanted = {id(e): why for e, why in picked}
    by_goal = {g["e"]["id"]: g for g in goals()}
    for e in m.events:
        st.apply(e)
        if id(e) not in wanted:
            continue
        sh, who, mark = e["shot"], nm(e), minute_of(e)[0]
        if wanted[id(e)] == "goal":
            g = by_goal[e["id"]]
            how = "penalty" if g["pen"] else "goal"
            title = f"{mark} {who} {how} for {e['team']['name']} ({st.scoreline()})"
            why = f"{'penalty ' if g['pen'] else ''}goal"
        else:
            keeper = next((m.by_id[r] for r in e.get("related_events", [])
                           if r in m.by_id and m.by_id[r]["type"]["name"] == "Goal Keeper"), None)
            res = {"Saved": f"saved by {nm(keeper)}" if keeper else "saved", "Saved to Post": "saved onto the post",
                   "Post": "hits the post", "Blocked": "blocked"}.get(sh["outcome"]["name"], "off target")
            title = f"{mark} {who} chance for {e['team']['name']}, {res} ({sh['statsbomb_xg']:.2f} xG)"
            why = f"chance, {sh['statsbomb_xg']:.2f} xG"
        v0 = max(vt(e["t"]) - LEAD, _plat()[2]["kick"][e["period"]])
        v1 = vt(e["t"]) + TAIL
        out.append({"title": title, "why": why, "clock": ctl.clock(e["t"]), "match_t": round(e["t"], 1),
                    "video_start": int(v0), "video_end": int(v1) + 1, "link": link(v0),
                    "lines": [{"clock": ctl.clock(l["t"]), "voice": l["voice"], "text": spoken(l)}
                              for l in on_air() if e["t"] - LEAD <= l["t"] <= e["t"] + TAIL]})
    if write:
        OUT.mkdir(exist_ok=True)
        (OUT / "highlights.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        md = ["# Highlights: edit list", "", "Video clips to cut from the broadcast; nothing is downloaded. "
              "Goals first by rule, then the biggest chances by xG. Times are video seconds.", ""]
        for i, h in enumerate(out, 1):
            md += [f"## {i}. {h['title']}", "", f"- Match clock {h['clock']} | {h['why']}",
                   f"- Clip {h['video_start']}s to {h['video_end']}s ({h['video_end'] - h['video_start']} s): [{h['link']}]({h['link']})",
                   "- On air:"] + [f"  - {l['clock']} ({VOICES.get(l['voice'], l['voice'])}) {l['text']}" for l in h["lines"]] \
                  + [""]
        (OUT / "highlights.md").write_text("\n".join(md), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 3. match report + social posts

def _span(mom, ctl, m, sign: int):
    """Best 10-minute window for one side: max of sign*(home-away) pressure, inside one period."""
    best = None
    for b in range(len(mom) - 9):
        if ctl.clock(b * 60)[:2] and max(q for q in m.period_start if m.period_start[q] <= b * 60) != \
                max(q for q in m.period_start if m.period_start[q] <= b * 60 + 599):
            continue
        d = sign * sum(r[0] - r[1] for r in mom[b:b + 10])
        if best is None or d > best[0]:
            best = (d, b)
    return best


def _window(ctl, m, b: int) -> str:
    def lab(t):
        p, mi = max(q for q in m.period_start if m.period_start[q] <= t), int(ctl.clock(t).split(":")[0]) + 1
        return f"{LIMIT[p]}+{mi - LIMIT[p]}'" if mi > LIMIT[p] else f"{mi}'"
    return f"{lab(b * 60)} and {lab(b * 60 + 599)}"


def _join(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _goal_clause(g: dict, first: bool, home: str) -> str:
    """How one goal reads, from the score it changed. Variants rotate with the goal's place so openings differ."""
    t, who, w, how = g["team"], g["who"], g["when"], " from the penalty spot" if g["pen"] else ""
    s = f"{g['score'][0]}-{g['score'][1]}"
    prior = sum(g["score"]) - 1
    after = g["before"] + 1
    if prior == 0:
        return f"{t} took the lead {w}, {who} scoring{how or (' after ' + g['assist'] + ' set him up' if g['assist'] else '')}."
    if after == 0:
        return f"{who} levelled for {t} {w}{how}, making it {s}."
    if after == 1 and g["before"] == 0:
        return f"{t} edged back in front {w} through {who}{how}."
    if after > 1:
        return f"{who} made it {s} {w}{(' after a move created by ' + g['assist']) if g['assist'] and not g['pen'] else how}."
    return f"{who} pulled one back for {t} {w}{how}."


def report() -> str:
    """Match report and three social posts, written from goals, State.snapshot(), State.mom and the shootout tally."""
    m, ctl, _ = _plat()
    st = engine.State(m)
    for e in m.events:
        st.apply(e)
    gl, snap = goals(), st.snapshot()
    h, a = m.home, m.away
    assert tuple(st.score) == tuple(m.truth), "state score disagrees with ground truth"
    so = st.shootout
    win = h if so[0] > so[1] else a
    lose = a if win == h else h
    so_txt = f"{max(so)}-{min(so)}"
    final = f"{st.score[0]}-{st.score[1]}"
    draw = st.score[0] == st.score[1]
    tally = defaultdict(int)
    for g in gl:
        tally[g["who"]] += 1
    top, topn = max(tally.items(), key=lambda kv: kv[1])
    hat = f" Top scorer: {top}, with {'a hat-trick' if topn == 3 else str(topn) + ' goals'}." if topn >= 2 else ""
    stage = m.title.rsplit(" ", 1)[-1]
    head = (f"{win} beat {lose} {so_txt} on penalties after a {final} draw in the World Cup {stage.lower()}"
            if draw and sum(so) else f"{h} {final} {a} in the World Cup {stage.lower()}")
    # story: one sentence per goal, quick doubles fused into one sentence
    story, i = [], 0
    while i < len(gl):
        g = gl[i]
        nxt = gl[i + 1] if i + 1 < len(gl) else None
        gap = int(nxt["e"]["t"] - g["e"]["t"]) if nxt else None
        if nxt and gap < 150 and nxt["team"] == g["team"]:
            again = "he" if nxt["who"] == g["who"] else nxt["who"]
            story.append(f"{_goal_clause(g, i == 0, h)[:-1]}, and {again} struck again just {gap} seconds later"
                         f" to make it {nxt['score'][0]}-{nxt['score'][1]}.")
            i += 2
            continue
        story.append(_goal_clause(g, i == 0, h))
        i += 1
    if draw and sum(so):
        story.append(f"With the score still {final} after extra time, the shootout settled it, and {win} won it {so_txt}.")
    hs, as_ = snap[h], snap[a]
    story.append(f"By the numbers, {h} had {hs['possession_pct']}% of the ball and {hs['shots']} shots "
                 f"({hs['xg']} xG) against {a}'s {as_['shots']} ({as_['xg']} xG).")
    mom = st.mom
    swings = []
    for who, sign in ((h, 1), (a, -1)):
        r = _span(mom, ctl, m, sign)
        if r:
            swings.append((who, _window(ctl, m, r[1]), r[0]))
    if len(swings) == 2:
        (w1, s1, _), (w2, s2, _) = swings
        story.append(f"On pressure, {w1}'s strongest stretch ran between {s1}, and {w2}'s between {s2}.")
    # social posts
    code = lambda t: t[:3].upper()
    scorers = ", ".join(f"{w} {c}" for w, c in sorted(tally.items(), key=lambda kv: -kv[1]))
    single = f"{h} {final} {a} after extra time, and {win} win {so_txt} on penalties. Goals: {scorers}."
    single = single if len(single) <= 280 else f"{h} {final} {a} (pens {so_txt}). {scorers}."
    thread = [f"1/5 {h} {final} {a}, and {win} win the {stage.lower()} {so_txt} on penalties.",
              f"2/5 {gl[0]['who']} opened the scoring {gl[0]['when']}" + (f" and {gl[1]['who']} made it {gl[1]['score'][0]}-"
                                                                             f"{gl[1]['score'][1]} {gl[1]['when']}." if len(gl) > 1 else "."),
              f"3/5 {gl[2]['who']} scored {gl[2]['when']} and again {int(gl[3]['e']['t'] - gl[2]['e']['t'])} seconds later "
              f"to level at {gl[3]['score'][0]}-{gl[3]['score'][1]}." if len(gl) > 3 else "3/5 The second half turned the game.",
              f"4/5 Extra time: {gl[4]['who']} {gl[4]['when']}, then {gl[5]['who']} {gl[5]['when']} for {final}."
              if len(gl) > 5 else "4/5 Extra time could not separate them.",
              f"5/5 {h} {hs['possession_pct']}% of the ball, {hs['shots']}-{as_['shots']} on shots, "
              f"{hs['xg']}-{as_['xg']} on xG. Then penalties: {win} {so_txt}."]
    words = m.title.replace("FIFA ", "").split()
    yr = next((w for w in words if w.isdigit()), "")
    mid = "".join(w for w in words if w != yr and w != stage)
    tags = " ".join(f"#{t}" for t in (f"{mid}{stage}", f"{mid}{yr}", code(h) + code(a)))
    caption = f"{h} {final} {a}, then {so_txt} on penalties to {win}. {scorers}. {tags}"
    posts = [single, *thread, caption]
    assert all(len(p) <= 280 for p in posts), [len(p) for p in posts]
    swing_note = "pressure = shots weighted by xG + final-third actions, per minute (State.mom)"
    md = [f"# {head}", "", f"*{m.title}. {h} {final} {a} after extra time"
          + (f"; {win} {so_txt} on penalties" if sum(so) else "") + f".{hat}*", "",
          " ".join(story), "", "## Numbers", "",
          f"| | {h} | {a} |", "|---|---|---|"]
    for lab, k in (("Possession %", "possession_pct"), ("Shots", "shots"), ("On target", "on_target"), ("xG", "xg"),
                   ("Pass accuracy %", "pass_pct"), ("Corners", "corners"), ("Fouls", "fouls"), ("Yellow cards", "yellow")):
        md.append(f"| {lab} | {hs[k]} | {as_[k]} |")
    md += ["", f"Momentum: {swing_note}.", "", "## Social", "", f"### Single post ({len(single)} chars)", "", single, "",
           "### Thread (5 posts)", ""] + [f"- {p}" for p in thread] + \
          ["", "### Caption", "", caption, ""]
    OUT.mkdir(exist_ok=True)
    (OUT / "report.md").write_text("\n".join(md), encoding="utf-8")
    return str(OUT / "report.md")


# ---------------------------------------------------------------- 4. player ratings

WON = ("Won", "Success", "Success In Play", "Success Out")


def _cards() -> dict:
    """One tally per player id from the events (shootout kept apart)."""
    m = _plat()[0]
    P = {}
    for e in m.events:
        pl = e.get("player")
        if not pl:
            continue
        c = P.setdefault(pl["id"], defaultdict(float, name=disp(m.name(pl)), team=e["team"]["name"], gk=False))
        if "Goalkeeper" in e.get("position", {}).get("name", ""):
            c["gk"] = True
        typ = e["type"]["name"]
        if e["period"] == 5:  # shootout: kept apart so no match stat is polluted
            if typ == "Shot":
                c["so_taken"] += 1
                c["so_goals"] += e["shot"]["outcome"]["name"] == "Goal"
            elif typ == "Goal Keeper" and e["goalkeeper"]["type"]["name"] == "Penalty Saved":
                c["so_saves"] += 1
            continue
        if typ in ("Ball Receipt*", "Pass", "Shot", "Dribble", "Clearance", "Interception", "Ball Recovery",
                   "Miscontrol", "Dispossessed"):
            c["touches"] += 1
        if typ == "Pass":
            p = e["pass"]
            c["passes"] += 1
            c["passes_ok"] += "outcome" not in p
            c["chances"] += bool(p.get("shot_assist") or p.get("goal_assist"))
        elif typ == "Shot":
            c["shots"] += 1
            c["xg"] += e["shot"]["statsbomb_xg"]
            c["goals"] += e["shot"]["outcome"]["name"] == "Goal"
        elif typ == "Dribble":
            c["dribbles"] += e["dribble"]["outcome"]["name"] == "Complete"
        elif typ == "Interception":
            c["wins"] += e["interception"]["outcome"]["name"] in WON
        elif typ == "Ball Recovery":
            c["wins"] += not e.get("ball_recovery", {}).get("recovery_failure")
        elif typ == "Duel":
            d = e["duel"]
            c["wins"] += d["type"]["name"] == "Tackle" and d.get("outcome", {}).get("name") in WON
        elif typ == "Foul Committed":
            c["fouls"] += 1
        elif typ in ("Dispossessed", "Miscontrol"):
            c["lost"] += 1
        elif typ == "Goal Keeper":
            gt = e["goalkeeper"]["type"]["name"]
            c["saves"] += gt == "Shot Saved"
            c["conceded"] += gt == "Goal Conceded"
    return P


def _rate(c) -> float:
    """Rating = 6.0 + 1.2*goals + 0.5*xG + 0.5*chances created + 0.2*ball wins + 0.15*completed dribbles
    + 0.3*saves + 3*(pass accuracy - 0.80) (only from 10 passes) - 0.15*fouls - 0.1*times dispossessed or miscontrol,
    clamped to 1..10. A flat formula on purpose: anyone can recompute it from the table.

    ponytail: it ignores minutes played and goals conceded, so a late substitute sits near 6 and a goalkeeper in a
    3-3 is not marked down for the three. Upgrade: per-90 rates once minutes are derived from the substitutions."""
    acc = c["passes_ok"] / c["passes"] if c["passes"] >= 10 else 0.80
    r = (6.0 + 1.2 * c["goals"] + 0.5 * c["xg"] + 0.5 * c["chances"] + 0.2 * c["wins"] + 0.15 * c["dribbles"]
         + 0.3 * c["saves"] + 3 * (acc - 0.80) - 0.15 * c["fouls"] - 0.1 * c["lost"])
    return r


def _verdict(c) -> str:
    acc = round(100 * c["passes_ok"] / c["passes"]) if c["passes"] else 0
    g, k = int(c["goals"]), int(c["chances"])
    if g >= 3:
        v = f"Hat-trick: {g} goals from {int(c['shots'])} shots"
    elif g == 2:
        v = "Two goals" + (f" and {k} chance{'s' if k > 1 else ''} created" if k else "")
    elif g == 1:
        v = "Scored once" + (f" and created {k} chance" + ("s" if k > 1 else "") if k else "")
    elif c["gk"] and c["saves"] >= 3:
        v = f"Busy in goal: {int(c['saves'])} saves"
    elif c["gk"]:
        v = f"Quiet in goal: {int(c['saves'])} saves, {acc}% passing"
    elif k >= 3:
        v = f"Creator: {k} chances made"
    elif c["wins"] >= 7:
        v = f"Ball winner: {int(c['wins'])} tackles, interceptions and recoveries"
    elif c["dribbles"] >= 4:
        v = f"Took people on: {int(c['dribbles'])} dribbles completed"
    elif c["passes"] >= 40 and acc >= 90:
        v = f"Reliable on the ball: {acc}% from {int(c['passes'])} passes"
    elif c["fouls"] >= 3:
        v = f"Combative: {int(c['fouls'])} fouls conceded"
    elif c["touches"] < 15:
        v = f"Short appearance: {int(c['touches'])} touches"
    else:
        v = f"Steady: {int(c['touches'])} touches, {acc}% passing"
    if c["so_goals"]:
        v += "; scored in the shootout"
    if c["so_saves"]:
        v += "; saved a shootout kick"
    return v + "."


def ratings() -> str:
    P = _cards()
    rows = []
    for pid, c in P.items():
        raw = _rate(c)
        rows.append({"player": c["name"], "team": c["team"], "touches": int(c["touches"]), "passes": int(c["passes"]),
                     "pass_pct": round(100 * c["passes_ok"] / c["passes"]) if c["passes"] else None,
                     "shots": int(c["shots"]), "xg": round(c["xg"], 2), "goals": int(c["goals"]),
                     "chances_created": int(c["chances"]), "dribbles": int(c["dribbles"]), "ball_wins": int(c["wins"]),
                     "saves": int(c["saves"]), "fouls": int(c["fouls"]), "shootout_goals": int(c["so_goals"]),
                     "rating": round(min(10.0, max(1.0, raw)), 1), "_raw": raw, "verdict": _verdict(c)})
    rows.sort(key=lambda r: (-r["rating"], -r["_raw"], r["player"]))
    for r in rows:
        del r["_raw"]
    OUT.mkdir(exist_ok=True)
    (OUT / "ratings.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Player ratings", "", "Rating = 6.0 + 1.2 goals + 0.5 xG + 0.5 chances created + 0.2 ball wins + 0.15 completed "
          "dribbles + 0.3 saves + 3 x (pass accuracy - 80%, from 10 passes) - 0.15 fouls - 0.1 times dispossessed or "
          "miscontrol, clamped to 1-10. Ball wins = interceptions, recoveries, won tackles. Chances created = passes "
          "that led to a shot. Regulation and extra time only; shootout kicks are not counted in the numbers.", "",
          "| # | Player | Team | Rating | Touches | Passes (acc) | Shots | xG | Goals | Chances | Dribbles | Ball wins | Fouls "
          "| Verdict |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        pa = f"{r['passes']} ({r['pass_pct']}%)" if r["passes"] else "0"
        md.append(f"| {i} | {r['player']} | {r['team']} | {r['rating']} | {r['touches']} | {pa} | {r['shots']} | {r['xg']} | "
                  f"{r['goals']} | {r['chances_created']} | {r['dribbles']} | {r['ball_wins']} | {r['fouls']} | {r['verdict']} |")
    (OUT / "ratings.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return str(OUT / "ratings.md")


# ---------------------------------------------------------------- 5. asset library search

def search(q: str, kinds=("line", "note", "moment")) -> list[dict]:
    """Case- and accent-insensitive lookup; every word of q must occur in the item (so 'di maria' finds 'Di María')."""
    m, ctl, _ = _plat()
    words = engine.fold(q).split()
    hit = lambda text: all(w in engine.fold(text) for w in words)
    out = []
    if "line" in kinds:
        out += [{"kind": "line", "clock": ctl.clock(l["t"]), "link": link(vt(l["t"])), "text": spoken(l)}
                for l in on_air() if hit(l["text"])]
    if "note" in kinds:
        prep = json.loads((ROOT / "prep_notes.json").read_text(encoding="utf-8"))["notes"]
        out += [{"kind": "note", "clock": None, "link": None, "text": f"[{n['about']}] {n['text']}"}
                for n in prep if hit(n["about"] + " " + n["text"])]
    if "moment" in kinds:
        out += [{"kind": "moment", "clock": h["clock"], "link": h["link"], "text": h["title"]}
                for h in highlights(write=False) if hit(h["title"] + " " + " ".join(l["text"] for l in h["lines"]))]
    return out


# ---------------------------------------------------------------- status for the dashboard

def status() -> dict:
    """Reads samples/ only, never rebuilds."""
    def size(name):
        p = OUT / name
        return p.stat().st_size if p.exists() else 0

    def kb(name):
        return f"{size(name) / 1024:.0f} KB"

    def load(name):
        try:
            return json.loads((OUT / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    vtt = (OUT / "commentary.vtt").read_text(encoding="utf-8") if size("commentary.vtt") else ""
    cues, hl, rt = vtt.count(" --> "), load("highlights.json"), load("ratings.json")
    rep = (OUT / "report.md").read_text(encoding="utf-8") if size("report.md") else ""
    try:
        notes = len(json.loads((ROOT / "prep_notes.json").read_text(encoding="utf-8"))["notes"])
    except (OSError, ValueError, KeyError):
        notes = 0
    built = sum(bool(x) for x in (cues, hl, rep, rt))
    state = "ready" if built == 4 else "partial" if built else "not built"
    summary = (f"{cues} subtitle cues, {len(hl)} highlight clips, a {len(rep.split())}-word report and "
               f"{len(rt)} player ratings from one data set." if built else "No products built yet: run python products.py all.")
    facts = [["Subtitles", f"{cues} cues, {kb('commentary.vtt')}"], ["Highlights", f"{len(hl)} clips, {kb('highlights.json')}"],
             ["Report", f"{len(rep.split())} words + 3 social posts"], ["Ratings", f"{len(rt)} players, {kb('ratings.json')}"],
             ["Search index", f"{cues} lines + {notes} notes + {len(hl)} moments"]]
    return {"key": "products", "title": "Products", "state": state, "summary": summary, "facts": facts[:8]}


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[0] if argv else "all"
    steps = {"subtitles": subtitles, "highlights": lambda: highlights() and str(OUT / "highlights.json"),
             "report": report, "ratings": ratings}
    if cmd == "all":
        for name, f in steps.items():
            print(f"{name}: {f()}")
        print(json.dumps(status(), ensure_ascii=False))
    elif cmd in steps:
        print(steps[cmd]())
    elif cmd == "search" and len(argv) > 1:
        for r in search(" ".join(argv[1:])):
            print(f"[{r['kind']}] {r['clock'] or '-'} {r['link'] or ''}\n    {r['text']}")
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    else:
        print(__doc__.split("\n\n")[2])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
