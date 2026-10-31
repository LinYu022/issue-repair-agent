"""Serve the local workbench. Credentials come from a host-only file, not frontend code."""

import argparse
import json
import secrets
from pathlib import Path

import uvicorn

from app.product_api import ProductHost, create_product_app

ROOT = Path(__file__).resolve().parents[1]


def load_host(data, credentials):
    host = ProductHost(credentials=json.loads(credentials.read_text(encoding="utf-8")))
    if data and data.exists():
        bundle = json.loads(data.read_text(encoding="utf-8"))
        host.archives, host.reports = bundle["archives"], bundle["reports"]
    return host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / ".local/p2-18/workbench.json")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".local/p2-18/credentials.json")
    parser.add_argument("--init-credentials", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8018)
    args = parser.parse_args()
    if args.init_credentials:
        args.credentials.parent.mkdir(parents=True, exist_ok=True)
        with args.credentials.open("x", encoding="utf-8") as stream:
            json.dump(
                {secrets.token_urlsafe(36): r for r in ("viewer", "planner", "approver", "operator", "curator")},
                stream,
                indent=2,
            )
        args.credentials.chmod(0o600)
        print("Role credentials saved locally. Open the file yourself; do not share it.")
        return
    host = load_host(args.data, args.credentials)
    try:
        uvicorn.run(create_product_app(host), host=args.host, port=args.port)
    finally:
        for loop in host.loops.values():
            if loop.session.durable is not None:
                loop.session.durable.close()


if __name__ == "__main__":
    main()
