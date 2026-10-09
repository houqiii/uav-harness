import json
from pathlib import Path
import pytest_asyncio
from uav_harness.config import Settings
from uav_harness.runtime import Harness


ROOT = Path(__file__).resolve().parents[1]


def mock_settings(tmp_path, control=True):
    raw = json.loads((ROOT/"examples/mock-fleet.json").read_text())
    raw["state_dir"] = str(tmp_path)
    raw["allow_control"] = control
    raw["ack_timeout_s"] = .3
    raw["telemetry_timeout_s"] = .5
    return Settings.model_validate(raw)


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(mock_settings(tmp_path))
    await h.open()
    try:
        yield h
    finally:
        await h.close()
