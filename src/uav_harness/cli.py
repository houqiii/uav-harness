import argparse
import asyncio
import json
from pathlib import Path
import uvicorn
from .api import create_app
from .config import Settings
from .contracts import Plan
from .runtime import Harness


async def inspect(settings):
    harness = Harness(settings)
    await harness.open()
    try:
        print(json.dumps({"vehicles": harness.fleet()}, ensure_ascii=False, indent=2))
    finally:
        await harness.close()


async def run(settings, path, request_key):
    harness = Harness(settings)
    await harness.open()
    try:
        plan = Plan.model_validate_json(Path(path).read_text())
        row = await harness.submit(plan, request_key)
        if row["job_id"] in harness.jobs:
            await harness.jobs[row["job_id"]]
        row = harness.journal.get(row["job_id"])
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0 if row["status"] == "succeeded" else 1
    finally:
        await harness.close()


def main():
    parser = argparse.ArgumentParser(description="Independent heterogeneous MAVLink Harness")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("inspect", help="Identity/telemetry queries only; no motion or parameter writes")
    execute = commands.add_parser("run")
    execute.add_argument("plan")
    execute.add_argument("--request-key", required=True)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    if args.command == "serve":
        uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port)
    elif args.command == "inspect":
        asyncio.run(inspect(settings))
    else:
        raise SystemExit(asyncio.run(run(settings, args.plan, args.request_key)))


if __name__ == "__main__":
    main()
