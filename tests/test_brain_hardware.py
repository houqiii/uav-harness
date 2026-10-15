import io
import time

import httpx
import pytest
from PIL import Image

from uav_harness.brain.settings import CameraConfig
from uav_harness.brain.hardware import SnapshotCamera
from uav_harness.brain.contracts import Telemetry
from uav_harness.contracts import Plan
from uav_harness.errors import HarnessError


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (640, 480)).save(out, format="JPEG")
    return out.getvalue()


def telemetry():
    return Telemetry(
        observed_at=time.time(),
        position_ned=(0, 0, -50),
        heading_deg=0,
        armed=True,
        airborne=True,
        source="test",
    )


async def test_http_camera_requires_fresh_capture_time_and_matching_calibration():
    config = CameraConfig(
        vehicle_id="v",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
        width=640,
        height=480,
        fx=500,
        fy=500,
        ground_down_m=0,
    )
    camera = SnapshotCamera(
        config,
        lambda: telemetry(),
        lambda: {"pitch": 0, "roll": 0},
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=jpeg(), headers={"X-Capture-Timestamp": str(time.time())}
            )
        ),
    )
    obs = await camera.capture("v")
    assert obs.camera.ground_down_m == 0 and obs.camera.timing == "camera_timestamp"
    assert obs.telemetry.position_ned[2] == -50
    old = SnapshotCamera(
        config,
        lambda: telemetry(),
        lambda: {"pitch": 0, "roll": 0},
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=jpeg(), headers={"X-Capture-Timestamp": "1"}
            )
        ),
    )
    with pytest.raises(HarnessError, match="timestamp"):
        await old.capture("v")


async def test_camera_without_known_calibration_cannot_provide_metric_ground():
    config = CameraConfig(
        vehicle_id="v",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
        ground_down_m=0,
    )
    camera = SnapshotCamera(
        config,
        lambda: telemetry(),
        lambda: {"pitch": 0, "roll": 0},
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=jpeg(), headers={"X-Capture-Timestamp": str(time.time())}
            )
        ),
    )
    obs = await camera.capture("v")
    assert obs.camera.ground_down_m is None


async def test_px4_yaw_holds_position_and_waits_for_heading_telemetry(harness):
    plan = Plan.model_validate(
        {
            "actions": [
                {
                    "action_id": "takeoff",
                    "vehicle_id": "px4-1",
                    "action": "flight.takeoff",
                    "params": {"altitude_home_m": 1},
                },
                {
                    "action_id": "yaw",
                    "vehicle_id": "px4-1",
                    "action": "flight.yaw",
                    "depends_on": ["takeoff"],
                    "params": {"heading_deg": 90},
                },
            ]
        }
    )
    job = await harness.submit(plan, "brain-yaw-wire")
    await harness.jobs[job["job_id"]]
    assert harness.journal.get(job["job_id"])["status"] == "succeeded"
    points = harness.simulators["px4-1"].setpoints
    assert any(
        abs(p["yaw"] - 1.570796) < 0.01 and abs(p["z"] + 1) < 0.1 for p in points
    )


async def test_brain_reservation_blocks_other_harness_jobs_until_release(harness):
    await harness.reserve("px4-1", "brain-test")
    plan = Plan.model_validate(
        {
            "actions": [
                {"action_id": "a", "vehicle_id": "px4-1", "action": "vehicle.arm"}
            ]
        }
    )
    with pytest.raises(HarnessError, match="mission"):
        await harness.submit(plan, "other-owner")
    harness.release("px4-1", "brain-test")
    job = await harness.submit(plan, "after-release")
    await harness.jobs[job["job_id"]]


async def test_snapshot_rejects_body_or_gimbal_rotation_during_capture():
    angles = {"pitch": 0, "roll": 0}

    def response(req):
        angles["pitch"] = 30
        return httpx.Response(
            200, content=jpeg(), headers={"X-Capture-Timestamp": str(time.time())}
        )

    cfg = CameraConfig(
        vehicle_id="v", snapshot_url="http://camera.local/snapshot", mount_pitch_deg=-45
    )
    camera = SnapshotCamera(
        cfg, telemetry, lambda: angles.copy(), httpx.MockTransport(response)
    )
    with pytest.raises(HarnessError, match="pose|rotat|orientation"):
        await camera.capture("v")


