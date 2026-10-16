import asyncio
import json
import math

import httpx
import pytest
from pydantic import ValidationError

from uav_harness.brain.contracts import Decision, MissionRequest, Policy
from uav_harness.brain.geometry import ground_target, approach_offset
from uav_harness.brain.simulation import SimulatedPort
from uav_harness.brain.provider import QwenPlanner
from uav_harness.brain.runner import Mission
from uav_harness.errors import HarnessError


def decision(obs, action=None, target=None):
    return Decision.model_validate(
        {
            "observation_id": obs.observation_id,
            "task_goal": "approach",
            "task_interpretation": "寻找红帽子人员并靠近观察",
            "scene_summary": "根据当前图像判断",
            "plan": ["检查目标", "根据新观测选择动作"],
            "target": target,
            "next_action": action or {"action": "observe"},
        }
    )


def detection(box=None):
    return {
        "label": "戴红帽子的人",
        "bbox": box or [460, 400, 540, 600],
        "ground_contact_visible": True,
    }


def test_contracts_are_generic_strict_and_bounded():
    assert (
        MissionRequest(query="寻找蓝色帐篷", vehicle_id="px4-1").query == "寻找蓝色帐篷"
    )
    with pytest.raises(ValidationError):
        MissionRequest(query="   ", vehicle_id="px4-1")
    with pytest.raises(ValidationError):
        Decision.model_validate(
            {
                "observation_id": "x",
                "task_interpretation": "x",
                "scene_summary": "x",
                "plan": [],
                "next_action": {"action": "shell", "command": "anything"},
            }
        )
    with pytest.raises(ValidationError):
        Policy(max_step_m=float("nan"))


async def test_ground_projection_and_standoff_are_computed_not_vlm_metres():
    p = SimulatedPort("v", seed=5)
    p.position = [0, 0, -50]
    p.heading = 90
    p.pitch = -45
    o = await p.observe()
    t = ground_target(o, detection([490, 480, 510, 500]))
    assert t["position_ned"][0] == pytest.approx(0, abs=0.01)
    assert t["position_ned"][1] == pytest.approx(50, abs=0.1)
    offset = approach_offset(o, t, Policy(), 10)
    assert math.dist(offset, (0, 0, 0)) <= 10.001
    o.camera.ground_down_m = None
    with pytest.raises(HarnessError, match="ground"):
        ground_target(o, detection())


async def test_simulation_camera_changes_with_actions_and_does_not_leak_world_targets():
    p = SimulatedPort("v", seed=21)
    a = await p.observe()
    await p.execute({"action": "takeoff", "altitude_m": 50})
    b = await p.observe()
    await p.execute({"action": "yaw", "heading_deg": 120})
    c = await p.observe()
    assert a.image != b.image and b.image != c.image
    assert c.telemetry.position_ned[2] == -50
    assert c.camera.yaw_deg == 120
    assert "people" not in json.dumps(c.model_dump(exclude={"image"}))
    assert a.observation_id != c.observation_id


async def test_provider_receives_image_telemetry_task_and_history_without_deepseek(
    monkeypatch,
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    requests = []
    obs = await SimulatedPort("v", seed=1).observe()

    def respond(req):
        requests.append(req)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": decision(obs).model_dump_json()},
                    }
                ]
            },
        )

    result = await QwenPlanner(httpx.MockTransport(respond)).decide(
        "找红帽子",
        obs,
        Policy(),
        [{"result": "last move rejected"}],
        None,
        lambda e: None,
    )
    assert result.next_action.action == "observe"
    wire = json.loads(requests[0].content)
    assert requests[0].url.host == "dashscope.aliyuncs.com"
    assert wire["model"] == "qwen3-vl-plus"
    assert wire["stream"]
    content = wire["messages"][-1]["content"]
    assert any(
        x["type"] == "image_url"
        and x["image_url"]["url"].startswith("data:image/jpeg;base64,")
        for x in content
    )
    assert "last move rejected" in json.dumps(content)
    assert "position_ned" in json.dumps(content)


async def test_motion_history_cannot_supply_a_stale_image_box_as_current_evidence():
    from uav_harness.brain.provider import planning_memory

    obs = await SimulatedPort("v", seed=1).observe()
    obs.camera.position_ned = (0, 0, -50)
    old = {
        "label": "person",
        "confirmed_observations": 3,
        "bbox": [518, 475, 532, 508],
        "geometry": {"position_ned": [20, 0, 0], "uncertainty_m": 2},
        "last_seen_at": 1,
        "observation_id": "old-frame",
    }
    history = [
        {
            "decision": {
                "next_action": {"action": "approach", "step_m": 10},
                "scene_summary": "obsolete box 518 475 532 508",
                "target": old,
            },
            "result": {"status": "succeeded"},
        }
    ]
    actions, memory = planning_memory(obs, history, old)
    serialized = json.dumps([actions, memory])
    assert "bbox" not in serialized and "obsolete box" not in serialized
    assert actions[0]["result"]["status"] == "succeeded"
    assert memory["camera_aim_hint"]["pitch_deg"] == pytest.approx(
        -math.degrees(math.atan2(50, 20))
    )
    assert memory["camera_aim_hint"]["requires_current_image_confirmation"]
    assert memory["confirmed_observations"] == 3


