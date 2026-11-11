"""Phase 2 extract: download the raw results for every in-scope contest.

For each contest in etl.races: GET the election page (fresh form_build_id),
check the contest's label, POST the contest change, POST submit with
ward = ALL, and save the AJAX JSON as data/raw/{election_id}/{race_id}.json.
The source documents in etl.races.DOCUMENTS are saved alongside.

This step only downloads. Parsing happens from the cache in the transform
step, so it can be redone without touching the site.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
from pathlib import Path

from .extract import BASE_URL, BlockedError, Crawler
from .probe import (ProbeError, ajax_post, apply_ajax, describe_tables, drupal_settings,
                    form_errors, parse_ajax, parse_form)
from .races import DOCUMENTS, ELECTIONS, TURNOUT, Contest, Election


def results_filename(contest: Contest) -> str:
    return f"{contest.race_id}.json"


def summarize(html: str) -> dict:
    """Shape of a results fragment: wards present, precinct rows, column headers."""
    parts = re.split(r"(<table.*?</table>)", html, flags=re.S | re.I)
    wards, precinct_rows, header, label = [], 0, [], None
    tables = 0
    for p in parts:
        if p[:6].lower() == "<table":
            tables += 1
            t = describe_tables(p, max_rows=1)[0]
            if label is not None:
                wards.append(label)
                precinct_rows += max(t["n_rows"] - 2, 0)        # minus header and Total
                header = header or (t["rows"][0] if t["rows"] else [])
            label = None
        else:
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", p))
            m = re.findall(r"\bWard\s+(\d+)\b", text)
            label = m[-1] if m else None
    return {"tables": tables, "wards": wards, "precinct_rows": precinct_rows, "header": header}


class ResultsFetcher:
    def __init__(self, crawler: Crawler, election_ids: list[str], log=print,
                 elections: dict[str, Election] = ELECTIONS, documents=DOCUMENTS):
        self.c = crawler
        self.election_ids = election_ids
        self.log = log
        self.elections = elections
        self.documents = documents
        self.failed: list[str] = []

    def _save(self, key: str, path: Path, resp, kind: str, found_on: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        self.c._record(key, kind, path, resp, found_on=found_on)

    def fetch_contest(self, e: Election, contest: Contest) -> list[str]:
        page_url = f"{BASE_URL}/elections/results/{e.page_id}"
        key = f"{page_url}?ajax_form=1#contest={contest.value};ward=ALL"
        dest = self.c.out_dir / e.election_id / results_filename(contest)
        if self.c._cached(key):
            return [f"- {contest.race_id}: cached (use --refresh to redo)"]

        resp = self.c._get(page_url)
        if not resp.ok:
            raise ProbeError(f"page GET returned HTTP {resp.status_code} (cf-ray {resp.headers.get('cf-ray', '?')})")
        form = parse_form(resp.text)
        state = drupal_settings(resp.text).get("ajaxPageState", {})
        label = dict(form["selects"].get("contest", [])).get(contest.value)
        if label is None:
            raise ProbeError(f"contest value {contest.value!r} is not on the page")
        if not contest.label_re.match(label):
            raise ProbeError(f"contest {contest.value!r} is labeled {label!r}, expected {contest.label!r}")

        fields = dict(form["fields"])
        fields.update({"contest": contest.value, "ward": "", "precinct": ""})
        for step, trigger, value in (("contest", "contest", None), ("submit", "submit", "Submit")):
            r = ajax_post(self.c, page_url, fields, state, trigger, value)
            if not r.ok:
                raise ProbeError(f"{step} POST returned HTTP {r.status_code} (cf-ray {r.headers.get('cf-ray', '?')})")
            res = parse_ajax(r.text)
            if bad := form_errors(res):
                raise ProbeError(f"{step} POST: form error {bad}")
            apply_ajax(fields, form, res)

        s = summarize(res["html"])
        if s["tables"] < 2 or not s["wards"]:
            raise ProbeError(f"submit returned {s['tables']} table(s) and no ward tables; not saved")
        # Validated: keep the raw submit response exactly as served.
        self._save(key, dest, r, "results", found_on=page_url)
        hdr = s["header"]
        cols = hdr[1:3] if contest.race_id == TURNOUT else [h for h in hdr[2::2]]
        return [f"- {contest.race_id}: {label!r} -> {dest.name}: {len(s['wards'])} wards, "
                f"{s['precinct_rows']} precinct rows, columns {cols}"]

    def fetch_document(self, e: Election, url: str, name: str) -> list[str]:
        dest = self.c.out_dir / e.election_id / name
        if self.c._cached(url):
            return [f"- {name}: cached"]
        if not self.c._allowed(url):
            return [f"- {name}: robots.txt disallows {url}; skipped"]
        r = self.c._get(url)
        ctype = r.headers.get("content-type", "")
        if not r.ok or "pdf" not in ctype.lower():
            self.failed.append(f"{e.election_id}/{name}")
            return [f"- {name}: FAILED, HTTP {r.status_code}, {ctype!r}; not saved"]
        self._save(url, dest, r, "document")
        return [f"- {name}: saved ({len(r.content):,} bytes)"]

    def run(self) -> list[Path]:
        written = []
        for eid in self.election_ids:
            e = self.elections[eid]
            out = [f"# Raw results fetch: {eid} ({e.election_date}), page {e.page_id}",
                   f"Run {dt.datetime.now().isoformat(timespec='seconds')}", "", "## Contests"]
            try:
                for contest in e.contests:
                    self.log(f"[{eid}] {contest.race_id} (contest {contest.value!r})")
                    try:
                        lines = self.fetch_contest(e, contest)
                    except ProbeError as err:
                        self.failed.append(f"{eid}/{contest.race_id}")
                        lines = [f"- {contest.race_id}: FAILED: {err}"]
                    out += lines
                    self.log("  " + lines[-1])
                out += ["", "## Documents"]
                for url, name in self.documents.get(eid, []):
                    out += self.fetch_document(e, url, name)
                    self.log("  " + out[-1])
            except BlockedError as err:
                out += ["", f"STOPPED (blocked): {err}"]
                raise
            finally:
                path = self.c.out_dir / eid / "fetch_summary.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(out) + "\n", encoding="utf-8")
                written.append(path)
        return written
