"""Polite discovery crawler for chicagoelections.gov results pages.

Phase 1 tool: we don't yet know the site's results URL patterns, so this
crawls only the results section, caches every HTML page it reads, downloads
every spreadsheet/CSV/export link it finds, and writes a manifest. Nothing
here parses results; that is Phase 2, done from the cache.

Politeness:
  * honors robots.txt (fetched with the same User-Agent)
  * fixed delay between requests (default 4s, minimum 2s)
  * descriptive User-Agent
  * cache-first: a URL already on disk is never re-fetched unless --refresh
  * stops immediately on a block / challenge / rate-limit response
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urldefrag, urlparse

import requests

BASE_URL = "https://www.chicagoelections.gov"
DEFAULT_START = BASE_URL + "/elections/results"
DEFAULT_PREFIX = "/elections/results"
# Applied only to links on the start (index) page, to pick the elections to follow.
DEFAULT_ELECTION_FILTER = r"(2026|2024).*general|general.*(2026|2024)"
FILE_EXTENSIONS = {".xls", ".xlsx", ".csv", ".zip", ".txt"}
FILE_HINTS = ("download", "export", "format=xls", "format=csv")
USER_AGENT = (
    "precinct-results-etl/0.1 (public aggregate election results research; "
    "polite crawler: robots.txt, {delay}s delay, cached)"
)
MIN_DELAY = 2.0
MANIFEST_FIELDS = [
    "url", "kind", "local_path", "http_status", "content_type", "bytes",
    "sha256", "found_on", "link_text", "depth", "retrieved_on",
]


class BlockedError(RuntimeError):
    """The site refused us (Cloudflare block/challenge, 403, 429, 503)."""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href, self._text = href, []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.links.append((self._href, text))
            self._href = None


def extract_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Return (absolute_url_without_fragment, link_text) for every <a href>."""
    p = _LinkParser()
    p.feed(html)
    out, seen = [], set()
    for href, text in p.links:
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        if url not in seen:
            seen.add(url)
            out.append((url, text))
    return out


def is_file_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    if Path(path).suffix in FILE_EXTENSIONS:
        return True
    low = url.lower()
    return any(h in low for h in FILE_HINTS)


def looks_blocked(status: int, headers: dict, body: bytes) -> bool:
    if status in (403, 429, 503):
        return True
    head = body[:20000].lower()
    markers = (b"attention required", b"just a moment", b"cf-challenge",
               b"you have been blocked", b"challenge-platform")
    return "cloudflare" in headers.get("server", "").lower() and any(m in head for m in markers)


def cache_name(url: str, suffix: str) -> str:
    """Stable, readable, collision-safe filename for a URL."""
    parsed = urlparse(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (parsed.path + "_" + parsed.query).strip("/"))[:80].strip("_")
    digest = hashlib.sha256(url.encode()).hexdigest()[:10]
    return f"{slug or 'index'}__{digest}{suffix}"


def filename_from_response(url: str, resp: requests.Response) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    if m:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(m.group(1)))
        stem, suffix = Path(name).stem, Path(name).suffix
        return f"{stem}__{hashlib.sha256(url.encode()).hexdigest()[:10]}{suffix}"
    suffix = Path(urlparse(url).path).suffix or _suffix_for(resp.headers.get("content-type", ""))
    return cache_name(url, suffix)


def _suffix_for(content_type: str) -> str:
    ct = content_type.lower()
    if "spreadsheetml" in ct:
        return ".xlsx"
    if "ms-excel" in ct:
        return ".xls"
    if "csv" in ct:
        return ".csv"
    if "html" in ct:
        return ".html"
    return ".bin"