async def test_provider_rejects_stale_reference_and_truncated_output(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    obs = await SimulatedPort("v", seed=1).observe()

    def reply(req):
        d = decision(obs).model_dump()
        d["observation_id"] = "wrong"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(d)}}
                ]
            },
        )

    with pytest.raises(HarnessError, match="observation"):
        await QwenPlanner(httpx.MockTransport(reply)).decide(
            "找人", obs, Policy(), [], None, lambda e: None
        )


class ScriptedPlanner:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.inputs = []

    async def decide(self, query, obs, policy, history, target, progress):
        self.inputs.append((obs, history.copy()))
        fn = next(self.choices)
        return fn(obs)


async def test_runner_replans_from_new_observation_and_actual_action_result():
    p = SimulatedPort("v", seed=3)
    planner = ScriptedPlanner(
        [
            lambda o: decision(o, {"action": "takeoff", "altitude_m": 30}),
            lambda o: decision(o, {"action": "altitude", "altitude_m": 20}),
            lambda o: decision(o, {"action": "finish", "outcome": "needs_operator"}),
        ]
    )
    m = Mission(
        MissionRequest(query="观察后调整高度", vehicle_id="v"), p, planner, Policy()
    )
    await m.run()
    assert [x["action"] for x in p.executions][:2] == ["takeoff", "altitude"]
    assert planner.inputs[1][0].telemetry.position_ned[2] == -30
    assert planner.inputs[2][0].telemetry.position_ned[2] == -20
    assert planner.inputs[1][1][-1]["result"]["status"] == "succeeded"
    assert m.status == "needs_operator"


@pytest.mark.parametrize(
    "pitch,fx,covered", [(-35, 1236, True), (-35, 6000, False), (-5, 1236, False)]
)
async def test_search_coverage_requires_a_ground_view_wide_enough_for_scan_step(
    pitch, fx, covered
):
    p = SimulatedPort("v", seed=3)
    p.position = [0, 0, -50]
    p.pitch = pitch
    observe = p.observe

    async def frame():
        obs = await observe()
        obs.camera.fx = fx
        return obs

    p.observe = frame
    planner = ScriptedPlanner(
        [lambda o: decision(o, {"action": "finish", "outcome": "needs_operator"})]
    )
    mission = Mission(
        MissionRequest(query="寻找目标", vehicle_id="v"), p, planner, Policy()
    )
    await mission.run()
    assert bool(mission.scanned) is covered


async def test_runner_rejects_unconfirmed_target_and_reports_feedback_in_next_round():
    p = SimulatedPort("v", seed=3)
    p.position = [0, 0, -50]
    planner = ScriptedPlanner(
        [
            lambda o: decision(o, {"action": "approach", "step_m": 5}, detection()),
            lambda o: decision(o, {"action": "finish", "outcome": "needs_operator"}),
        ]
    )
    m = Mission(
        MissionRequest(query="靠近红帽子", vehicle_id="v"), p, planner, Policy()
    )
    await m.run()
    assert not any(x["action"] == "move" for x in p.executions)
    assert planner.inputs[1][1][-1]["result"]["status"] == "rejected"


async def test_pause_cancels_inference_and_cannot_dispatch_late_decision():
    p = SimulatedPort("v", seed=2)
    entered = asyncio.Event()

    class Slow:
        async def decide(self, *args):
            entered.set()
            await asyncio.sleep(100)

    m = Mission(MissionRequest(query="找人", vehicle_id="v"), p, Slow(), Policy())
    task = asyncio.create_task(m.run())
    await entered.wait()
    await m.pause()
    await asyncio.wait_for(task, 2)
    assert m.status == "paused"
    assert all(x["action"] == "hold" for x in p.executions)


