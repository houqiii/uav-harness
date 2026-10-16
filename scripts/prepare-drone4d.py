#!/usr/bin/env python3
"""Build an explicitly patched native Drone4D client; no Linux simulator is installed on this Mac."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PINNED_REVISION = "b4d83e309d2e8dc2bc9dbd7daa81b6e7148635a3"
parser = argparse.ArgumentParser()
parser.add_argument(
    "--source",
    type=Path,
    default=Path(os.getenv("DRONE4D_SOURCE", str(ROOT.parent / "ANet-Drone4D"))),
)
parser.add_argument("--output", type=Path, default=ROOT / "state/drone4d-web")
parser.add_argument(
    "--if-needed", action="store_true", help="reuse matching native bridge build"
)
args = parser.parse_args()
source = args.source.resolve()
output = args.output.resolve()
if not source.is_dir():
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--no-checkout",
            "https://github.com/ANetResearch/ANet-Drone4D.git",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", PINNED_REVISION],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", PINNED_REVISION], cwd=source, check=True
    )
revision = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True
).stdout.strip()
version = json.loads((source / "package.json").read_text())["version"]
if version != "0.2.0":
    raise SystemExit(f"Drone4D {version} needs compatibility review; expected 0.2.0")
bridge_hash = hashlib.sha256(
    (ROOT / "integrations/drone4d/harnessBridge.ts").read_bytes()
).hexdigest()
manifest = output / "harness-integration.json"
if args.if_needed and manifest.is_file() and (output / "index.html").is_file():
    try:
        if (
            json.loads(manifest.read_text()).get("bridge_sha256") == bridge_hash
            and json.loads(manifest.read_text()).get("source_revision") == revision
        ):
            print("Native Drone4D renderer already prepared:", output)
            raise SystemExit(0)
    except ValueError:
        pass
node = subprocess.run(
    ["node", "--version"], capture_output=True, text=True, check=True
).stdout.strip()
if not node.startswith("v22.") or int(node.split(".")[1]) < 12:
    raise SystemExit("Drone4D requires Node 22.12 or later in the Node 22 series")
if not (source / "node_modules").is_dir():
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=source, check=True)
shutil.copyfile(
    ROOT / "integrations/drone4d/harnessBridge.ts",
    source / "apps/web/src/harnessBridge.ts",
)
main = source / "apps/web/src/main.tsx"
text = main.read_text()
if "import { installHarnessBridge }" not in text:
    if "installBootCapacity()" not in text:
        raise SystemExit("Drone4D bootstrap changed; compatibility review required")
    text = "import { installHarnessBridge } from './harnessBridge'\n" + text
    text = text.replace(
        "installBootCapacity()",
        "await installHarnessBridge()\ninstallBootCapacity()",
        1,
    )
    main.write_text(text)
subprocess.run(
    ["npm", "run", "build", "-w", "apps/web", "--", "--base=/drone4d-assets/"],
    cwd=source,
    check=True,
)
output.mkdir(parents=True, exist_ok=True)
shutil.copytree(source / "apps/web/dist", output, dirs_exist_ok=True)
revision = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True
).stdout.strip()
(output / "harness-integration.json").write_text(
    json.dumps(
        {
            "drone4d_version": version,
            "source_revision": revision,
            "bridge": "native-fpv-v1",
            "bridge_sha256": bridge_hash,
        },
        indent=2,
    )
)
for filename in ("LICENSE", "NOTICE"):
    shutil.copyfile(source / filename, output / ("DRONE4D-" + filename))
print("Native Drone4D renderer ready:", output)
