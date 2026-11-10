# Phase 1 report: chicagoelections.gov results source

Based on the discovery fetch of 2026-11-10 (commit bff7b60): the results
index plus the election pages for 2024g and 2026g. These pages contain no
results data, so a few questions stay open until the probe in "Next fetch"
runs.

## Access
- The site is behind Cloudflare and blocks cloud/data-center IPs, so all
  fetching has to run from a normal connection (your computer).
- robots.txt is a standard Drupal file and does not disallow any results
  paths, including the POST endpoint below.

## Where the precinct data lives
There are no static per-contest files (no XLS/CSV links anywhere on these
pages), which is why the fetch found 0 files. Results come from a Drupal
form on each election page:

| Element | Value |
|---|---|
| Election page | `/elections/results/{page_id}`: **2026g = 42**, **2024g = 41** (not in date order; the 2026 primaries are 76-78) |
| Form | `election_results_form`, hidden fields `election_id`, `form_build_id` (changes on every page load), `form_id` |
| Dropdowns | `contest`, `ward`, `precinct` (ward and precinct show only "ALL" until a contest is chosen) |
| AJAX endpoint | `POST /elections/results/{page_id}?ajax_form=1` |
| Contest change | `_triggering_element_name=contest` → `::ajaxRebuildPage` (fills the ward/precinct lists) |
| Submit | `_triggering_element_name=submit`, `_triggering_element_value=Submit` → `::displayElectionResults` |
| Response | Drupal AJAX command JSON with an HTML results fragment (expected; not yet seen) |

**Turnout**: contest value `''` (blank) = "Registered Voters/Ballots/Turnout",
in both elections. So registered voters and ballots cast are published, and
probably per precinct.

**Ward summary (QA 7)**: probably the same form with ward = ALL; to be
confirmed by the probe.

**Citywide totals (QA 8)**:
- 2024g: certified *Proclamation of Official Results (November 5, 2024).pdf*
  at `/media/1337`.
- 2026g: no proclamation yet (results are unofficial). Until one is posted,
  the only citywide totals are the results form's own totals, so check 8 for
  2026g is close to a self-check.

## Race mapping (contest values are per election, not stable across elections)

| race_id | page | contest value | Source label |
|---|---|---|---|
| (turnout) | 42 | `''` | Registered Voters/Ballots/Turnout |
| 2026g-GOV | 42 | 12 | Governor & Lieutenant Governor, Illinois |
| 2026g-SOS | 42 | 14 | Secretary of State, State of Illinois |
| 2026g-SBPRES | 42 | 121 | President, Board of Education |
| 2026g-SB01A … 2026g-SB10B | 42 | 122 … 141 (1A=122, 1B=123, … 10B=141, in order) | Member, Board of Education, Subdistrict 1A … 10B |
| (turnout) | 41 | `''` | Registered Voters/Ballots/Turnout |
| 2024g-SB01 … 2024g-SB10 | 41 | 110 … 119 | Member of the Chicago Board of Education, 1st … 10th District |

All 34 race_ids map one-to-one; no contest is ambiguous. Things that complicate the mapping:
- Contest values get reused across elections (110 is the 2024 SB 1st
  District but a 2026 judgeship), so the mapping must be keyed by
  (page_id, contest value), and each run should check that the label still
  matches before parsing.
- The Governor contest is a joint ticket, so candidate_name will be a
  Governor/Lt. Governor pair as the source shows it.
- School board races cover only part of the city. QA 9 (coverage) needs an
  independent list of precincts per sub-district/district. The site links
  `/districts-maps/district-precinct-schedules`, which is likely the source;
  not fetched yet.
- The 2026 contest list also includes judicial retention names (142-202) and
  referenda, some precinct-specific (labels like "W07 P14"). That is the
  first sign of the source's precinct labeling; all of these are out of
  scope.
- Neither page shows an "Unofficial" or "last updated" label, so
  result_status comes from the CLI flag as planned. The probe will check
  whether the results fragment carries a timestamp.

## Still unknown (needs the probe)
1. Whether ward = ALL returns precinct rows or only ward totals (this decides
   between about 70 requests and about 1,800).
2. Precinct/ward labels in the results table.
3. Whether write-ins are reported per precinct.
4. Whether the results fragment links a download (XLS/CSV) of its own.
5. Whether a session cookie is needed for the POST to work.

## Next fetch
1. **Probe, about 10 requests**: for page 42, contest `''` (turnout) and
   contest 12 (Governor), with ward = ALL and with ward = 1. Each case is a
   GET of the election page (for a fresh `form_build_id`), then the
   contest-change POST, then the submit POST. Save every raw response under
   `data/raw/_discovery/probe/`.
2. **Fetch the 2024 proclamation PDF** (`/media/1337`), and add `/media` and
   `.pdf` to the fetcher's scope.
3. **Fetch the district-precinct schedules page** for QA 9.
4. After reviewing the probe: the full contest loop for the 35 contests
   above (24 for 2026g, 11 for 2024g), cached under
   `data/raw/{election_id}/`, at the same 4s delay.
