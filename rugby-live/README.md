# Nations Live Centre

Source for the live rugby page (a Claude artifact) and its live feeder.

- `page.src.html` + `base.css` + `data.json` -> `python3 build.py` -> `dist/index.html` (the published page; `data.json` is published beside it).
- `feed.py` polls the rugby365/Opta match-centre feed and writes one JSON document per match into `live/`. It exits when something changed; each document is then written to the page's database (collection `matches`, doc id = match id), which pushes it to every open page.

```
python3 feed.py --data data.json --out live --ids 604003          # live loop
python3 feed.py --data data.json --out live --ids 604003 --once   # one snapshot
```
