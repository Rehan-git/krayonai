# Precinct results ETL

Pulls precinct-level results from the Chicago Board of Election Commissioners
into the campaign workbook. See the project spec for phases and schemas.

## Phase 1: discovery fetch (run from your own computer)

The board's site blocks cloud/data-center IPs, so run the fetch locally:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m etl fetch
```

What it does:
- Reads robots.txt, waits 4s between requests, sends a descriptive User-Agent.
- Starts at https://www.chicagoelections.gov/elections/results and follows only
  election links matching 2024/2026 General (`--election-filter`), up to
  depth 2 and 150 pages (`--max-depth`, `--max-pages`).
- Saves every HTML page to `data/raw/_discovery/pages/` and every
  XLS/XLSX/CSV/download link to `data/raw/_discovery/files/`.
- Writes `data/raw/_discovery/manifest.csv` (URL, local file, sha256,
  found_on, link text, retrieved_on). Reruns skip anything already cached.
- Stops at the first block/challenge/rate-limit response (exit code 2).

If no election matches the filter it prints the links it saw; rerun with
`--start-url <election page URL>`. Then commit `data/raw/_discovery/` and push.

## Phase 1: probe the results form

Results are served by an AJAX form, not files (see docs/phase1_report.md).
The probe makes about 15 requests, 6s apart:

```bash
python -m etl probe
```

1. Replays the form for 4 cases, all wards: 2026g turnout, 2026g Governor,
   2024g School Board District 1 and 2026g subdistrict 1A. It saves every
   raw response (never error pages, which contain your IP address) and
   retries once after a 502/504.
2. Downloads the 2024 general proclamation (`/media/1337`).
3. Saves the district-precinct schedules page and its school board files.

Output goes to `data/raw/_discovery/probe/`, and `probe_summary.md` there
describes what came back. Reruns skip completed steps (`--refresh` redoes
them). Commit and push `data/raw/_discovery/`.

## Phase 2: download raw results

```bash
python -m etl fetch-results
```

For all 35 contests in `etl/races.py` (2026g: turnout, Governor, SOS,
School Board President, 20 subdistricts; 2024g: turnout, 10 districts):

- Loads the election page and checks the contest's label matches the race
  map exactly. A mismatch is refused, and nothing is saved under that race_id.
- Submits the form with ward = ALL (every precinct, one table per ward) and
  saves the raw response as `data/raw/{election_id}/{race_id}.json`
  (turnout: `turnout.json`).
- Saves the 2024 proclamation PDF and the 2026 school board precinct
  schedule PDF next to them.
- Writes `data/raw/{election_id}/fetch_summary.md` (wards, precinct rows,
  candidate columns per contest) and adds each file to `data/raw/manifest.csv`.

About 3 requests per contest, 6s apart: roughly 11 minutes in total.
Options: `--election 2026g` (repeatable), `--refresh` to re-download, e.g.
when counts are updated or certified. A failed contest doesn't stop the
others; rerun the same command to retry only the failed ones (exit code 1
until all succeed; 2 if blocked). Then commit and push `data/raw/`.

## Tests

```bash
python -m pytest -q
```