@dataclass
class Crawler:
    out_dir: Path
    start_urls: list[str] = field(default_factory=lambda: [DEFAULT_START])
    path_prefix: str = DEFAULT_PREFIX
    election_filter: str | None = DEFAULT_ELECTION_FILTER
    max_depth: int = 2
    max_pages: int = 150
    max_files: int = 400
    delay: float = 4.0
    refresh: bool = False
    respect_robots: bool = True
    session: requests.Session | None = None
    log: callable = print

    def __post_init__(self) -> None:
        self.delay = max(self.delay, MIN_DELAY)
        self.session = self.session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT.format(delay=int(self.delay))
        self.pages_dir = self.out_dir / "pages"
        self.files_dir = self.out_dir / "files"
        self.manifest_path = self.out_dir / "manifest.csv"
        self.manifest: dict[str, dict] = self._load_manifest()
        self._last_request = 0.0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self.host = urlparse(self.start_urls[0]).netloc

    # ---------- HTTP ----------
    def _get(self, url: str) -> requests.Response:
        return self._request("GET", url)

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        wait = self._last_request + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        resp = self.session.request(method, url, timeout=60, allow_redirects=True, **kw)
        if looks_blocked(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content):
            raise BlockedError(
                f"HTTP {resp.status_code} from {url} looks like a block/challenge/rate-limit. "
                "Stopping without retrying. Open the URL in a browser to see the page."
            )
        return resp

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        if self._robots is None:
            robots_url = f"{urlparse(url).scheme}://{self.host}/robots.txt"
            rp = urllib.robotparser.RobotFileParser()
            resp = self._get(robots_url)
            rp.parse(resp.text.splitlines() if resp.ok else [])
            self._robots = rp
        return self._robots.can_fetch(self.session.headers["User-Agent"], url)

    # ---------- manifest ----------
    def _load_manifest(self) -> dict[str, dict]:
        if not self.manifest_path.exists():
            return {}
        with self.manifest_path.open(newline="", encoding="utf-8") as f:
            return {row["url"]: row for row in csv.DictReader(f)}

    def _save_manifest(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            w.writeheader()
            for url in sorted(self.manifest):
                w.writerow({k: self.manifest[url].get(k, "") for k in MANIFEST_FIELDS})
        tmp.replace(self.manifest_path)

    def _cached(self, url: str) -> Path | None:
        row = self.manifest.get(url)
        if row and not self.refresh and row.get("local_path"):
            p = self.out_dir / row["local_path"]
            if p.exists():
                return p
        return None

    def _record(self, url, kind, path, resp, found_on="", text="", depth=""):
        body = path.read_bytes()
        self.manifest[url] = {
            "url": url, "kind": kind,
            "local_path": path.relative_to(self.out_dir).as_posix(),
            "http_status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "found_on": found_on, "link_text": text, "depth": depth,
            "retrieved_on": dt.date.today().isoformat(),
        }
        self._save_manifest()

    # ---------- fetch one ----------
    def fetch_page(self, url, found_on="", text="", depth=0) -> str | None:
        cached = self._cached(url)
        if cached:
            return cached.read_text(encoding="utf-8", errors="replace")
        if not self._allowed(url):
            self.log(f"  robots.txt disallows {url}; skipping")
            return None
        resp = self._get(url)
        if not resp.ok:
            self.log(f"  HTTP {resp.status_code} {url}; skipping")
            return None
        if "html" not in resp.headers.get("content-type", "html").lower():
            # Turned out to be a file; store it as one.
            self._store_file(url, resp, found_on, text, depth)
            return None
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        path = self.pages_dir / cache_name(url, ".html")
        path.write_bytes(resp.content)
        self._record(url, "page", path, resp, found_on, text, depth)
        return resp.text

    def _store_file(self, url, resp, found_on, text, depth) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        path = self.files_dir / filename_from_response(url, resp)
        path.write_bytes(resp.content)
        self._record(url, "file", path, resp, found_on, text, depth)
        self.log(f"  saved file {path.name} ({len(resp.content):,} bytes)")

    def fetch_file(self, url, found_on="", text="", depth=0) -> None:
        if self._cached(url) or not self._allowed(url):
            return
        resp = self._get(url)
        if not resp.ok:
            self.log(f"  HTTP {resp.status_code} {url}; skipping")
            return
        self._store_file(url, resp, found_on, text, depth)

    # ---------- crawl ----------
    def _in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == self.host and p.path.startswith(self.path_prefix)

    def run(self) -> dict:
        queue = deque((u, "", "", 0) for u in self.start_urls)
        seen = set(self.start_urls)
        pages = files = 0
        filt = re.compile(self.election_filter, re.I) if self.election_filter else None
        index_links: list[tuple[str, str]] = []

        while queue:
            url, found_on, text, depth = queue.popleft()
            if pages >= self.max_pages:
                self.log(f"Reached --max-pages {self.max_pages}; stopping crawl.")
                break
            self.log(f"[page d{depth}] {url}")
            html = self.fetch_page(url, found_on, text, depth)
            pages += 1
            if html is None:
                continue
            for link, ltext in extract_links(html, url):
                if link in seen or urlparse(link).netloc != self.host:
                    continue
                if is_file_link(link):
                    seen.add(link)
                    if files < self.max_files:
                        self.log(f"[file] {link}  ({ltext})")
                        self.fetch_file(link, url, ltext, depth + 1)
                        files += 1
                    continue
                if depth + 1 > self.max_depth or not self._in_scope(link):
                    continue
                if depth == 0:
                    index_links.append((link, ltext))
                    if filt and not (filt.search(ltext) or filt.search(link)):
                        continue
                seen.add(link)
                queue.append((link, url, ltext, depth + 1))

        if filt and index_links and not any(filt.search(t) or filt.search(u) for u, t in index_links):
            self.log("\nNo index links matched --election-filter. Links seen on the start page:")
            for u, t in index_links:
                self.log(f"  {t!r:60} {u}")
        return {"pages": pages, "files": files, "manifest": str(self.manifest_path)}
