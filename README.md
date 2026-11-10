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
The probe makes about 20 requests, 6s apart:

```bash
python -m etl probe
```

1. Replays the form for 5 cases: 2026g turnout and Governor, each for all
   wards and ward 1, plus 2024g School Board District 1 for all wards. It
   saves every raw response, and any export/download link in the results.
2. Downloads the 2024 general proclamation (`/media/1337`).
3. Saves the district-precinct schedules page and its school board files.

Output goes to `data/raw/_discovery/probe/`, and `probe_summary.md` there
describes what came back. Reruns skip completed steps (`--refresh` redoes
them). Commit and push `data/raw/_discovery/`.

## Tests

```bash
python -m pytest -q
```
