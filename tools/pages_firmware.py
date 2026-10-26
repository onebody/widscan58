#!/usr/bin/env python3
"""Stage PlatformIO firmware builds for the web flasher. GPL-3.0-or-later.

Copies a build's bootloader, partition table and app into <out>/<id>/<env>/ and
writes <out>/<id>/manifest.json in ESP Web Tools' manifest format, plus a
sha256 per part that the web UI checks before flashing, and the commit time
(the one the firmware's INFO reports) as "date". Offsets come from the IDF
build's flasher_args.json (the bootloader is at 0x2000 on the C5, 0x0 on the S3).

The versions to offer come from firmware/versions.json, a list of ids, labels
and git refs; .github/workflows/pages.yml builds each ref in its own worktree
and calls this once per version, then --index collects the manifests into
<out>/index.json, which the flash dialog reads:

    tools/pages_firmware.py --list
    tools/pages_firmware.py --id rh-uart --src /tmp/fw/rh-uart web/dist/firmware esp32c5
    tools/pages_firmware.py --index web/dist/firmware

Without --id a single version is staged straight into <out>/ and described by
<out>/manifest.json (what this tool did before there was a version list; still
handy for `npm run dev`).

--src is the checkout to take the build and the version from: the ref that was
built, not the copy of this script doing the staging (the official ref predates
it). It defaults to this repository.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "firmware" / "versions.json"
# esptool-js reports these names (ROM CHIP_NAME); the web UI matches on them.
CHIP_FAMILY = {"esp32c5": "ESP32-C5", "esp32s3": "ESP32-S3"}
# flasher_args.json section -> file name in the PlatformIO build directory.
PARTS = {"bootloader": "bootloader.bin", "partition-table": "partitions.bin", "app": "firmware.bin"}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def git(src: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True, check=True).stdout.strip()


def read_versions(path: Path) -> dict:
    try:
        versions = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {path}: {e}")
    ids = [v.get("id") for v in versions.get("versions", [])]
    if not ids or any(not isinstance(i, str) or not ID_RE.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        sys.exit(f"{path}: versions need unique ids of letters, digits, '.', '_' and '-'")
    if versions.get("default") not in ids:
        sys.exit(f"{path}: default must be one of the version ids")
    for v in versions["versions"]:
        if not v.get("ref"):
            sys.exit(f"{path}: version {v['id']} has no ref")
    return versions


def stage(a: argparse.Namespace) -> None:
    src = a.src.resolve()
    build_root = src / "firmware" / ".pio" / "build"
    version = a.version
    if version is None:
        version = git(src, "describe", "--tags", "--always", "--dirty")
    date = a.date
    if date is None:
        date = git(src, "log", "-1", "--format=%cI") or None

    dest = a.out / a.id if a.id else a.out
    dest.mkdir(parents=True, exist_ok=True)
    builds = []
    for env in a.envs:
        build = build_root / env
        try:
            args = json.loads((build / "flasher_args.json").read_text())
        except OSError:
            sys.exit(f"{build}: no build; run `pio run -d {src / 'firmware'} -e {env}` first")
        (dest / env).mkdir(exist_ok=True)
        parts = []
        for section, name in PARTS.items():
            data = (build / name).read_bytes()
            shutil.copyfile(build / name, dest / env / name)
            parts.append({
                "path": f"{env}/{name}",
                "offset": int(args[section]["offset"], 16),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        builds.append({"chipFamily": CHIP_FAMILY[env], "parts": parts})
        print(f"{a.id + '/' if a.id else ''}{env}: " + ", ".join(f"{p['path']} @ {p['offset']:#x}" for p in parts))

    manifest = {"name": "Widscan58", "version": version, "date": date, "builds": builds}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{dest / 'manifest.json'}: {version}")


def index(a: argparse.Namespace) -> None:
    versions = read_versions(a.versions)
    entries = []
    for v in versions["versions"]:
        try:
            manifest = json.loads((a.out / v["id"] / "manifest.json").read_text())
        except (OSError, ValueError) as e:
            sys.exit(f"{a.out / v['id']}: not staged ({e}); build and stage it first")
        entries.append({
            "id": v["id"],
            "label": v.get("label", v["id"]),
            "note": v.get("note", ""),
            "dir": v["id"],
            "manifest": manifest,
        })
    out = {"default": versions["default"], "versions": entries}
    (a.out / "index.json").write_text(json.dumps(out, indent=2) + "\n")
    for e in entries:
        print(f"{e['id']}: {e['manifest']['version']} ({e['manifest'].get('date') or 'no date'})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path, nargs="?", help="directory the web page serves as firmware/")
    ap.add_argument("envs", nargs="*", choices=sorted(CHIP_FAMILY))
    ap.add_argument("--versions", type=Path, default=VERSIONS, help="the version list (default: firmware/versions.json)")
    ap.add_argument("--id", help="stage into <out>/<id>/ for this version in the list")
    ap.add_argument("--src", type=Path, default=ROOT, help="checkout to build from and to take the version from")
    ap.add_argument("--list", action="store_true", help="print the versions as id, ref, label and note (tab separated)")
    ap.add_argument("--index", action="store_true", help="write <out>/index.json from the staged versions")
    ap.add_argument("--version", help="override the version (default: git describe of --src)")
    ap.add_argument("--date", help="override the commit time, ISO 8601 (default: HEAD's commit time of --src)")
    a = ap.parse_args()

    if a.list:
        for v in read_versions(a.versions)["versions"]:
            fields = [v["id"], v["ref"], v.get("label", v["id"]), v.get("note", "")]
            print("\t".join(" ".join(str(f).split()) for f in fields))
        return
    if a.out is None:
        ap.error("out is required")
    if a.index:
        if a.id or a.envs:
            ap.error("--index takes only out")
        index(a)
        return
    if not a.envs:
        ap.error("list at least one env to stage")
    if a.id is not None and not ID_RE.fullmatch(a.id):
        ap.error(f"bad --id {a.id!r}")
    stage(a)


if __name__ == "__main__":
    main()
