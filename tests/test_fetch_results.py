import json
from urllib.parse import parse_qs

import pytest

from etl import fetch_results as fr
from etl.extract import BlockedError, Crawler
from etl.races import ELECTIONS, TURNOUT, Contest, Election
from test_probe import Drupal, FORM, WARDS, site  # noqa: F401  (mock server + fixture)

SPEC_RACES = ({"2026g-SBPRES", "2026g-SOS", "2026g-GOV"}
              | {f"2026g-SB{d:02d}{s}" for d in range(1, 11) for s in "AB"}
              | {f"2024g-SB{d:02d}" for d in range(1, 11)})


def test_race_map_covers_exactly_the_spec_races():
    ids = [c.race_id for e in ELECTIONS.values() for c in e.contests if c.race_id != TURNOUT]
    assert len(ids) == len(set(ids)) == 33 and set(ids) == SPEC_RACES
    assert [c.race_id for e in ELECTIONS.values() for c in e.contests].count(TURNOUT) == 2
    assert ELECTIONS["2026g"].contests[4] == Contest("2026g-SB01A", "122",
                                                     "Member, Board of Education, Subdistrict 1A")
    assert ELECTIONS["2026g"].contests[-1].value == "141"
    assert ELECTIONS["2024g"].contests[-1].label.endswith("10th District")
    assert ELECTIONS["2024g"].contests[3].label.endswith("3rd District")


RESULTS = ("<p>Download Data in Excel.</p><table><tr><th>Total Votes</th></tr><tr><td>9</td></tr></table>"
           + "".join(f"<h3>Ward {w}</h3><table><tr><th>Precinct</th><th>Total Voters</th><th>A</th><th>%</th></tr>"
                     "<tr><td>1</td><td>3</td><td>3</td><td>100%</td></tr>"
                     "<tr><td>2</td><td>0</td><td>0</td><td>0%</td></tr>"
                     "<tr><td>Total</td><td>3</td><td>3</td><td>100%</td></tr></table>" for w in (1, 2, 3)))


_ORIG_GET = Drupal.do_GET


class Results(Drupal):
    submit_html = RESULTS
    block = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        data = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        Drupal.posts.append(data)
        Drupal.builds += 1
        cmds = [{"command": "update_build_id", "old": data["form_build_id"], "new": f"form-{Drupal.builds}"}]
        if data["_triggering_element_name"] == "contest":
            cmds.append({"command": "insert", "data": FORM.format(pid=data["election_id"], build=f"form-{Drupal.builds}",
                                                                  wards=WARDS, precincts='<option value="">ALL</option>')})
        else:
            cmds.append({"command": "openDialog", "data": Results.submit_html})
        self._send(200, "application/json", json.dumps(cmds))

    def do_GET(self):
        if Results.block and self.path != "/robots.txt":
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.end_headers()
            return self.wfile.write(b"<title>Attention Required! | Cloudflare</title>")
        if self.path == "/doc.pdf":
            return self._send(200, "application/pdf", b"%PDF-1.4 doc")
        return _ORIG_GET(self)


@pytest.fixture
def results_site(site, monkeypatch):
    monkeypatch.setattr(Drupal, "do_POST", Results.do_POST)
    monkeypatch.setattr(Drupal, "do_GET", Results.do_GET)
    monkeypatch.setattr(fr, "BASE_URL", site)
    Results.submit_html, Results.block = RESULTS, False
    return site


ELECTIONS_T = {
    "2026g": Election("2026g", "42", "2026-11-03", (
        Contest(TURNOUT, "", "Registered Voters/Ballots/Turnout"),
        Contest("2026g-GOV", "12", "Governor & Lieutenant Governor, Illinois"))),
    "2024g": Election("2024g", "41", "2024-11-05", (Contest("2024g-SB01", "110", "SB 1st"),)),
}


