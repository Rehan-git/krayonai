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

## Tests

```bash
python -m pytest -q
```
