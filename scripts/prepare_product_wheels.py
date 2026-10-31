"""Download Linux CPython 3.12 wheels for the product's frozen production requirements."""

import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


def main():
    wanted = set(
        re.findall(r"^([a-z][a-z0-9-]*)==", (ROOT / "environments/workbench-requirements.txt").read_text(), re.M)
    )
    wanted.discard("tzdata")  # Windows-only requirement; the product image is Linux.
    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    target = ROOT / ".local/p2-18/wheels"
    target.mkdir(parents=True, exist_ok=True)
    wheels = []
    opener = build_opener(ProxyHandler({}))
    for package in packages:
        if package["name"] not in wanted:
            continue
        matches = [
            w
            for w in package["wheels"]
            if w["url"].endswith("-py3-none-any.whl") or ("cp312-cp312-manylinux" in w["url"] and "x86_64" in w["url"])
        ]
        wheel = next(iter(matches), None)
        if wheel is None:
            raise ValueError("linux312_wheel_missing: " + package["name"])
        path = target / wheel["url"].rsplit("/", 1)[-1]
        data = path.read_bytes() if path.exists() else opener.open(wheel["url"], timeout=60).read()
        if "sha256:" + hashlib.sha256(data).hexdigest() != wheel["hash"]:
            raise ValueError("wheel_hash_mismatch")
        path.write_bytes(data)
        wheels.append({"name": package["name"], "filename": path.name, "sha256": wheel["hash"]})
        print(package["name"] + ": verified", flush=True)
    if len(wheels) != len(wanted):
        raise ValueError("locked_package_missing")
    (target.parent / "wheels.json").write_text(json.dumps(wheels, indent=2), encoding="utf-8")
    print(json.dumps({"verified_linux_wheels": len(wheels)}))


if __name__ == "__main__":
    main()
