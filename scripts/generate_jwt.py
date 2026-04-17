"""Mint an Enable Banking JWT for the Home Assistant config flow.

Enable Banking's API signs requests with a short-lived JWT (RS256) derived
from your application's private key. This script builds one and prints it
to stdout, ready to paste into Settings → Devices & Services → Enable
Banking → Add → JWT field.

Usage:
    python scripts/generate_jwt.py --key path/to/key.pem --app-id <UUID>
    python scripts/generate_jwt.py --key key.pem --app-id <UUID> --ttl 2 --copy

Environment variable fallbacks (so you can omit the flags after first use):
    ENABLEBANKING_KEY       — path to the private key (.pem)
    ENABLEBANKING_APP_ID    — application UUID from the Enable Banking console

Requires once:
    pip install "pyjwt[crypto]"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import jwt
except ImportError:
    sys.exit(
        'Missing dependency. Install with:\n    pip install "pyjwt[crypto]"\n'
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint an Enable Banking JWT for the HA config flow.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=os.environ.get("ENABLEBANKING_KEY"),
        help="Path to RSA private key (PEM). Env: ENABLEBANKING_KEY",
    )
    parser.add_argument(
        "--app-id",
        default=os.environ.get("ENABLEBANKING_APP_ID"),
        help="Enable Banking application ID. Env: ENABLEBANKING_APP_ID",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=24,
        help="JWT lifetime in hours",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Also copy the JWT to the Windows clipboard",
    )
    args = parser.parse_args()

    missing: list[str] = []
    if args.key is None:
        missing.append("--key / ENABLEBANKING_KEY")
    if not args.app_id:
        missing.append("--app-id / ENABLEBANKING_APP_ID")
    if missing:
        parser.error("Missing: " + ", ".join(missing))
    return args


def build_jwt(key_path: Path, app_id: str, ttl_hours: int) -> str:
    try:
        private_key = key_path.read_bytes()
    except OSError as exc:
        sys.exit(f"Cannot read private key at {key_path}: {exc}")

    now = int(time.time())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + ttl_hours * 3600,
    }
    headers = {"typ": "JWT", "kid": app_id}

    try:
        return jwt.encode(payload, private_key, algorithm="RS256", headers=headers)
    except (ValueError, TypeError) as exc:
        sys.exit(
            f"Could not sign JWT — is {key_path} a valid RSA private key in PEM format?\n{exc}"
        )


def copy_to_clipboard(text: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(
            ["clip"], input=text, text=True, check=True, shell=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def main() -> int:
    args = parse_args()
    token = build_jwt(args.key, args.app_id, args.ttl)

    # Stdout: just the token, so `python generate_jwt.py ... | <thing>` is clean.
    print(token)

    # Stderr: friendly summary.
    print(f"\nJWT valid for {args.ttl}h (app {args.app_id[:8]}...).", file=sys.stderr)
    if args.copy and copy_to_clipboard(token):
        print("Copied to clipboard — paste into the HA config flow.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
