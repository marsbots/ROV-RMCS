"""Rotate the RMCS audit log (M3 operations).

`docs/api-contract.md` §12 requires the append-only JSONL audit log to be rotated
and archived (minimum 90 days online, 1 year archived). The server only appends;
this script does the rotation part of that policy:

    python scripts/rotate_audit_logs.py                 # rotate if due
    python scripts/rotate_audit_logs.py --force         # rotate now
    python scripts/rotate_audit_logs.py --status        # show current state

Behaviour
---------
* Rotates when the live log is older than ``--max-age-days`` (default 1) or
  larger than ``--max-size-mb`` (default 100), matching the contract.
* Archives are written next to the live file as ``audit.YYYY-MM-DD.jsonl.gz``
  and never rewritten afterwards (append-only history).
* Rotation is a rename + gzip, so ordering is preserved: the archived file keeps
  the original line order and the new live file starts empty.
* Only files this script owns (``<stem>.*.jsonl.gz``) are ever deleted, and only
  once they exceed ``--keep-days`` (default 365). Nothing else is touched.

Exit codes: 0 rotated/clean, 1 error.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_FILE = ROOT / "data" / "audit.jsonl"


def archive_name(live: Path, stamp: str) -> Path:
    return live.with_name(f"{live.stem}.{stamp}{live.suffix}.gz")


def describe(live: Path, keep_days: int) -> str:
    if not live.exists():
        return f"live log missing: {live}"
    stat = live.stat()
    age_days = (time.time() - stat.st_mtime) / 86400
    lines = 0
    with live.open("rb") as handle:
        for _ in handle:
            lines += 1
    archives = sorted(live.parent.glob(f"{live.stem}.*{live.suffix}.gz"))
    return (
        f"live: {live}\n"
        f"  size={stat.st_size} bytes, lines={lines}, age={age_days:.2f}d\n"
        f"  archives={len(archives)} (keep={keep_days}d): "
        + ", ".join(a.name for a in archives[-5:])
    )


def rotate(live: Path, stamp: str) -> Path:
    target = archive_name(live, stamp)
    if target.exists():
        # Never overwrite history; disambiguate within the same second.
        index = 1
        while True:
            candidate = live.with_name(f"{live.stem}.{stamp}-{index}{live.suffix}.gz")
            if not candidate.exists():
                target = candidate
                break
            index += 1

    staging = live.with_suffix(live.suffix + ".rotating")
    shutil.move(str(live), str(staging))
    try:
        with staging.open("rb") as source, gzip.open(target, "wb", compresslevel=9) as dest:
            shutil.copyfileobj(source, dest)
    except Exception:
        # Restore the original log if compression failed; losing audit records is
        # worse than a stale rotation.
        if staging.exists():
            shutil.move(str(staging), str(live))
        raise
    staging.unlink()
    live.touch()
    return target


def prune(live: Path, keep_days: int) -> list[Path]:
    cutoff = time.time() - keep_days * 86400
    removed: list[Path] = []
    for archive in sorted(live.parent.glob(f"{live.stem}.*{live.suffix}.gz")):
        if archive.stat().st_mtime < cutoff:
            archive.unlink()
            removed.append(archive)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE)
    parser.add_argument("--max-age-days", type=float, default=1.0)
    parser.add_argument("--max-size-mb", type=float, default=100.0)
    parser.add_argument("--keep-days", type=int, default=365, help="archive retention (contract: >= 365)")
    parser.add_argument("--force", action="store_true", help="rotate regardless of age/size")
    parser.add_argument("--status", action="store_true", help="report state and exit without rotating")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    live: Path = args.audit_file
    if not live.exists():
        print(f"audit log not found: {live}", file=sys.stderr)
        return 1

    if args.status:
        print(describe(live, args.keep_days))
        return 0

    stat = live.stat()
    age_days = (time.time() - stat.st_mtime) / 86400
    size_mb = stat.st_size / (1024 * 1024)
    due = args.force or age_days >= args.max_age_days or size_mb >= args.max_size_mb

    if not due:
        if not args.quiet:
            print(f"rotation not due (age={age_days:.2f}d size={size_mb:.3f}MB); nothing to do")
        return 0

    if stat.st_size == 0:
        if not args.quiet:
            print("live audit log is empty; skipping rotation")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive = rotate(live, stamp)
    if not args.quiet:
        print(f"rotated -> {archive.name}")

    removed = prune(live, args.keep_days)
    if removed and not args.quiet:
        print("pruned expired archives: " + ", ".join(p.name for p in removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
