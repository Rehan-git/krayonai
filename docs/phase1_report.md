# Phase 1 report: chicagoelections.gov results source

Based on the discovery fetch (commit bff7b60) and the first probe (commit
cff3805, run 2026-11-10). Every figure checked below was compared within
the board's own published data; nothing was estimated.

## Access
- The site is behind Cloudflare and blocks cloud/data-center IPs, so all
  fetching has to run from a normal connection (your computer).
- robots.txt is a standard Drupal file and disallows no results paths.
- During the probe the site returned **502 Bad gateway** twice (the 2024
  page GET and a ward-level POST). The fetcher now retries once after 30s
  and never saves error pages, because Cloudflare error pages print the
  visitor's IP address.

## Where the precinct data lives
There are no static result files. Each contest comes from one AJAX request
on its election page:

| Element | Value |
|---|---|
| Election page | `/elections/results/{page_id}`: **2026g = 42**, **2024g = 41** |
| Endpoint | `POST /elections/results/{page_id}?ajax_form=1` |
| Steps | GET the page (for `form_build_id`), POST a contest change, POST submit with ward = ALL |
| Response | Drupal AJAX JSON; an `openDialog` command holds the results HTML (about 1.3 MB for Governor) |

**Ward = ALL returns every precinct**, so the full run needs about 3
requests per contest (about 105 for the 35 contests; roughly 12 minutes at
6s spacing). Per-ward requests are not needed.

### Results HTML layout
- Table 0: citywide totals.
- Then 50 tables, each after a "Ward N" heading (N = 1 … 50, no padding).
- Each ward table has one row per precinct, numbered 1 … n within the ward
  (sequential in every ward), plus a final "Total" row.
- Turnout contest columns: Precinct, Registered Voters, Ballots Cast,
  Turnout.
- Candidate contest columns: Precinct, "Total Voters" (actually the total
  votes in the contest), then a vote count and a % column for each
  candidate.
- Candidate names are shown as the ballot shows them. Governor is a ticket
  ("JB Pritzker & Christian Mitchell").
- **No party labels** in the 2026 results.
- **No "unofficial" or "last updated" label** anywhere, so result_status
  comes from the CLI flag as planned.
- The "Download Data in Excel." link returned an HTML page saying "Cannot
  generate xls download." It is not used.

### Checks on the probe data (2026g)
| Check | Result |
|---|---|
| Precincts | 1,291 in 50 wards (15 to 40 per ward); identical list in turnout and Governor |
| Turnout: precincts → ward totals → citywide | exact match (1,570,238 registered, 740,216 ballots) |
| Registered voters > 0 and ballots ≤ registered, every precinct | pass |
| Governor: each precinct's candidate votes sum to its "Total Voters" | pass |
| Governor: precincts → ward totals → citywide, all 11 candidates | exact match (732,964 votes) |
| Governor votes ≤ ballots cast, every precinct | pass |

## Write-ins
Named write-in candidates are reported **per precinct**, labeled "(Write-in)".
Governor has 3 ballot candidates and 8 write-ins, most with 0 votes. So
under the spec they become candidate rows, flagged in QA.

Row volume: Governor alone is **14,201 rows** (1,291 × 11), or **4,003** if
write-in rows with 0 votes are left out. Across all races this will exceed
the 25,000 rows `Precinct_Results` has. **Decision needed** (see below).

## 2024 proclamation (`/media/1337`, 21-page PDF, certified)
"Tabulated Statement of the Returns and Proclamation of the Results", Nov
5, 2024. It contains:
- Ballots cast by district: citywide 1,018,359; each ward; each Board of
  Education district (e.g. District 1: 117,241).
- Precincts per district: city 1,291; ward counts; Board of Education
  District 1 … 10 = 151, 125, 130, 135, 141, 145, 104, 153, 172, 153.
- Candidate totals per contest, with **party prefixes** ("DEM - ",
  "NON - "). It is a two-column page layout, so the parser must split the
  columns.

That gives QA 8 its citywide totals for 2024g, QA 9 its expected precinct
count per district, and candidates_draft its party labels for 2024g.

## Split precincts (affects school board races)
The district precinct counts add up to 1,409 against 1,291 precincts,
because some precincts are split between districts. The 2026 schedule PDF
("District Schedule Chicago Elected School Board 2026", current as of
2026-02-09) lists each subdistrict's precincts by ward and marks splits
"(pt)". For example, 1A has 72 whole and 7 split precincts.

Consequences:
- A precinct can have results in two school board races. result_id stays
  unique, since it includes race_id.
- QA 5 (votes ≤ ballots) still holds per race, because each part's votes
  are part of the precinct's ballots.
- QA 9 coverage for 2026g uses the schedule (whole and part precincts).
  For 2024g it uses the proclamation's precinct counts. A 2024 precinct
  list per district isn't on the schedules page (only the 2026 school
  board schedule is linked).
- Still unseen: whether the results label split precincts ("17" or
  "17(pt)"). The next probe includes 2026 subdistrict 1A to find out.

## Race mapping (contest values per election page)

| race_id | page | contest value | Source label |
|---|---|---|---|
| (turnout) | 42 | `''` | Registered Voters/Ballots/Turnout |
| 2026g-GOV | 42 | 12 | Governor & Lieutenant Governor, Illinois |
| 2026g-SOS | 42 | 14 | Secretary of State, State of Illinois |
| 2026g-SBPRES | 42 | 121 | President, Board of Education |
| 2026g-SB01A … 2026g-SB10B | 42 | 122 … 141 (1A=122, 1B=123, … 10B=141) | Member, Board of Education, Subdistrict 1A … 10B |
| (turnout) | 41 | `''` | Registered Voters/Ballots/Turnout |
| 2024g-SB01 … 2024g-SB10 | 41 | 110 … 119 | Member of the Chicago Board of Education, 1st … 10th District |

Contest values are reused across elections, so the mapping is keyed by
(page, contest), and each run checks the label before parsing.

## Still open
1. 2024g results (the page returned a 502) and how split precincts are
   labeled: the rerun probe covers 2024g SB 1st District and 2026g SB 1A.
2. Party labels for 2026g Governor and SOS candidates: not in the results.
   See decisions.
3. Ballot order for candidate_id NN: the results list candidates in a fixed
   column order, but nothing on the page says it is ballot order.

## Decisions needed
- **Write-in rows**: (a) keep all, including 0-vote rows (about 14k rows
  for Governor alone; the Sheet must grow), or (b) keep write-in rows only
  where votes > 0, with ballot candidates kept at every precinct even when
  0. Recommended: (b). It loses nothing, since a missing write-in row means
  0, and QA can confirm the totals.
- **2026g party labels**: leave party blank for GOV/SOS until the 2026
  proclamation is certified, or name another official source.
- **Ballot order**: accept the board's column order as NN, or provide the
  specimen ballot.
