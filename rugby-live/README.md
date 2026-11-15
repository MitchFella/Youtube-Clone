# Nations Live Centre

Source for the live rugby page (a Claude artifact) and its live feeder.

- `page.src.html` + `base.css` + `data.json` -> `python3 build.py` -> `dist/index.html` (the published page; `data.json` is published beside it).
- `feed.py` polls the rugby365/Opta match-centre feed every 10 s (ESPN's scoreboard is the status cross-check and the fallback score source) and writes one JSON document per match into `live/`. It exits when something changed and prints what to write to the page's database (collection `matches`, doc id = match id); a write there reaches every open page at once.
- `RELAY.md` is the instruction sheet for the relay: a small helper session on a cheap model that runs `feed.py`, copies each `PUSH` line into the database and runs it again. The page cannot fetch the feeds itself and the script cannot write to the database, so one model call per update is unavoidable; keeping that call in a small session is what makes a full day of matches fit in the usage allowance.

```
python3 feed.py --ids 604004                                  # live loop, one match
python3 feed.py --ids 604005,604006 --ver 604005:0,604006:0   # two at once, documents not created yet
python3 feed.py --ids 604003 --once                           # one snapshot
python3 feed.py --sim 5                                       # relay self-test, no network
```