async def test_manual_takeover_between_actions_is_not_overridden(harness):
    import asyncio
    from uav_harness.brain.hardware import HarnessPort
    from uav_harness.errors import Uncertain

    harness.settings.brain.allow_hardware_control = True
    cfg = CameraConfig(
        vehicle_id="px4-1",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
    )
    port = HarnessPort(harness, "px4-1", cfg, "mission-test")
    await port.execute({"action": "takeoff", "altitude_m": 1})
    await port.execute({"action": "yaw", "heading_deg": 30})
    sim = harness.simulators["px4-1"]
    sim.main, sim.sub = 1, 0
    await asyncio.sleep(0.15)
    before = len(sim.commands)
    with pytest.raises(Uncertain, match="manual"):
        port.telemetry()
    with pytest.raises(Uncertain, match="manual"):
        await port.hold()
    assert len(sim.commands) == before


async def test_interrupt_action_uses_native_hold_but_preserves_uncertain_journal(
    harness,
):
    import asyncio
    from uav_harness.brain.hardware import HarnessPort

    harness.settings.brain.allow_hardware_control = True
    cfg = CameraConfig(
        vehicle_id="px4-1",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
    )
    port = HarnessPort(harness, "px4-1", cfg, "mission-test")
    await port.execute({"action": "takeoff", "altitude_m": 1})
    action = asyncio.create_task(
        port.execute({"action": "move", "offset_ned": (10, 0, 0)})
    )
    # Cancel after the directed mode ACK, while the streamed displacement is pending.
    for _ in range(300):
        if port.session.target and port.session.mode() == (6, 0):
            break
        await asyncio.sleep(0.01)
    assert port.session.mode() == (6, 0)
    action.cancel()
    await asyncio.gather(action, return_exceptions=True)
    await port.hold()
    assert port.session.mode() == (4, 3)
    assert port.session.target is None
    assert harness.journal.unknown_vehicles() == {"px4-1"}
    assert port.interrupted_jobs
    with pytest.raises(HarnessError, match="ground/disarmed"):
        await port.reconcile()
    await harness.adapters["px4-1"].land()
    await port.reconcile()
    assert not harness.journal.unknown_vehicles()


async def test_takeoff_closes_transition_modes_before_next_inference(harness):
    import asyncio
    from uav_harness.brain.hardware import HarnessPort
    from uav_harness.errors import Uncertain

    harness.settings.brain.allow_hardware_control = True
    cfg = CameraConfig(
        vehicle_id="px4-1",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
    )
    port = HarnessPort(harness, "px4-1", cfg, "mission-test")
    await port.execute({"action": "takeoff", "altitude_m": 1})
    assert port.expected_modes == {(4, 3)}
    harness.simulators["px4-1"].main, harness.simulators["px4-1"].sub = 1, 0
    await asyncio.sleep(0.15)
    with pytest.raises(Uncertain, match="manual"):
        port.telemetry()


async def test_native_hold_transition_is_safe_with_concurrent_telemetry_polling(
    harness,
):
    import asyncio
    from uav_harness.brain.hardware import HarnessPort

    plan = Plan.model_validate(
        {
            "actions": [
                {
                    "action_id": "up",
                    "vehicle_id": "ap-1",
                    "action": "flight.takeoff",
                    "params": {"altitude_home_m": 1},
                }
            ]
        }
    )
    job = await harness.submit(plan, "initial-guided-takeoff")
    await harness.jobs[job["job_id"]]
    harness.settings.brain.allow_hardware_control = True
    cfg = CameraConfig(
        vehicle_id="ap-1",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
    )
    port = HarnessPort(harness, "ap-1", cfg, "mission-test")
    action = asyncio.create_task(port.hold())
    while not action.done():
        port.telemetry()
        await asyncio.sleep(0.001)
    await action
    assert not port.relinquished
    assert port.expected_modes == {(17, 0)}


async def test_px4_takeoff_manual_takeover_is_preserved_during_cancellation(harness):
    import asyncio
    from uav_harness.brain.hardware import HarnessPort
    from uav_harness.errors import Uncertain

    harness.settings.brain.allow_hardware_control = True
    cfg = CameraConfig(
        vehicle_id="px4-1",
        snapshot_url="http://camera.local/snapshot",
        mount_pitch_deg=-35,
    )
    port = HarnessPort(harness, "px4-1", cfg, "mission-test")
    action = asyncio.create_task(port.execute({"action": "takeoff", "altitude_m": 10}))
    for _ in range(300):
        if port.session.allowed_modes == {(4, 2), (4, 3)}:
            break
        await asyncio.sleep(0.01)
    assert port.session.allowed_modes == {(4, 2), (4, 3)}
    sim = harness.simulators["px4-1"]
    sim.main, sim.sub = 1, 0
    await asyncio.sleep(0.15)
    action.cancel()
    await asyncio.gather(action, return_exceptions=True)
    with pytest.raises(Uncertain, match="manual"):
        await port.hold()
    assert port.session.mode() == (1, 0)
    assert not port.session.target
