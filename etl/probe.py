"""Phase 1 probe: learn the results form's output format with a few requests.

Three steps (see docs/phase1_report.md, "Next fetch"):
  1. Results form: for a handful of (election page, contest, ward) cases,
     replay what the browser does (GET the page, AJAX contest change,
     AJAX ward change if a ward is picked, AJAX submit) and save every raw
     response. Any download/export link in the results gets fetched too.
  2. The certified 2024 general proclamation (/media/1337).
  3. The district-precinct schedules page, plus its school-board files.

Everything is saved under <out>/probe/ and listed in <out>/manifest.csv.
probe/probe_summary.md describes what came back, for Phase 1 review. Nothing
here parses vote counts.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .extract import BASE_URL, Crawler, cache_name, extract_links, filename_from_response, is_file_link


@dataclass(frozen=True)
class Case:
    label: str
    page_id: str          # election page: 42 = 2026g, 41 = 2024g
    contest: str          # '' = Registered Voters/Ballots/Turnout
    ward: str             # '' = ALL; otherwise the ward option's visible text, e.g. '1'


# Ward = ALL already returns every precinct (one table per ward), so per-ward
# cases were dropped after the first probe. The SB cases show how split
# precincts ("17(pt)" in the district schedule) appear in results.
DEFAULT_CASES = [
    Case("2026g_turnout_allwards", "42", "", ""),
    Case("2026g_gov_allwards", "42", "12", ""),
    Case("2024g_sb01_allwards", "41", "110", ""),
    Case("2026g_sb01a_allwards", "42", "122", ""),
]
PROCLAMATION_2024G = BASE_URL + "/media/1337"
SCHEDULES_URL = BASE_URL + "/districts-maps/district-precinct-schedules"
SCHEDULE_FILTER = r"school|education"
EXPORT_WORDS = re.compile(r"download|export|excel|xls|csv|print", re.I)
FORM_ERROR_MARKERS = ("problem with your form submission", "please try again",
                      "an illegal choice", "form has become outdated")


class ProbeError(RuntimeError):
    pass


# ---------- HTML/AJAX helpers (pure functions, unit-tested) ----------

def parse_form(html: str, form_id: str = "election-results-form") -> dict:
    """Return {'action', 'fields': {name: value}, 'selects': {name: [(value, text)]}}."""
    m = re.search(r'<form[^>]*id="%s"[^>]*>(.*?)</form>' % re.escape(form_id), html, re.S)
    if not m:
        raise ProbeError(f"form #{form_id} not found")
    tag = re.match(r"<form[^>]*>", m.group(0)).group(0)
    action = (re.search(r'action="([^"]*)"', tag) or [None, ""])[1]
    body = m.group(1)
    fields: dict[str, str] = {}
    for inp in re.findall(r"<input[^>]*>", body):
        name = re.search(r'name="([^"]*)"', inp)
        itype = (re.search(r'type="([^"]*)"', inp) or [None, "text"])[1]
        if not name or itype in ("submit", "button", "image"):
            continue
        val = re.search(r'value="([^"]*)"', inp)
        fields[name.group(1)] = htmllib.unescape(val.group(1)) if val else ""
    selects: dict[str, list[tuple[str, str]]] = {}
    for name, inner in re.findall(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', body, re.S):
        opts = [(htmllib.unescape(v), " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", t)).split()))
                for v, t in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', inner, re.S)]
        selects[name] = opts
        sel = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', inner)
        fields[name] = htmllib.unescape(sel.group(1)) if sel else (opts[0][0] if opts else "")
    return {"action": action, "fields": fields, "selects": selects}


def drupal_settings(html: str) -> dict:
    m = re.search(r'data-drupal-selector="drupal-settings-json"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else {}


def parse_ajax(text: str) -> dict:
    """Digest a Drupal AJAX command list: new build id, inserted HTML, messages."""
    try:
        commands = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProbeError(f"AJAX response is not JSON: {text[:200]!r}") from e
    new_build, parts, messages = None, [], []
    for c in commands if isinstance(commands, list) else []:
        cmd = c.get("command")
        if cmd == "update_build_id":
            new_build = c.get("new")
        elif cmd in ("insert", "replace", "html", "openDialog") and isinstance(c.get("data"), str):
            parts.append(c["data"])
        elif cmd == "message":
            messages.append(str(c.get("message", "")))
    return {"build_id": new_build, "html": "\n".join(parts), "messages": messages,
            "commands": [c.get("command") for c in commands] if isinstance(commands, list) else []}


def describe_tables(html: str, max_rows: int = 6) -> list[dict]:
    out = []
    for tbl in re.findall(r"<table.*?</table>", html, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tbl, re.S | re.I):
            cells = [" ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", c)).split())
                     for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
            rows.append(cells)
        caption = re.search(r"<caption[^>]*>(.*?)</caption>", tbl, re.S | re.I)
        out.append({"caption": " ".join(re.sub(r"<[^>]+>", " ", caption.group(1)).split()) if caption else "",
                    "n_rows": len(rows), "rows": rows[:max_rows], "last_row": rows[-1] if rows else []})
    return out


def export_candidates(html: str, page_url: str) -> list[tuple[str, str]]:
    links = [(u, t) for u, t in extract_links(html, page_url) if is_file_link(u) or EXPORT_WORDS.search(t)]
    buttons = [(b, "") for b in re.findall(r"<(?:button|input)[^>]*(?:download|export|excel|csv)[^>]*>", html, re.I)]
    return links + buttons


# ---------- the probe ----------

class Prober:
    def __init__(self, crawler: Crawler, cases=DEFAULT_CASES, schedule_filter=SCHEDULE_FILTER, log=print):
        self.c = crawler
        self.cases = cases
        self.schedule_filter = re.compile(schedule_filter, re.I)
        self.log = log
        self.dir = crawler.out_dir / "probe"
        self.summary: list[str] = []

    # -- saving --
    def _save(self, key: str, name: str, resp, kind: str, found_on: str = "") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_bytes(resp.content)
        self.c._record(key, kind, path, resp, found_on=found_on)
        return path

    def _cached(self, key: str) -> Path | None:
        return self.c._cached(key)

    # -- one form case --
    def run_case(self, case: Case) -> None:
        page_url = f"{BASE_URL}/elections/results/{case.page_id}"
        ajax_url = f"{page_url}?ajax_form=1"
        out = [f"## {case.label}", f"page {page_url}, contest {case.contest!r}, ward {case.ward or 'ALL'}"]
        done_key = f"{ajax_url}#{case.label}/submit"
        if self._cached(done_key):
            out.append("cached from an earlier run (use --refresh to redo)")
            self.summary += out + [""]
            self.log(f"[probe] {case.label}: cached")
            return

        self.log(f"[probe] {case.label}: GET {page_url}")
        resp = self.c._get(page_url)
        if not resp.ok:
            # Error pages are not saved: Cloudflare's include the visitor's IP address.
            raise ProbeError(f"page GET returned HTTP {resp.status_code} (cf-ray {resp.headers.get('cf-ray', '?')})")
        self._save(f"{page_url}#{case.label}/page", f"{case.label}__0_page.html", resp, "probe-page")
        page = resp.text
        form = parse_form(page)
        settings = drupal_settings(page)
        state = settings.get("ajaxPageState", {})
        fields = dict(form["fields"])
        contests = dict(form["selects"].get("contest", []))
        if case.contest not in contests:
            raise ProbeError(f"{case.label}: contest {case.contest!r} not in the page's contest list")
        out.append(f"contest label: {contests[case.contest]!r}")

        def ajax(step: str, trigger: str, trigger_value: str | None = None) -> dict:
            data = {k: v for k, v in fields.items()}
            data.update({"_triggering_element_name": trigger, "_drupal_ajax": "1",
                         "ajax_page_state[theme]": state.get("theme", ""),
                         "ajax_page_state[theme_token]": state.get("theme_token", ""),
                         "ajax_page_state[libraries]": state.get("libraries", "")})
            if trigger_value is not None:
                data["_triggering_element_value"] = trigger_value
            self.log(f"[probe] {case.label}: POST {step}")
            r = self.c._request("POST", ajax_url, data=data, headers={
                "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": page_url, "Origin": BASE_URL})
            if not r.ok:
                raise ProbeError(f"{step} POST returned HTTP {r.status_code} (cf-ray {r.headers.get('cf-ray', '?')})")
            n = {"contest": 1, "ward": 2, "submit": 3}[step]
            self._save(f"{ajax_url}#{case.label}/{step}", f"{case.label}__{n}_{step}.json", r, "probe-ajax",
                       found_on=page_url)
            res = parse_ajax(r.text)
            low = (res["html"] + " ".join(res["messages"])).lower()
            bad = [m for m in FORM_ERROR_MARKERS if m in low]
            out.append(f"- {step}: HTTP {r.status_code}, commands {res['commands']}"
                       + (f", FORM ERROR markers {bad}" if bad else ""))
            if res["build_id"]:
                fields["form_build_id"] = res["build_id"]
            if res["html"]:
                # Rebuilt form or just a wrapper fragment: read whatever inputs/selects came back.
                frag = res["html"] if "election-results-form" in res["html"] else (
                    f'<form id="election-results-form">{res["html"]}</form>')
                try:
                    newer = parse_form(frag)
                except ProbeError:
                    newer = parse_form(f'<form id="election-results-form">{res["html"]}</form>')
                for k, v in newer["fields"].items():
                    if k in ("form_build_id", "form_id", "election_id", "homepage") or k not in fields:
                        fields[k] = v
                form["selects"].update(newer["selects"])
            return res

        fields["contest"] = case.contest
        fields["ward"], fields["precinct"] = "", ""
        ajax("contest", "contest")
        wards = form["selects"].get("ward", [])
        out.append(f"ward options after contest change: {len(wards)} (first: {wards[:4]})")

        if case.ward:
            match = [v for v, t in wards if t == case.ward or v == case.ward
                     or t.lstrip("0") == case.ward.lstrip("0") or v.lstrip("0") == case.ward.lstrip("0")]
            if not match:
                raise ProbeError(f"{case.label}: ward {case.ward!r} not in ward options {wards[:10]}")
            fields["ward"] = match[0]
            ajax("ward", "ward")
            precincts = form["selects"].get("precinct", [])
            out.append(f"precinct options after ward change: {len(precincts)} (first: {precincts[:5]})")

        res = ajax("submit", "submit", "Submit")
        tables = describe_tables(res["html"])
        out.append(f"results: {len(tables)} table(s)")
        for i, t in enumerate(tables):
            out.append(f"  table {i}: {t['n_rows']} rows, caption {t['caption']!r}")
            for row in t["rows"]:
                out.append(f"    {row}")
            out.append(f"    ... last row: {t['last_row']}")
        text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", res["html"])).split())
        for kw in ("unofficial", "official", "updated", "as of", "write-in", "write in", "precincts"):
            for hit in re.findall(r".{0,60}%s.{0,60}" % re.escape(kw), text, re.I)[:2]:
                out.append(f"  [{kw}] {hit}")

        exports = export_candidates(res["html"], page_url)
        out.append(f"export/download candidates: {len(exports)}")
        # Listed, not fetched: the first probe's "Download Data in Excel." link
        # returned "Cannot generate xls download.", and the AJAX HTML is complete.
        for u, t in exports[:10]:
            out.append(f"  {t!r} {u}")
        self.summary += out + [""]

    # -- documents --
    def fetch_document(self, label: str, url: str, want: re.Pattern | None = None, max_files: int = 20) -> None:
        out = [f"## {label}", url]
        if self._cached(url):
            out.append("cached from an earlier run")
            self.summary += out + [""]
            return
        if not self.c._allowed(url):
            out.append("robots.txt disallows; skipped")
            self.summary += out + [""]
            return
        self.log(f"[probe] {label}: GET {url}")
        r = self.c._get(url)
        ctype = r.headers.get("content-type", "")
        if not r.ok:
            out.append(f"HTTP {r.status_code}; skipped")
            self.summary += out + [""]
            return
        if "html" not in ctype.lower():
            name = f"{label}__{filename_from_response(url, r)}"
            self._save(url, name, r, "probe-doc")
            out.append(f"saved {name} ({len(r.content):,} bytes, {ctype})")
            self.summary += out + [""]
            return
        self._save(url, f"{label}__{cache_name(url, '.html')}", r, "probe-page")
        links = [(u, t) for u, t in extract_links(r.text, url)
                 if is_file_link(u) or u.lower().endswith(".pdf") or "/files/" in u or "/media/" in u]
        if want is None:   # a Drupal media page: take its file link(s)
            links = [(u, t) for u, t in links if "/files/" in u or u.lower().endswith(".pdf")]
        out.append(f"HTML page; {len(links)} file-like links")
        picked = [(u, t) for u, t in links if want is None or want.search(t) or want.search(u)]
        for u, t in links:
            out.append(f"  {'*' if (u, t) in picked else ' '} {t[:70]!r} {u}")
        for u, t in picked[:max_files]:
            if self._cached(u) or not self.c._allowed(u):
                continue
            fr = self.c._get(u)
            if fr.ok:
                name = f"{label}__{filename_from_response(u, fr)}"
                self._save(u, name, fr, "probe-doc", found_on=url)
                out.append(f"  saved {name} ({len(fr.content):,} bytes)")
        self.summary += out + [""]

    def run(self) -> Path:
        self.summary = [f"# Probe summary ({dt.date.today().isoformat()})", ""]
        for case in self.cases:
            try:
                self.run_case(case)
            except ProbeError as e:
                self.summary += [f"## {case.label}", f"STOPPED: {e}", ""]
                self.log(f"[probe] {case.label}: {e}")
        self.fetch_document("proclamation_2024g", PROCLAMATION_2024G)
        self.fetch_document("precinct_schedules", SCHEDULES_URL, want=self.schedule_filter)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "probe_summary.md"
        path.write_text("\n".join(self.summary) + "\n", encoding="utf-8")
        return path
