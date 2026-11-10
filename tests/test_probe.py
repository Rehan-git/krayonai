import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from etl import extract, probe
from etl.extract import Crawler
from etl.probe import Case, Prober, describe_tables, parse_ajax, parse_form

REAL_2026 = Path(__file__).resolve().parents[1] / "data/raw/_discovery/pages/elections_results_42__d3cbf80866.html"


# ---------- pure helpers ----------

@pytest.mark.skipif(not REAL_2026.exists(), reason="discovery page not present")
def test_parse_form_on_real_2026_page():
    f = parse_form(REAL_2026.read_text(encoding="utf-8"))
    assert f["action"] == "/elections/results/42"
    assert f["fields"]["election_id"] == "42"
    assert f["fields"]["form_id"] == "election_results_form"
    assert f["fields"]["form_build_id"].startswith("form-")
    assert "fulltext" not in f["fields"]            # the site search form is separate
    contests = dict(f["selects"]["contest"])
    assert contests["12"].startswith("Governor")
    assert contests["121"] == "President, Board of Education"
    assert contests[""] == "Registered Voters/Ballots/Turnout"
    s = probe.drupal_settings(REAL_2026.read_text(encoding="utf-8"))
    assert s["ajax"]["edit-submit"]["url"] == "/elections/results/42?ajax_form=1"


def test_parse_ajax_and_tables():
    cmds = [{"command": "settings", "settings": {}},
            {"command": "update_build_id", "old": "form-a", "new": "form-b"},
            {"command": "insert", "data": "<table><tr><th>Precinct</th><th>Votes</th></tr>"
                                          "<tr><td>1</td><td>10</td></tr><tr><td>Total</td><td>10</td></tr></table>"}]
    res = parse_ajax(json.dumps(cmds))
    assert res["build_id"] == "form-b"
    t = describe_tables(res["html"])
    assert t[0]["n_rows"] == 3 and t[0]["rows"][0] == ["Precinct", "Votes"] and t[0]["last_row"] == ["Total", "10"]
    with pytest.raises(probe.ProbeError):
        parse_ajax("<html>not json</html>")


# ---------- mock Drupal site ----------

FORM = """<html><script type="application/json" data-drupal-selector="drupal-settings-json">
{{"ajaxPageState": {{"theme": "cboe", "libraries": "libs", "theme_token": "tok"}}}}</script>
<form action="/elections/results/{pid}" method="post" id="election-results-form">
<select name="contest"><option value="" selected>Registered Voters/Ballots/Turnout</option>
<option value="12">Governor &amp; Lieutenant Governor, Illinois</option><option value="110">SB 1st</option></select>
<select name="ward">{wards}</select><select name="precinct">{precincts}</select>
<input type="hidden" name="election_id" value="{pid}" />
<input type="hidden" name="form_build_id" value="{build}" />
<input type="hidden" name="form_id" value="election_results_form" />
<input type="text" name="homepage" value="" />
<input type="submit" name="submit" value="Submit" /></form></html>"""
WARDS = '<option value="">ALL</option>' + "".join(f'<option value="{i}">{i}</option>' for i in range(1, 51))