def make(base, tmp_path, elections=ELECTIONS_T, ids=("2026g", "2024g")):
    c = Crawler(out_dir=tmp_path, start_urls=[base + "/"], delay=0, log=lambda *a: None)
    docs = {"2024g": [(base + "/doc.pdf", "proclamation_2024-11-05.pdf")]}
    return fr.ResultsFetcher(c, list(ids), log=lambda *a: None, elections=elections, documents=docs)


def test_fetches_every_contest_into_election_folders(results_site, tmp_path):
    f = make(results_site, tmp_path)
    paths = f.run()
    assert f.failed == []
    assert (tmp_path / "2026g" / "turnout.json").exists()
    assert (tmp_path / "2026g" / "2026g-GOV.json").exists()
    assert (tmp_path / "2024g" / "2024g-SB01.json").exists()
    assert (tmp_path / "2024g" / "proclamation_2024-11-05.pdf").read_bytes().startswith(b"%PDF")
    saved = json.loads((tmp_path / "2026g" / "2026g-GOV.json").read_text())
    assert [c["command"] for c in saved] == ["update_build_id", "openDialog"]   # raw, as served
    steps = [(p["election_id"], p["contest"], p["_triggering_element_name"], p["ward"]) for p in Drupal.posts]
    assert steps == [("42", "", "contest", ""), ("42", "", "submit", ""),
                     ("42", "12", "contest", ""), ("42", "12", "submit", ""),
                     ("41", "110", "contest", ""), ("41", "110", "submit", "")]
    summary = (tmp_path / "2026g" / "fetch_summary.md").read_text()
    assert "2026g-GOV: 'Governor & Lieutenant Governor, Illinois' -> 2026g-GOV.json: 3 wards, 6 precinct rows" in summary
    assert [p.name for p in paths] == ["fetch_summary.md", "fetch_summary.md"]
    manifest = (tmp_path / "manifest.csv").read_text()
    assert "#contest=12;ward=ALL" in manifest and "2026g/2026g-GOV.json" in manifest


def test_label_mismatch_is_refused_and_others_continue(results_site, tmp_path):
    wrong = {"2026g": Election("2026g", "42", "2026-11-03", (
        Contest("2026g-SOS", "12", "Secretary of State, State of Illinois"),
        Contest(TURNOUT, "", "Registered Voters/Ballots/Turnout")))}
    f = make(results_site, tmp_path, elections=wrong, ids=["2026g"])
    f.run()
    assert f.failed == ["2026g/2026g-SOS"]
    assert not (tmp_path / "2026g" / "2026g-SOS.json").exists()
    assert (tmp_path / "2026g" / "turnout.json").exists()
    assert "labeled 'Governor & Lieutenant Governor, Illinois'" in (tmp_path / "2026g" / "fetch_summary.md").read_text()
    assert all(p["contest"] == "" for p in Drupal.posts)          # nothing POSTed for the mismatch


def test_rerun_is_cached_and_refresh_refetches(results_site, tmp_path):
    make(results_site, tmp_path).run()
    n = len(Drupal.posts)
    make(results_site, tmp_path).run()
    assert len(Drupal.posts) == n
    c = Crawler(out_dir=tmp_path, start_urls=[results_site + "/"], delay=0, refresh=True, log=lambda *a: None)
    fr.ResultsFetcher(c, ["2024g"], log=lambda *a: None, elections=ELECTIONS_T, documents={}).run()
    assert len(Drupal.posts) == n + 2


def test_response_without_ward_tables_is_not_saved(results_site, tmp_path):
    Results.submit_html = "<p>No results available.</p>"
    f = make(results_site, tmp_path, ids=["2024g"])
    f.run()
    assert f.failed == ["2024g/2024g-SB01"]
    assert not (tmp_path / "2024g" / "2024g-SB01.json").exists()


def test_block_stops_run_but_writes_summary(results_site, tmp_path):
    Results.block = True
    with pytest.raises(BlockedError):
        make(results_site, tmp_path, ids=["2026g"]).run()
    assert "STOPPED (blocked)" in (tmp_path / "2026g" / "fetch_summary.md").read_text()
