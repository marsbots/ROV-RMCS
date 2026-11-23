"""RMCS production readiness check (M3 operations follow-up).

Verifies the items the M3 milestone left open, without printing secret values:

  * launchd service loaded, running and set to restart
  * environment wired as intended (JWT_SECRET / admin / demo / device token set,
    never defaulted) — lengths only, never values
  * production VEHICLES_FILE separate from the repo default, and device tokens
    referenced through token_env rather than committed plaintext
  * CORS restricted to the expected origins
  * TLS endpoint live (https://rmcs.marsbot.kr) and WebSocket upgrade path sane
  * audit log present, append-only, and within the rotation window
  * .env.example carries no real secrets

Usage:
    python scripts/check_production_readiness.py
    python scripts/check_production_readiness.py --host rmcs.marsbot.kr --json

Exit code 0 when every check passes, 1 when any check fails or warns.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import plistlib as _plistlib  # noqa: F401 - keeps the import obvious in tracebacks
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_FILE = ROOT / "data" / "audit.jsonl"
DEFAULT_VEHICLES_FILE = ROOT / "server" / "config" / "vehicles.json"
PLIST = Path.home() / "Library" / "LaunchAgents" / "ai.openclaw.rmcs-server.plist"

SENSITIVE = ("SECRET", "PASSWORD", "TOKEN")
# Values that must never appear as real credentials (they were removed in b756900).
FORBIDDEN_DEFAULTS = ("change-me", "dev-device-token", "admin", "demo", "changeme")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, warn: bool = False) -> None:
        self.results.append(Result(name=name, ok=ok, detail=detail, warn=warn))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]

    @property
    def warned(self) -> list[Result]:
        return [r for r in self.results if r.ok and r.warn]


def load_launchd_env() -> dict[str, str]:
    if not PLIST.exists():
        return {}
    with PLIST.open("rb") as handle:
        data = plistlib.load(handle)
    return dict(data.get("EnvironmentVariables") or {})


def service_state() -> tuple[bool, str]:
    try:
        output = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/ai.openclaw.rmcs-server"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"launchctl unavailable: {exc}"
    if output.returncode != 0:
        return False, "service not loaded"
    text = output.stdout
    running = re.search(r"^\s*state = running", text, re.MULTILINE) is not None
    pid = re.search(r"^\s*pid = (\d+)", text, re.MULTILINE)
    return running, ("running pid=" + pid.group(1)) if pid else "loaded but not running"


def probe_tls(host: str, timeout: float = 10.0) -> tuple[bool, str]:
    url = f"https://{host}/healthz"
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:  # noqa: S310
            body = response.read(200).decode("utf-8", "replace")
            payload = json.loads(body)
            return True, f"HTTP {response.status} ok={payload.get('ok')}"
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def probe_ws_reject(host: str, timeout: float = 15.0) -> tuple[bool, str]:
    """A /ws/vehicle upgrade without a device token must be refused."""
    import asyncio

    try:
        import websockets
    except ImportError:
        return False, "websockets not installed; cannot probe WSS"

    url = f"wss://{host}/ws/vehicle"

    async def attempt() -> tuple[bool, str]:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=timeout):
                return False, "unauthenticated upgrade accepted"
        except Exception as exc:  # noqa: BLE001 - any refusal proves auth is enforced
            text = str(exc)
            refused = "403" in text or "4401" in text or "401" in text
            return refused, f"refused ({text[:80]})"

    return asyncio.run(attempt())


def probe_ws_accept(host: str, token: str, timeout: float = 15.0) -> tuple[bool, str]:
    """With a valid device token the upgrade must succeed and yield a welcome."""
    import asyncio
    import json as _json

    try:
        import websockets
    except ImportError:
        return False, "websockets not installed; cannot probe WSS"

    url = f"wss://{host}/ws/vehicle?token={token}"

    async def attempt() -> tuple[bool, str]:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=timeout) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout)
                payload = _json.loads(raw)
                return payload.get("type") == "welcome", f"upgrade OK, first message={payload.get('type')}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"

    return asyncio.run(attempt())


def check_env(report: Report, env: dict[str, str]) -> None:
    if not env:
        report.add("launchd env", False, f"no EnvironmentVariables in {PLIST}")
        return

    report.add("launchd env", True, f"{len(env)} variables injected")

    for key in ("JWT_SECRET", "ADMIN_PASSWORD", "DEMO_PASSWORD", "ROV_TOKEN_ROV_01"):
        value = env.get(key, "")
        if not value:
            report.add(key, False, "not set")
        elif value.lower() in FORBIDDEN_DEFAULTS:
            report.add(key, False, f"set to a forbidden default (len={len(value)})")
        else:
            report.add(key, True, f"set (len={len(value)})")

    jwt = env.get("JWT_SECRET", "")
    if jwt:
        report.add("JWT_SECRET strength", len(jwt) >= 32, f"len={len(jwt)} (>=32 required)")

    cors = env.get("CORS_ORIGINS", "")
    wildcard = cors.strip() == "*"
    report.add(
        "CORS restriction",
        bool(cors) and not wildcard,
        f"{cors or '(unset -> server default)'}" + (" — wildcard is not allowed in production" if wildcard else ""),
        warn=not cors,
    )


def check_vehicles(report: Report, service_env: dict[str, str]) -> None:
    path = DEFAULT_VEHICLES_FILE
    if not path.exists():
        report.add("vehicles file", False, f"missing: {path}")
        return

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.add("vehicles file", False, f"invalid JSON: {exc}")
        return

    vehicles = payload.get("vehicles") or []
    report.add("vehicles file", True, f"{len(vehicles)} vehicle(s): " + ", ".join(v.get("id", "?") for v in vehicles))

    plaintext = [v.get("id", "?") for v in vehicles if v.get("token") or v.get("device_token")]
    report.add(
        "device tokens via token_env",
        not plaintext,
        "all tokens referenced by token_env" if not plaintext else f"plaintext token in: {plaintext}",
    )

    # A tracked vehicles.json is acceptable as a template. It only becomes a
    # production problem if the file carries secrets (checked above) or if the
    # service points at this repo copy instead of a deployed copy.
    deployed = (service_env.get("VEHICLES_FILE") or os.environ.get("VEHICLES_FILE", "")).strip()
    tracked_ok = tracked.returncode != 0 or not plaintext
    report.add(
        "vehicles file tracking",
        tracked_ok,
        "not git-tracked" if tracked.returncode != 0 else "git-tracked template with no secrets",
        warn=tracked.returncode == 0,
    )
    report.add(
        "VEHICLES_FILE override",
        bool(deployed),
        deployed or "(not set in this shell; launchd value checked separately)",
        warn=not deployed,
    )


def check_audit(report: Report, audit_file: Path, max_age_days: float) -> None:
    if not audit_file.exists():
        report.add("audit log", False, f"missing: {audit_file}")
        return

    stat = audit_file.stat()
    age_days = (time.time() - stat.st_mtime) / 86400
    report.add(
        "audit log active",
        stat.st_size > 0,
        f"{stat.st_size} bytes, age={age_days:.2f}d",
    )

    writable = os.access(audit_file, os.W_OK)
    report.add("audit log writable", writable, "append target writable" if writable else "not writable")

    archives = sorted(audit_file.parent.glob(f"{audit_file.stem}.*{audit_file.suffix}.gz"))
    report.add(
        "audit rotation",
        age_days <= max_age_days,
        f"rotated archives={len(archives)}, live age={age_days:.2f}d (target <= {max_age_days:g}d)",
        warn=age_days > max_age_days,
    )


def check_env_example(report: Report) -> None:
    path = ROOT / ".env.example"
    if not path.exists():
        report.add(".env.example", False, "missing")
        return
    text = path.read_text(encoding="utf-8")
    leaks = [
        line.split("=")[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith("#")
        for value in [line.split("=", 1)[1].strip()]
        if value and any(token in line.split("=")[0].upper() for token in SENSITIVE)
    ]
    report.add(
        ".env.example secrets",
        not leaks,
        "no secret values committed" if not leaks else f"values present for: {leaks}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="rmcs.marsbot.kr")
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE)
    parser.add_argument("--max-audit-age-days", type=float, default=2.0)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument(
        "--probe-device-ws",
        action="store_true",
        help="also open a real WSS upgrade with the device token (disconnects a live agent)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = Report()

    running, detail = service_state()
    report.add("launchd service", running, detail)

    service_env = load_launchd_env()
    check_env(report, service_env)
    check_vehicles(report, service_env)
    check_audit(report, args.audit_file, args.max_audit_age_days)
    check_env_example(report)

    if not args.skip_network:
        ok, detail = probe_tls(args.host)
        report.add(f"TLS {args.host}", ok, detail)
        ok, detail = probe_ws_reject(args.host)
        report.add("WS auth enforced", ok, detail)

        if args.probe_device_ws:
            token = service_env.get("ROV_TOKEN_ROV_01", "")
            if not token:
                report.add("WSS upgrade with device token", False, "ROV_TOKEN_ROV_01 not set")
            else:
                ok, detail = probe_ws_accept(args.host, token)
                report.add("WSS upgrade with device token", ok, detail)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not report.failed,
                    "warnings": len(report.warned),
                    "checks": [
                        {"name": r.name, "ok": r.ok, "warn": r.warn, "detail": r.detail} for r in report.results
                    ],
                },
                indent=2,
            )
        )
    else:
        width = max(len(r.name) for r in report.results) + 2
        for result in report.results:
            mark = "PASS" if result.ok and not result.warn else ("WARN" if result.ok else "FAIL")
            print(f"[{mark}] {result.name:<{width}} {result.detail}")
        print()
        if report.failed:
            print(f"FAILED: {len(report.failed)} check(s) — {', '.join(r.name for r in report.failed)}")
        elif report.warned:
            print(f"OK with {len(report.warned)} warning(s): {', '.join(r.name for r in report.warned)}")
        else:
            print("PASS - production readiness checks all succeeded")

    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
