import csv
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from etl import extract
from etl.extract import BlockedError, Crawler, extract_links, is_file_link, looks_blocked

PAGES = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /elections/results/private\n"),
    "/elections/results": ("text/html", """
        <a href="/elections/results/100">November 3, 2026 General Election</a>
        <a href="/elections/results/90">November 5, 2024 General Election</a>
        <a href="/elections/results/80">February 28, 2023 Municipal Election</a>
        <a href="https://elsewhere.example/x">offsite</a>
        <a href="mailto:a@b.c">mail</a>"""),
    "/elections/results/100": ("text/html", """
        <a href="/elections/results/100?contest=5">Governor</a>
        <a href="/elections/results/100/download?contest=5&amp;format=xlsx">Download</a>
        <a href="/elections/results/private">secret</a>
        <a href="/about">About (out of scope)</a>"""),
    "/elections/results/100?contest=5": ("text/html", "<a href='/files/gov.xls'>xls</a>"),
    "/elections/results/90": ("text/html", "<a href='/files/sb.csv#top'>csv</a>"),
    "/elections/results/80": ("text/html", "<a href='/files/should_not_fetch.xls'>x</a>"),
    "/elections/results/100/download?contest=5&format=xlsx":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "XLSXBYTES"),
    "/files/gov.xls": ("application/vnd.ms-excel", "XLSBYTES"),
    "/files/sb.csv": ("text/csv", "ward,precinct\n1,1\n"),
}


class Handler(BaseHTTPRequestHandler):
    hits: list = []
    block = False

    def do_GET(self):
        Handler.hits.append(self.path)
        if Handler.block and self.path != "/robots.txt":
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.end_headers()
            self.wfile.write(b"<title>Attention Required! | Cloudflare</title>")
            return
        if self.path not in PAGES:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = PAGES[self.path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(extract, "MIN_DELAY", 0.0)
    Handler.hits, Handler.block = [], False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def make(site, tmp_path, **kw):
    return Crawler(out_dir=tmp_path, start_urls=[site + "/elections/results"],
                   delay=0, log=lambda *a: None, **kw)


def manifest(tmp_path):
    with (tmp_path / "manifest.csv").open() as f:
        return {r["url"]: r for r in csv.DictReader(f)}


def test_extract_links_absolute_dedup_no_fragments():
    links = extract_links('<a href="/a#x">A</a><a href="/a">A2</a><a href="tel:1">t</a>',
                          "https://h.example/p/q")
    assert links == [("https://h.example/a", "A")]


@pytest.mark.parametrize("url,expected", [
    ("https://h/x/file.XLSX", True),
    ("https://h/r/1/download?contest=2", True),
    ("https://h/r?format=csv", True),
    ("https://h/elections/results/100", False),
])
def test_is_file_link(url, expected):
    assert is_file_link(url) is expected


def test_looks_blocked():
    assert looks_blocked(403, {}, b"")
    assert looks_blocked(200, {"server": "cloudflare"}, b"<title>Just a moment...</title>")
    assert not looks_blocked(200, {"server": "cloudflare"}, b"<h1>Results</h1>")


def test_crawl_filters_elections_respects_robots_and_saves_files(site, tmp_path):
    summary = make(site, tmp_path).run()
    m = manifest(tmp_path)
    fetched = {u.replace(site, "") for u in m}
    assert "/elections/results/100" in fetched and "/elections/results/90" in fetched
    assert "/elections/results/80" not in fetched          # filtered out (2023)
    assert "/files/should_not_fetch.xls" not in fetched
    assert "/elections/results/private" not in fetched     # robots.txt
    assert "/about" not in fetched                         # outside path prefix
    assert "/private" not in " ".join(Handler.hits)
    files = {u.replace(site, ""): r for u, r in m.items() if r["kind"] == "file"}
    assert set(files) == {"/elections/results/100/download?contest=5&format=xlsx",
                          "/files/gov.xls", "/files/sb.csv"}
    assert files["/elections/results/100/download?contest=5&format=xlsx"]["local_path"].endswith(".xlsx")
    for r in files.values():
        assert (tmp_path / r["local_path"]).exists() and r["sha256"]
    assert summary["files"] == 3


def test_rerun_uses_cache_and_is_idempotent(site, tmp_path):
    make(site, tmp_path).run()
    first = manifest(tmp_path)
    Handler.hits = []
    make(site, tmp_path).run()
    assert Handler.hits == ["/robots.txt"] or Handler.hits == []
    assert manifest(tmp_path).keys() == first.keys()


def test_block_stops_immediately(site, tmp_path):
    Handler.block = True
    with pytest.raises(BlockedError):
        make(site, tmp_path).run()
    assert len([h for h in Handler.hits if h != "/robots.txt"]) == 1
