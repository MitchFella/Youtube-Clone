#!/usr/bin/env python3
"""Live feeder for the Nations Live Centre page.

Polls the rugby365 match-centre feed (Opta data) every 10 s, cross-checks the
status against ESPN's scoreboard, and writes one compact JSON document per match
into live/. It exits as soon as something worth pushing has changed and prints
one PUSH line per changed match with exactly what to write to the page's
database, then a NEXT line with the command to run again:

  PUSH collection=matches doc_id=604004 if_version=3 file_path=/.../live/604004.json | LIVE 12-7 38' score/status
  NEXT python3 /.../feed.py --ids 604004

The script cannot write to the page's database itself; a small relay session
does that (RELAY.md). Scores, cards and half/full time exit at once; commentary
and stats-only changes at most every --minor-gap seconds. If rugby365 fails, the
scoreline carries on from ESPN alone.

  feed.py --ids 604004                                  # live loop, one match
  feed.py --ids 604005,604006 --ver 604005:0,604006:0   # two at once, new documents
  feed.py --ids 604003 --once                           # one snapshot
  feed.py --sim 5                                       # relay self-test, no network
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


def espn_only(match, ev, prev):
    """Second source. ESPN's scoreboard carries score, clock and status on its own,
    so when the rugby365 feed fails the scoreline keeps moving. Timeline, stats and
    commentary are kept from the last good rugby365 document."""
    try:
        by = {c.get("homeAway"): c for c in ev["competitions"][0].get("competitors", [])}
        home, away = by["home"], by["away"]
    except Exception:
        return None
    nm = lambda c: (c["team"].get("displayName") or "").strip().lower()
    if nm(home) != match["_home_name"].lower() and nm(away) == match["_home_name"].lower():
        home, away = away, home
    st = ev.get("status") or {}
    typ = st.get("type") or {}
    name, state = (typ.get("name") or "").upper(), typ.get("state")
    status = "NS"
    if state == "post":
        status = "FT"
    elif "HALFTIME" in name or "HALF_TIME" in name:
        status = "HT"
    elif state == "in":
        status = "LIVE"
    doc = dict(prev or {"id": match["id"], "min": 0, "per": 1, "events": [], "sg": [], "flow": [],
                        "comm": [], "pst": {}, "pens": [], "lineups": {"h": [], "a": []}})
    doc["status"], doc["at"] = status, now_iso()
    if status in ("LIVE", "HT"):
        doc["per"] = max(int(st.get("period") or 1), 1)
        doc["min"] = minute(st.get("displayClock")) or int(float(st.get("clock") or 0) // 60) or doc.get("min") or 0
    elif status == "FT":
        doc["per"], doc["min"] = 2, max(80, doc.get("min") or 0)
    num = lambda v: int(v) if str(v if v is not None else "").strip().isdigit() else None
    hs, as_ = num(home.get("score")), num(away.get("score"))
    if status != "NS" and hs is not None and as_ is not None:
        doc["hs"], doc["as"] = hs, as_
    raw = dict(doc.get("raw") or {})
    raw.update({"src": "espn-only", "espn": typ.get("name")})
    doc["raw"] = raw
    return doc


def dump(obj, path, **kw):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


def push_line(coll, mid, ver, path, note):
    """One line the relay copies into an ArtifactData set. ver: None unknown, 0 no document yet."""
    v = "unknown" if ver is None else ("none" if ver == 0 else str(ver))
    print("PUSH collection=%s doc_id=%s if_version=%s file_path=%s | %s" % (coll, mid, v, path, note))


def simulate(a, st, save, again):
    """Relay self-test: N fake updates to collection 'selftest', no network. The third one
    carries a deliberately wrong version so the version-conflict recovery gets exercised."""
    n = st.get("sim", 0)
    if n >= a.sim:
        print("DONE self-test finished after %d updates" % n)
        return 0
    time.sleep(8)
    n += 1
    path = os.path.join(a.out, "selftest-t1.json")
    dump({"id": "t1", "n": n, "of": a.sim, "status": "LIVE", "hs": 7 * n, "as": 3 * (n // 2), "min": 9 * n,
          "at": now_iso(), "note": "relay self-test"}, path)
    v = st["ver"].get("t1", 0)
    push_line("selftest", "t1", v + 5 if n == 3 else v, path, "self-test update %d of %d" % (n, a.sim))
    st["ver"]["t1"] = (v + 5 if n == 3 else v) + 1
    st["sim"] = n
    save()
    print(again)
    return 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(here, "data.json"))
    ap.add_argument("--out", default=os.path.join(here, "live"))
    ap.add_argument("--ids", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=10)
    ap.add_argument("--minor-gap", type=float, default=90,
                    help="least seconds between exits caused only by commentary/stats changes (all matches together)")
    ap.add_argument("--heartbeat", type=float, default=300)
    ap.add_argument("--max-secs", type=float, default=540)
    ap.add_argument("--ver", default="",
                    help="document versions now in the database, e.g. 604004:3,604005:0 (0 = no document yet)")
    ap.add_argument("--init-ver", default="",
                    help="like --ver, but only for documents this checkout has no version for yet (safe to repeat on a restart)")
    ap.add_argument("--sim", type=int, default=0, help="relay self-test with N fake updates, no network")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    state_path = os.path.join(a.out, ".state.json")
    st = json.load(open(state_path)) if os.path.exists(state_path) else {}
    st.setdefault("m", {})
    st.setdefault("ver", {})
    for arg, force in ((a.init_ver, False), (a.ver, True)):
        for part in filter(None, arg.split(",")):
            k, _, v = part.partition(":")
            if force or k.strip() not in st["ver"]:
                st["ver"][k.strip()] = int(v)
    save = lambda: dump(st, state_path)
    again = "NEXT python3 %s%s%s%s" % (os.path.abspath(__file__), " --ids " + a.ids if a.ids else "",
                                       " --minor-gap %g" % a.minor_gap if a.minor_gap != 90 else "",
                                       " --sim %d" % a.sim if a.sim else "")
    if a.sim:
        return simulate(a, st, save, again)

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
    days = {m["ko"][:10].replace("-", "") for m in matches.values()}
    pm, ver = st["m"], st["ver"]
    start, espn, espn_t, errs, blank = time.time(), {}, 0, {}, 0
    ko = {i: datetime.fromisoformat(m["ko"].replace("Z", "+00:00")).timestamp() for i, m in matches.items()}
    docs, due = {}, {i: 0 for i in matches}

    while True:
        now = time.time()
        if now - espn_t >= 30:  # ESPN scoreboard: status cross-check and second score source
            try:
                fresh = {}
                for d in days:
                    for e in http(ESPN % d, timeout=12).get("events", []):
                        fresh[e["id"]] = e
                espn, espn_t = fresh, now
            except Exception:
                pass
        tried = got = 0
        for mid, m in matches.items():
            if now < due[mid]:
                continue
            tried += 1
            path = os.path.join(a.out, mid + ".json")
            ev = espn.get(mid)
            try:
                doc = transform(m, http(m["_url"], {"action": "update", "comments": 0, "events": 0}),
                                (ev or {}).get("status"))
                errs[mid] = 0
            except Exception as ex:
                errs[mid] = errs.get(mid, 0) + 1
                if errs[mid] in (1, 10):
                    print("ERR", mid, repr(ex)[:160], file=sys.stderr)
                doc = None
                if ev and errs[mid] >= 2:
                    try:
                        prev = json.load(open(path)) if os.path.exists(path) else None
                    except Exception:
                        prev = None
                    doc = espn_only(m, ev, prev)
                if doc is None:
                    continue
            dump(doc, path, ensure_ascii=False, separators=(",", ":"))
            docs[mid] = doc
            got += 1
            # Poll only what can change soon: finished-and-pushed matches and
            # matches far from kick-off are looked at every few minutes.
            if doc["status"] == "FT" and (pm.get(mid) or {}).get("status") == "FT":
                due[mid] = now + 300
            elif doc["status"] == "NS" and ko[mid] - now > 1200:
                due[mid] = now + 120
        blank = blank + 1 if (tried and not got) else 0

        now = time.time()
        gap_ok = now - st.get("t", 0) >= a.minor_gap
        pend, go, all_done = [], False, len(docs) == len(matches)
        for mid, doc in docs.items():
            p = pm.get(mid) or {}
            major, minor = sig_major(doc), sig_minor(doc)
            reason = None
            if a.once or not p:
                reason = "first"
            elif major != p.get("major"):
                reason = "score/status"
            elif minor != p.get("minor"):
                reason = "detail"
            elif doc["status"] in ("LIVE", "HT") and now - p.get("t", 0) >= a.heartbeat:
                reason = "heartbeat"
            if reason:
                pend.append((mid, reason, doc, major, minor))
                go = go or reason != "detail" or gap_ok
            if doc["status"] != "FT" or reason:
                all_done = False
        if go:
            for mid, reason, doc, major, minor in pend:
                pm[mid] = {"major": major, "minor": minor, "t": now, "status": doc["status"]}
                push_line("matches", mid, ver.get(mid), os.path.join(a.out, mid + ".json"),
                          "%s %s-%s %s' %s%s" % (doc["status"], doc.get("hs"), doc.get("as"), doc.get("min"), reason,
                                                 " (espn only)" if (doc.get("raw") or {}).get("src") == "espn-only" else ""))
                if ver.get(mid) is not None:
                    ver[mid] += 1
            st["t"] = now
            save()
            if not a.once:
                print(again)
            return 0
        if all_done:
            print("DONE all matches finished and pushed"); return 0
        if now > max(ko.values()) + 4 * 3600:
            print("DONE match window is over (last kick-off was more than 4 hours ago)"); return 0
        if blank >= 30:
            print("FEED_ERRORS no data from either source for 30 polls"); print(again); return 3
        if time.time() - start > a.max_secs:
            print("TIMEOUT nothing new"); print(again); return 0
        live = any((pm.get(i) or {}).get("status") in ("LIVE", "HT") for i in matches)
        soon = any(abs(time.time() - datetime.fromisoformat(m["ko"].replace("Z", "+00:00")).timestamp()) < 900 for m in matches.values())
        time.sleep(a.poll if (live or soon) else 45)


if __name__ == "__main__":
    sys.exit(main())
