"""CLI entry point: python -m etl <command>.

Phase 1:  python -m etl fetch            # discovery crawl of results pages
Later:    python -m etl run --election 2026g --status Unofficial
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .extract import (BlockedError, Crawler, DEFAULT_ELECTION_FILTER,
                      DEFAULT_PREFIX, DEFAULT_START)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Polite discovery crawl; caches pages and files.")
    f.add_argument("--start-url", action="append", dest="start_urls",
                   help=f"Page(s) to start from (repeatable). Default: {DEFAULT_START}")
    f.add_argument("--out", default="data/raw/_discovery",
                   help="Cache directory (default: data/raw/_discovery)")
    f.add_argument("--path-prefix", default=DEFAULT_PREFIX,
                   help="Only follow HTML pages under this path (default: %(default)s)")
    f.add_argument("--election-filter", default=DEFAULT_ELECTION_FILTER,
                   help="Regex on start-page link text/URL picking elections to follow. "
                        "Pass '' to follow every link. Default: %(default)r")
    f.add_argument("--max-depth", type=int, default=2)
    f.add_argument("--max-pages", type=int, default=150)
    f.add_argument("--max-files", type=int, default=400)
    f.add_argument("--delay", type=float, default=4.0, help="Seconds between requests (min 2)")
    f.add_argument("--refresh", action="store_true", help="Re-download even if cached")

    p = sub.add_parser("probe", help="Phase 1 probe of the results form, 2024 proclamation "
                                      "and precinct schedules (about 20 requests).")
    p.add_argument("--out", default="data/raw/_discovery")
    p.add_argument("--delay", type=float, default=6.0,
                   help="Seconds between requests (default 6; the form may reject faster submits)")
    p.add_argument("--refresh", action="store_true", help="Redo steps already cached")

    r = sub.add_parser("fetch-results", help="Download raw results for every in-scope contest "
                                              "(about 3 requests per contest) plus QA documents.")
    r.add_argument("--election", action="append", choices=["2026g", "2024g"],
                   help="Repeatable. Default: both.")
    r.add_argument("--out", default="data/raw")
    r.add_argument("--delay", type=float, default=6.0, help="Seconds between requests (default 6)")
    r.add_argument("--refresh", action="store_true",
                   help="Re-download even if cached (e.g. when counts are updated or certified)")

    args = ap.parse_args(argv)
    if args.cmd == "fetch-results":
        from .fetch_results import ResultsFetcher
        crawler = Crawler(out_dir=Path(args.out), delay=args.delay, refresh=args.refresh)
        fetcher = ResultsFetcher(crawler, args.election or ["2026g", "2024g"])
        try:
            paths = fetcher.run()
        except BlockedError as e:
            print(f"\nBLOCKED: {e}", file=sys.stderr)
            return 2
        print("\nSummaries: " + ", ".join(str(p) for p in paths))
        if fetcher.failed:
            print(f"FAILED ({len(fetcher.failed)}): {', '.join(fetcher.failed)}. "
                  "Rerun the same command to retry only these.", file=sys.stderr)
            return 1
        print(f"All done. Commit and push {args.out}/.")
        return 0
    if args.cmd == "probe":
        from .probe import Prober
        crawler = Crawler(out_dir=Path(args.out), delay=args.delay, refresh=args.refresh)
        try:
            path = Prober(crawler).run()
        except BlockedError as e:
            print(f"\nBLOCKED: {e}", file=sys.stderr)
            return 2
        print(f"\nDone. Summary: {path}\nCommit and push {args.out}/ (probe/ and manifest.csv).")
        return 0
    if args.cmd == "fetch":
        crawler = Crawler(
            out_dir=Path(args.out),
            start_urls=args.start_urls or [DEFAULT_START],
            path_prefix=args.path_prefix,
            election_filter=args.election_filter or None,
            max_depth=args.max_depth, max_pages=args.max_pages,
            max_files=args.max_files, delay=args.delay, refresh=args.refresh,
        )
        try:
            summary = crawler.run()
        except BlockedError as e:
            print(f"\nBLOCKED: {e}", file=sys.stderr)
            return 2
        print(f"\nDone: {summary['pages']} pages, {summary['files']} files. "
              f"Manifest: {summary['manifest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
