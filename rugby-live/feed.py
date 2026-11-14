#!/usr/bin/env python3
"""Live feeder for the Nations Championship Live+ page.

Polls the rugby365 match-centre feed (Opta data) and writes one compact JSON
document per match into --out. It exits as soon as something worth pushing has
changed, printing which match ids to write to the page's database
(collection "matches", doc id = match id). Run it again after pushing.

  feed.py --data data.json --out live --ids 604003            # live loop
  feed.py --data data.json --out live --ids 604002 --once     # one snapshot
"""
import argparse, hashlib, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 (rugby-live-feeder)", "X-Requested-With": "XMLHttpRequest"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/rugby/17567/scoreboard?dates=%s"
PKEYS = ["points", "tries", "try_assists", "carries", "carries_metres", "clean_breaks",
         "defenders_beaten", "offloads", "passes", "total_kicks", "total_tackles",
         "missed_tackles", "dominant_tackles", "turnovers_won", "turnovers_conceded",
         "penalties_conceded", "lineouts_won"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(url, data=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def minute(v):
    m = re.search(r"\d+", str(v or ""))
    return int(m.group()) if m else 0


def transform(match, j, espn=None):
    """rugby365 payload -> compact overlay document for one match."""
    g, s = j["game"], j["stats"]
    home_name = match["_home_name"]
    flip = g["homeTeam"]["name"].strip().lower() != home_name.lower()
    H, A = ("awayTeam", "homeTeam") if flip else ("homeTeam", "awayTeam")
    side = lambda sd: ({"home": "a", "away": "h"} if flip else {"home": "h", "away": "a"}).get(sd, "")
    pair = lambda h, a: [a, h] if flip else [h, a]

    hp, ap = (g.get("awayPlayers") or [], g.get("homePlayers") or []) if flip else (g.get("homePlayers") or [], g.get("awayPlayers") or [])
    names = {p["id"]: p.get("fname") or p.get("name") for p in hp + ap}
    full = lambda pid, fallback: names.get(pid) or fallback or ""

    ke = s.get("keyEvents") or {}
    worm = (s.get("pointsFlow") or {}).get("wormData") or {}
    cur = int(worm.get("currentGameMinute") or ke.get("currentMinute") or 0)
    labels = " ".join(str(e.get("label", "")) for e in ke.get("eventList", []) if e.get("type") == "interval").lower()
    date_status = str(g.get("dateStatus") or "")
    st = "NS"
    if g.get("finished") or g.get("status") == 2:
        st = "FT"
    elif g.get("status") == 1 or cur > 0:
        st = "LIVE"
    per = 2 if (cur > 40 or "half time" in labels or len(worm.get("second") or []) > 1) else 1
    if st == "LIVE" and re.search(r"half[ -]?time", date_status, re.I):
        st = "HT"
    e_clock = None
    if espn:
        name = ((espn.get("type") or {}).get("name") or "").upper()
        state = (espn.get("type") or {}).get("state")
        if st != "FT":
            if "HALFTIME" in name or "HALF_TIME" in name:
                st = "HT"
            elif state == "in" and st == "NS":
                st = "LIVE"
            elif state == "post" and st != "NS":
                st = "FT"
        if state == "in":
            per = max(per, int(espn.get("period") or 1))
            e_clock = espn.get("clock")
    if st == "HT":
        per, cur = 1, 40
    if st == "LIVE" and isinstance(e_clock, (int, float)) and e_clock > 0:
        em = int(e_clock // 60)
        if abs(em - cur) <= 12:
            cur = max(cur, em)

    # scoring / card / sub events in the page's [min, side, code, player, player2] form
    ev = []
    for e in reversed(ke.get("eventList", [])):
        t = str(e.get("type") or "")
        sd = side(e.get("side"))
        if not sd or t in ("interval", "line"):
            continue
        m = minute(e.get("time"))
        p = full(e.get("playerId"), e.get("player"))
        if "penalty-try" in t or "penalty_try" in t:
            ev.append([m, sd, "PT", ""])
        elif t.startswith("try"):
            ev.append([m, sd, "T", p])
            if e.get("conversionPlayer") or e.get("conversionPlayerId"):
                ev.append([minute(e.get("conversionTime")) or m, sd, "C", full(e.get("conversionPlayerId"), e.get("conversionPlayer"))])
        elif "penalty" in t:
            ev.append([m, sd, "P", p])
        elif "drop" in t:
            ev.append([m, sd, "D", p])
        elif "yellow" in t:
            ev.append([m, sd, "Y", p])
        elif "red" in t:
            ev.append([m, sd, "R", p])
    subs = s.get("substitutions") or {}
    for key in ("homeTeam", "awayTeam"):
        for x in subs.get(key) or []:
            ev.append([int(x.get("minute") or 0), side(x.get("side")), "S", (x.get("subOn") or {}).get("name", ""), (x.get("subOff") or {}).get("name", "")])
    ev.sort(key=lambda e: e[0])
    tries = [sum(1 for e in ev if e[1] == sd and e[2] in ("T", "PT")) for sd in ("h", "a")]

    ts = s.get("teamStats") or {}
    rows = lambda k: [[r["title"]] + pair(r["home"], r["away"]) for r in ts.get(k) or []]
    poss, terr, ent = ts.get("possession") or {}, ts.get("territory") or {}, ts.get("entries") or {}
    rk = s.get("ruckSpeed") or {}
    til = worm.get("time_in_lead") or {}
    groups = []
    live_any = st != "NS"
    if live_any:
        general = [["Possession"] + pair(poss.get("homePercent"), poss.get("awayPercent")),
                   ["Territory"] + pair(terr.get("homeDisplay"), terr.get("awayDisplay")),
                   ["Possession, last 10 min"] + pair(poss.get("homePercent10"), poss.get("awayPercent10")),
                   ["Minutes in lead"] + pair((til.get("home_team") or {}).get("minutes"), (til.get("away_team") or {}).get("minutes")),
                   ["22m entries"] + pair((ent.get("homeTeam") or {}).get("entries"), (ent.get("awayTeam") or {}).get("entries")),
                   ["Points per 22m entry"] + pair((ent.get("homeTeam") or {}).get("avgPoints"), (ent.get("awayTeam") or {}).get("avgPoints"))]
        groups.append(["Control", [r for r in general if r[1] is not None]])
        for title, key in (("Attack", "attack"), ("Defence", "defence"), ("Set piece", "setPlays"),
                           ("Turnovers", "turnovers"), ("Discipline", "penalties"), ("Kicking", "kicks")):
            if ts.get(key):
                groups.append([title, rows(key)])
        if rk.get("homeRucksWon") or rk.get("awayRucksWon"):
            groups.append(["Rucks", [
                ["Rucks won"] + pair(rk.get("homeRucksWon"), rk.get("awayRucksWon")),
                ["Quick ball (0-3s)"] + pair((rk.get("zeroToThree") or {}).get("homeTeam"), (rk.get("zeroToThree") or {}).get("awayTeam")),
                ["Slow ball (6s+)"] + pair((rk.get("sixPlus") or {}).get("homeTeam"), (rk.get("sixPlus") or {}).get("awayTeam"))]])

    flow, last = [], None
    for part in ("first", "second", "third", "fourth"):
        for p in worm.get(part) or []:
            v = pair(p.get("home", 0), p.get("away", 0))
            if v != last:
                flow.append([int(p.get("minute") or 0)] + v)
                last = v

    comm = []
    for c in (s.get("commentary") or {}).get("messages") or []:
        kind = "T" if c.get("try") or c.get("pTry") else "C" if c.get("con") else "P" if c.get("pg") else "D" if c.get("dg") else "Y" if c.get("yc") else "R" if c.get("rc") else ""
        comm.append([c.get("id"), minute(c.get("time")), kind, c.get("comment") or ""])

    pst = {}
    stats_map = (s.get("playerStats") or {}).get("stats") or {}
    for k in PKEYS:
        lst = stats_map.get(k)
        if isinstance(lst, list) and lst:
            pst[k] = [[x.get("name"), "h" if bool(x.get("home")) != flip else "a", x.get("total")] for x in lst if x.get("total")]

    pens = [[int(x.get("minute") or 0), "h" if (x.get("name") or "").lower() == home_name.lower() else "a"]
            for x in (s.get("lastPenalties") or {}).get("lastEvents") or []]
    h2 = s.get("headToHead") or {}
    doc = {
        "id": match["id"], "status": st, "min": cur, "per": per, "at": now_iso(),
        "ref": g.get("referee") or "",
        "raw": {"status": g.get("status"), "dateStatus": date_status, "worm": worm.get("status"),
                "espn": ((espn or {}).get("type") or {}).get("name")},
    }
    if st != "NS":
        doc.update({"hs": g[H].get("score"), "as": g[A].get("score"), "tries": tries, "events": ev,
                    "sg": groups, "flow": flow, "comm": comm, "pst": pst, "pens": pens})
    if hp and ap:
        doc["lineups"] = {"h": [p.get("fname") or p.get("name") for p in hp], "a": [p.get("fname") or p.get("name") for p in ap]}
    if h2.get("hasGames"):
        doc["h2h"] = {"n": h2.get("totalGames"), "w": pair(h2.get("homeWins"), h2.get("awayWins")), "d": h2.get("draws"),
                      "avg": pair(h2.get("homeAveragePoints"), h2.get("awayAveragePoints"))}
    return doc


def sig_major(d):
    ev = d.get("events") or []
    return [d["status"], d.get("hs"), d.get("as"), d.get("per"), sum(1 for e in ev if e[2] != "S")]


def sig_minor(d):
    body = {k: d.get(k) for k in ("sg", "comm", "pens", "lineups", "events", "ref")}
    return hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=10)
    ap.add_argument("--minor-gap", type=float, default=75, help="seconds between pushes for commentary/stats-only changes")
    ap.add_argument("--heartbeat", type=float, default=300)
    ap.add_argument("--max-secs", type=float, default=6600)
    a = ap.parse_args()

    base = json.load(open(a.data))
    matches = {}
    for m in base["matches"]:
        if m["id"] in a.ids.split(",") and m.get("rp"):
            mm = dict(m)
            mm["_url"] = m["rp"].replace("www.rugbypass.com", "rugby365.com")
            mm["_home_name"] = base["teams"][m["home"]]["name"]
            matches[m["id"]] = mm
    if not matches:
        print("NO_MATCHES"); return 2
    os.makedirs(a.out, exist_ok=True)
    state_path = os.path.join(a.out, ".pushed.json")
    pushed = json.load(open(state_path)) if os.path.exists(state_path) else {}
    start, errs = time.time(), 0

    while True:
        espn = {}
        try:
            days = {m["ko"][:10].replace("-", "") for m in matches.values()}
            for d in days:
                for e in http(ESPN % d, timeout=12).get("events", []):
                    espn[e["id"]] = e.get("status")
        except Exception:
            pass
        todo, all_done = [], True
        for mid, m in matches.items():
            try:
                j = http(m["_url"], {"action": "update", "comments": 0, "events": 0})
                doc = transform(m, j, espn.get(mid))
                errs = 0
            except Exception as ex:
                errs += 1
                print("ERR", mid, repr(ex)[:200], file=sys.stderr)
                all_done = False
                continue
            json.dump(doc, open(os.path.join(a.out, mid + ".json"), "w"), ensure_ascii=False, separators=(",", ":"))
            p = pushed.get(mid) or {}
            major, minor, t = sig_major(doc), sig_minor(doc), time.time()
            reason = None
            if a.once or not p:
                reason = "first"
            elif major != p.get("major"):
                reason = "score/status"
            elif minor != p.get("minor") and t - p.get("t", 0) >= a.minor_gap:
                reason = "detail"
            elif doc["status"] in ("LIVE", "HT") and t - p.get("t", 0) >= a.heartbeat:
                reason = "heartbeat"
            if reason:
                todo.append((mid, reason, doc))
                pushed[mid] = {"major": major, "minor": minor, "t": t, "status": doc["status"]}
            if doc["status"] != "FT" or reason:
                all_done = False
        if todo:
            json.dump(pushed, open(state_path, "w"))
            for mid, reason, doc in todo:
                print("PUSH %s reason=%s status=%s %s-%s min=%s per=%s raw=%s" % (mid, reason, doc["status"], doc.get("hs"), doc.get("as"), doc.get("min"), doc.get("per"), json.dumps(doc["raw"])))
            return 0
        if all_done:
            print("DONE all matches finished and pushed"); return 0
        if errs >= 30:
            print("FEED_ERRORS giving up after repeated fetch errors"); return 3
        if time.time() - start > a.max_secs:
            print("TIMEOUT nothing new; run again"); return 0
        live = any((pushed.get(i) or {}).get("status") in ("LIVE", "HT") for i in matches)
        soon = any(abs(time.time() - datetime.fromisoformat(m["ko"].replace("Z", "+00:00")).timestamp()) < 900 for m in matches.values())
        time.sleep(a.poll if (live or soon) else 45)


if __name__ == "__main__":
    sys.exit(main())