class Drupal(BaseHTTPRequestHandler):
    posts: list = []
    builds = 0

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        if self.path == "/robots.txt":
            return self._send(200, "text/plain", "User-agent: *\nDisallow: /admin/\n")
        if self.path.startswith("/elections/results/"):
            pid = self.path.rsplit("/", 1)[1]
            Drupal.builds += 1
            return self._send(200, "text/html", FORM.format(pid=pid, build=f"form-{Drupal.builds}",
                                                             wards='<option value="">ALL</option>',
                                                             precincts='<option value="">ALL</option>'))
        if self.path == "/media/1337":
            return self._send(200, "text/html", '<a href="/sites/default/files/2024-11/proc.pdf">PDF</a>')
        if self.path == "/sites/default/files/2024-11/proc.pdf":
            return self._send(200, "application/pdf", b"%PDF-1.4 fake")
        if self.path == "/districts-maps/district-precinct-schedules":
            return self._send(200, "text/html",
                              '<a href="/files/sb_subdistricts.xlsx">Board of Education Subdistricts</a>'
                              '<a href="/files/congress.xlsx">Congressional</a>')
        if self.path.startswith("/files/") or self.path.startswith("/export"):
            return self._send(200, "application/vnd.ms-excel", b"XLS")
        self._send(404, "text/plain", "nope")

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
        data = {k: v[0] for k, v in data.items()}
        Drupal.posts.append(data)
        assert self.headers["X-Requested-With"] == "XMLHttpRequest"
        Drupal.builds += 1
        new = f"form-{Drupal.builds}"
        trig = data["_triggering_element_name"]
        pid = data["election_id"]
        cmds = [{"command": "update_build_id", "old": data["form_build_id"], "new": new}]
        if trig in ("contest", "ward"):
            precincts = '<option value="">ALL</option>' + (
                "".join(f'<option value="{i}">{i}</option>' for i in range(1, 40)) if trig == "ward" else "")
            cmds.append({"command": "insert", "data": FORM.format(pid=pid, build=new, wards=WARDS,
                                                                  precincts=precincts)})
        else:
            cmds.append({"command": "insert", "data":
                         "<p>Unofficial results, updated 11/09/2026</p>"
                         "<table><tr><th>Ward</th><th>Votes</th></tr><tr><td>1</td><td>5</td></tr></table>"
                         f'<a href="/export?contest={data["contest"]}&ward={data["ward"]}">Download Excel</a>'})
        self._send(200, "application/json", json.dumps(cmds))

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(extract, "MIN_DELAY", 0.0)
    Drupal.posts, Drupal.builds = [], 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Drupal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(probe, "BASE_URL", base)
    monkeypatch.setattr(probe, "PROCLAMATION_2024G", base + "/media/1337")
    monkeypatch.setattr(probe, "SCHEDULES_URL", base + "/districts-maps/district-precinct-schedules")
    yield base
    srv.shutdown()


def make(base, tmp_path, cases):
    c = Crawler(out_dir=tmp_path, start_urls=[base + "/"], delay=0, log=lambda *a: None)
    return Prober(c, cases=cases, log=lambda *a: None)


def test_probe_replays_form_and_saves_everything(site, tmp_path):
    cases = [Case("gov_all", "42", "12", ""), Case("gov_w01", "42", "12", "1")]
    summary = make(site, tmp_path, cases).run().read_text()

    steps = [(p["_triggering_element_name"], p["ward"]) for p in Drupal.posts]
    assert steps == [("contest", ""), ("submit", ""), ("contest", ""), ("ward", "1"), ("submit", "1")]
    for p in Drupal.posts:
        assert p["contest"] == "12" and p["election_id"] == "42" and p["_drupal_ajax"] == "1"
        assert p["ajax_page_state[theme]"] == "cboe" and p["homepage"] == ""
        assert "submit" not in p
    # each POST carries the build id handed back by the previous step
    assert Drupal.posts[1]["form_build_id"] != Drupal.posts[0]["form_build_id"]
    assert Drupal.posts[1]["_triggering_element_value"] == "Submit"

    d = tmp_path / "probe"
    assert (d / "gov_all__3_submit.json").exists() and (d / "gov_w01__2_ward.json").exists()
    assert any(p.name.startswith("gov_w01__export_") for p in d.iterdir())
    assert any(p.name.endswith(".pdf") and p.name.startswith("proclamation_2024g") for p in d.iterdir())
    sched = [p.name for p in d.iterdir() if p.name.startswith("precinct_schedules__")]
    assert any("sb_subdistricts" in n for n in sched) and not any("congress" in n for n in sched)
    assert "Governor & Lieutenant Governor, Illinois" in summary
    assert "ward options after contest change: 51" in summary
    assert "precinct options after ward change: 40" in summary
    assert "[unofficial]" in summary and "Download Excel" in summary


def test_probe_rerun_is_cached(site, tmp_path):
    cases = [Case("t_all", "41", "", "")]
    make(site, tmp_path, cases).run()
    n = len(Drupal.posts)
    out = make(site, tmp_path, cases).run().read_text()
    assert len(Drupal.posts) == n and "cached from an earlier run" in out


def test_unknown_contest_stops_case_not_run(site, tmp_path):
    out = make(site, tmp_path, [Case("bad", "42", "999", "")]).run().read_text()
    assert "STOPPED" in out and Drupal.posts == []


def test_wrapper_fragment_without_form_tag_still_updates_options(site, tmp_path, monkeypatch):

    def fragment_only(self):
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
        trig = data["_triggering_element_name"][0]
        html = (f'<div id="w"><select name="ward">{WARDS}</select></div>' if trig == "contest"
                else "<table><tr><td>x</td></tr></table>")
        self._send(200, "application/json", json.dumps([{"command": "insert", "data": html}]))
    monkeypatch.setattr(Drupal, "do_POST", fragment_only)
    out = make(site, tmp_path, [Case("frag", "42", "12", "")]).run().read_text()
    assert "ward options after contest change: 51" in out