async def test_changed_pose_during_inference_does_not_execute_old_motion():
    p = SimulatedPort("v", seed=2)
    p.position = [0, 0, -50]

    class Moved:
        calls = 0

        async def decide(self, q, o, *args):
            self.calls += 1
            if self.calls == 1:
                p.heading = 100
                return decision(o, {"action": "altitude", "altitude_m": 20})
            return decision(o, {"action": "finish", "outcome": "needs_operator"})

    m = Mission(MissionRequest(query="找人", vehicle_id="v"), p, Moved(), Policy())
    await m.run()
    assert not any(x["action"] == "altitude" for x in p.executions)
    assert any(e["kind"] == "rejected" for e in m.events)


async def test_official_account_error_is_reported_without_provider_fallback(
    monkeypatch,
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "account-test")
    obs = await SimulatedPort("v", seed=1).observe()

    def reply(req):
        return httpx.Response(
            400, json={"error": {"code": "Arrearage", "message": "account overdue"}}
        )

    with pytest.raises(HarnessError, match="Arrearage"):
        await QwenPlanner(httpx.MockTransport(reply)).decide(
            "找人", obs, Policy(), [], None, lambda e: None
        )


async def test_auto_goal_is_inferred_by_vlm_and_cannot_silently_expand_later():
    p = SimulatedPort("v", seed=2)
    p.position = [0, 0, -50]
    first = lambda o: decision(o).model_copy(update={"task_goal": "find"})
    second = lambda o: decision(o, {"action": "approach", "step_m": 5}, detection())
    last = lambda o: decision(
        o, {"action": "finish", "outcome": "needs_operator"}
    ).model_copy(update={"task_goal": "find"})
    planner = ScriptedPlanner([first, second, last])
    m = Mission(
        MissionRequest(query="只找到就好，不靠近", vehicle_id="v"), p, planner, Policy()
    )
    await m.run()
    assert m.resolved_objective == "find"
    assert not any(c["action"] == "move" for c in p.executions)
    assert any("goal" in e["message"] for e in m.events if e["kind"] == "rejected")


async def test_near_nadir_camera_renders_targets_on_either_side_of_optical_axis():
    p = SimulatedPort("v", seed=3)
    p.position = [0, 0, -50]
    p.pitch = -85
    p.people = [(-10, 0, "red"), (10, 0, "blue")]
    obs = await p.observe()
    assert obs.image.startswith(b"\xff\xd8")


async def test_task_deadline_cancels_inference_and_holds_without_dispatch():
    p = SimulatedPort("v", seed=2)

    class Slow:
        async def decide(self, *args):
            await asyncio.sleep(100)

    m = Mission(
        MissionRequest(query="找人", vehicle_id="v"),
        p,
        Slow(),
        Policy(max_duration_s=0.05),
    )
    await asyncio.wait_for(m.run(), 1)
    assert m.status == "needs_operator"
    assert all(c["action"] == "hold" for c in p.executions)


async def test_duplicate_pause_does_not_interrupt_hold_cleanup():
    entered = asyncio.Event()
    holding = asyncio.Event()
    release = asyncio.Event()

    class Port(SimulatedPort):
        async def hold(self):
            holding.set()
            await release.wait()
            return await super().hold()

    class Slow:
        async def decide(self, *args):
            entered.set()
            await asyncio.sleep(100)

    p = Port("v", seed=2)
    m = Mission(MissionRequest(query="找人", vehicle_id="v"), p, Slow(), Policy())
    task = asyncio.create_task(m.run())
    await entered.wait()
    first = asyncio.create_task(m.pause())
    await holding.wait()
    second = asyncio.create_task(m.pause())
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second, task), 1)
    assert m.status == "paused"


async def test_camera_rotation_during_inference_blocks_target_relative_dispatch():
    p = SimulatedPort("v", seed=2)
    p.position = [0, 0, -50]

    class Rotated:
        calls = 0

        async def decide(self, q, o, *args):
            self.calls += 1
            if self.calls == 1:
                p.pitch = -60
                return decision(o, {"action": "approach", "step_m": 5}, detection())
            return decision(o, {"action": "finish", "outcome": "needs_operator"})

    m = Mission(
        MissionRequest(query="靠近目标", vehicle_id="v"), p, Rotated(), Policy()
    )
    await m.run()
    assert any("camera" in e["message"] for e in m.events if e["kind"] == "rejected")
    assert not any(c["action"] == "move" for c in p.executions)


async def test_uncertain_mission_cannot_release_owner_while_cleanup_is_running(harness):
    from uav_harness.brain.service import BrainService

    service = BrainService(harness, False, planner=object())
    port = SimulatedPort("ap-1", seed=2)
    port.reconcile = lambda: None
    m = Mission(
        MissionRequest(query="查找目标", vehicle_id="ap-1"), port, object(), Policy()
    )
    m.status = "unknown"
    m.task = asyncio.create_task(asyncio.sleep(100))
    service.missions[m.mission_id] = m
    service.owners["ap-1"] = m.mission_id
    try:
        with pytest.raises(HarnessError, match="cleanup"):
            await service.control(m.mission_id, "reconcile")
        assert service.owners["ap-1"] == m.mission_id
    finally:
        m.task.cancel()
        await asyncio.gather(m.task, return_exceptions=True)


async def test_confirmed_target_closes_loop_with_geometric_steps_and_observation_standoff():
    from uav_harness.brain.geometry import project

    p = SimulatedPort("v", seed=4)
    p.people = [(65, 0, "red")]

    class FeedbackPlanner:
        async def decide(self, q, o, policy, history, target, progress):
            if not o.telemetry.airborne:
                return decision(o, {"action": "takeoff", "altitude_m": 50})
            distance = 65 - o.telemetry.position_ned[0]
            desired_pitch = -math.degrees(
                math.atan2(-o.telemetry.position_ned[2], distance)
            )
            if abs(desired_pitch - o.camera.pitch_deg) > 5:
                return decision(o, {"action": "gimbal", "pitch_deg": desired_pitch})
            u, v, _ = project(o.camera, o.camera.position_ned, (65, 0, 0))
            # Fixture-only detections at a known physical point; never a production provider.
            box = [
                (u - 10) / o.camera.width * 1000,
                (v - 25) / o.camera.height * 1000,
                (u + 10) / o.camera.width * 1000,
                v / o.camera.height * 1000,
            ]
            det = detection(box)
            g = ground_target(o, det)
            if not target or target["confirmed_observations"] < 2:
                return decision(o, {"action": "observe"}, det)
            if (
                g["horizontal_distance_m"]
                <= policy.stand_off_m + g["uncertainty_m"] + 0.3
            ):
                return decision(o, {"action": "finish", "outcome": "found"}, det)
            return decision(o, {"action": "approach", "step_m": 30}, det)

    m = Mission(
        MissionRequest(query="靠近戴红帽子的人并保持观察距离", vehicle_id="v"),
        p,
        FeedbackPlanner(),
        Policy(),
    )
    await m.run()
    moves = [c["offset_ned"] for c in p.executions if c["action"] == "move"]
    assert m.status == "succeeded", m.events[-5:]
    assert any(c["action"] == "gimbal" for c in p.executions)
    assert len(moves) > 1 and all(math.dist((0, 0, 0), x) <= 10.001 for x in moves)
    assert 20 < 65 - p.position[0] < 25
    assert p.position[2] == -50
    assert m.target["confirmed_observations"] >= 2
    assert m.round > len(moves)


async def test_invalid_model_action_reports_fields_without_raw_response(monkeypatch):
    from uav_harness.brain.provider import InvalidDecision, planning_memory

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    obs = await SimulatedPort("v", seed=1).observe()

    def reply(req):
        data = decision(obs).model_dump()
        data["next_action"] = {"action": "zoom", "zoom_x": 9}
        data["scene_summary"] = "private-response-marker"
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(data)}
        }]})

    with pytest.raises(InvalidDecision) as caught:
        await QwenPlanner(httpx.MockTransport(reply)).decide(
            "找人", obs, Policy(), [], None, lambda e: None
        )
    message = str(caught.value)
    assert "next_action.zoom.zoom_x" in message
    assert "less_than_equal" in message
    assert "private-response-marker" not in message
    assert "test-key" not in message
    actions, _ = planning_memory(obs, [{"result": {"status": "rejected", "error": message}}], None)
    assert actions[0]["result"]["error"] == message


async def test_explicit_pixel_boxes_normalize_without_guessing_or_clamping():
    from pydantic import ValidationError
    from uav_harness.brain.provider import parse_decision, response_schema

    obs = await SimulatedPort("v", seed=1).observe()
    obs.camera.width, obs.camera.height = 1280, 720
    data = decision(obs).model_dump()
    data["target"] = {"label": "person", "bbox": [1150, 320, 1160, 330],
                      "ground_contact_visible": False, "bbox_coordinate_space": "pixels"}
    result = parse_decision(json.dumps(data), obs)
    assert result.target.bbox == pytest.approx([898.4375, 444.4444444, 906.25, 458.3333333])
    assert "bbox_coordinate_space" not in result.target.model_dump()
    assert "bbox_coordinate_space" in response_schema()["$defs"]["Detection"]["required"]
    data["target"].pop("bbox_coordinate_space")
    with pytest.raises(ValidationError):
        parse_decision(json.dumps(data), obs)
    data["target"]["bbox_coordinate_space"] = "pixels"
    data["target"]["bbox"][2] = 1300
    with pytest.raises(ValidationError):
        parse_decision(json.dumps(data), obs)
