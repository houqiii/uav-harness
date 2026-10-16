import json
import math
from types import SimpleNamespace

import pytest

from uav_harness.brain.contracts import Decision, MissionRequest, Policy, Telemetry
from uav_harness.brain.provider import InvalidDecision, RetryablePlannerError
from uav_harness.brain.runner import Mission
from uav_harness.brain.simulation import SimulatedPort
from uav_harness.drone4d.evaluation import EpisodeEvaluation
from uav_harness.drone4d.scene import search_scene
from uav_harness.errors import HarnessError


def proposal(obs, action):
    return Decision(
        observation_id=obs.observation_id,
        task_goal="approach",
        task_interpretation="自主寻找并靠近监控",
        scene_summary="根据当前图像",
        plan=["依据反馈调整"],
        next_action=action,
    )


async def test_vlm_can_explore_before_detection_but_cannot_escape_fence_or_cross_target():
    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    m = Mission(
        MissionRequest(query="自主搜索", vehicle_id="v"),
        port,
        None,
        Policy(max_step_m=30, max_radius_m=100, stand_off_m=30),
    )
    m.resolved_objective = "approach"
    cmd = m.ground_command(
        obs,
        proposal(obs, {"action": "search_move", "heading_deg": 90, "distance_m": 25}),
    )
    assert cmd["offset_ned"] == pytest.approx((0, 25, 0))
    m.policy.max_radius_m = 20
    with pytest.raises(HarnessError, match="radius"):
        m.ground_command(
            obs,
            proposal(
                obs, {"action": "search_move", "heading_deg": 90, "distance_m": 25}
            ),
        )
    m.policy.max_radius_m = 100
    m.target = {"geometry": {"position_ned": [0, 40, 0], "uncertainty_m": 2}}
    with pytest.raises(HarnessError, match="standoff"):
        m.ground_command(
            obs,
            proposal(
                obs, {"action": "search_move", "heading_deg": 90, "distance_m": 25}
            ),
        )


def test_monitoring_requires_physical_3d_range_not_horizontal_range_or_zoom():
    m = Mission(
        MissionRequest(query="50米内监控", vehicle_id="v"),
        None,
        None,
        Policy(stand_off_m=30, monitor_radius_m=50, distance_metric="slant"),
    )
    m.resolved_objective = "approach"
    m.target = {
        "visible": True,
        "confirmed_observations": 2,
        "geometry": {
            "horizontal_distance_m": 40,
            "slant_distance_m": math.hypot(40, 50),
            "uncertainty_m": 2,
        },
    }
    with pytest.raises(HarnessError, match="monitoring distance"):
        m.finish("found")
    m.target["geometry"].update(
        horizontal_distance_m=35, slant_distance_m=math.hypot(35, 25)
    )
    m.finish("found")
    assert m.status == "succeeded"


def test_evaluation_uses_actual_motion_and_does_not_publish_target_coordinates():
    def telemetry(n, d):
        return Telemetry(
            observed_at=1,
            position_ned=(n, 0, d),
            heading_deg=0,
            armed=True,
            airborne=True,
            source="test",
        )

    score = EpisodeEvaluation((150, 0, 0), telemetry(0, 0), 50)
    assert not score.snapshot("succeeded")["passed"]
    score.update(telemetry(120, -30))
    result = score.snapshot("succeeded")
    assert result["passed"] and result["flown_path_m"] > 120
    assert result["current_slant_distance_m"] == pytest.approx(math.sqrt(1800))
    assert "target" not in json.dumps(result) and "position" not in json.dumps(result)


def test_seed_changes_scene_but_never_inserts_a_preplanned_route(tmp_path):
    folder = tmp_path / "scenarios/sandbox/synthcity"
    folder.mkdir(parents=True)
    (folder / "t6_manual.json").write_text(
        json.dumps({"nests": [{"pos": [-26.9, -337.1, 0.05]}]})
    )
    positions = set()
    for seed in range(10, 25):
        scene = search_scene(tmp_path, seed)
        point = scene["targets"][0]["pos"]
        # Only native-DSM visibility-checked open-road positions may be used.
        assert point[:2] in [[-180.0, -300.0], [-175.0, -300.0], [-185.0, -300.0]]
        assert 140 <= math.dist(point[:2], [-31.9, -337.1]) <= 165
        # Initial yaw is east (90 degrees); keep every seed well outside that view.
        bearing = math.degrees(math.atan2(point[0] + 31.9, point[1] + 337.1)) % 360
        assert abs((bearing - 90 + 180) % 360 - 180) > 60
        assert not scene["tasks"]
        positions.add(tuple(point[:2]))
    assert len(positions) > 1
    assert search_scene(tmp_path, 17) == search_scene(tmp_path, 17)


@pytest.mark.parametrize("failure", [InvalidDecision, RetryablePlannerError])
async def test_invalid_vlm_json_recovers_with_fresh_observation_without_dispatch(
    failure,
):
    port = SimulatedPort("v", seed=1)
    seen = []

    class Planner:
        async def decide(self, q, obs, *args):
            seen.append(obs.observation_id)
            if len(seen) < 3:
                raise failure("transient planning failure; no action dispatched")
            return proposal(obs, {"action": "finish", "outcome": "needs_operator"})

    m = Mission(MissionRequest(query="监控", vehicle_id="v"), port, Planner(), Policy())
    await m.run()
    assert len(set(seen)) == 3 and m.status == "needs_operator"
    assert not any(x["action"] in ("move", "takeoff") for x in port.executions)


async def test_confirmed_target_can_be_approached_before_any_scan_coverage():
    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    m = Mission(
        MissionRequest(query="找到后接近", vehicle_id="v"), port, None, Policy()
    )
    m.resolved_objective = "approach"
    m.target = {
        "visible": True,
        "confirmed_observations": 2,
        "geometry": {
            "position_ned": [100, 0, 0],
            "horizontal_distance_m": 100,
            "uncertainty_m": 2,
        },
    }
    assert not m.scanned
    command = m.ground_command(obs, proposal(obs, {"action": "approach", "step_m": 10}))
    assert command["action"] == "move"
    assert command["offset_ned"] == pytest.approx((10, 0, 0))


async def test_candidate_association_survives_pose_jitter_but_requires_grounding():
    from uav_harness.brain.contracts import Detection
    from uav_harness.brain.provider import planning_memory

    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    m = Mission(MissionRequest(query="接近目标", vehicle_id="v"), port, None, Policy())
    m.resolved_objective = "approach"

    def frame(index, yaw, pitch, bbox):
        current = obs.model_copy(deep=True)
        current.observation_id = f"candidate-{index}"
        current.captured_at = obs.captured_at + index
        current.camera = current.camera.model_copy(
            update={
                "width": 1280,
                "height": 720,
                "cx": 640,
                "cy": 360,
                "fx": 2207,
                "fy": 2207,
                "yaw_deg": yaw,
                "pitch_deg": pitch,
                "roll_deg": 0,
            }
        )
        decision = proposal(current, {"action": "observe"})
        decision.target = Detection(
            label="疑似戴红帽人员", bbox=bbox, ground_contact_visible=False
        )
        return current, decision

    # Actual rounds 4–7: yaw/pitch change while the image candidate stays on
    # approximately the same world bearing. No target ground truth is used.
    samples = [
        (75.29, -16.16, [512, 578, 522, 602]),
        (75.10, -16.48, [518, 558, 528, 578]),
        (75.20, -16.03, [512, 580, 522, 605]),
        (75.52, -21.75, [508, 275, 516, 300]),
    ]
    for index, values in enumerate(samples, 1):
        current, decision = frame(index, *values)
        m.remember_target(current, decision)
        assert m.target["confirmed_observations"] == index
        m.remember_target(current, decision)
        assert m.target["confirmed_observations"] == index
    assert m.target["geometry"] is None
    with pytest.raises(HarnessError, match="ground geometry unavailable"):
        m.ground_command(
            current, proposal(current, {"action": "approach", "step_m": 10})
        )
    _, memory = planning_memory(current, [], m.target)
    assert memory["previous_observation_blockers"]
    assert memory["last_sighting_direction"]["directional_only"]
    assert "bbox" not in memory
    assert "historical_geometry" not in memory

    # Losing the target retains a reacquisition bearing, but breaks the streak.
    lost = current.model_copy(update={"observation_id": "lost"})
    m.remember_target(lost, proposal(lost, {"action": "observe"}))
    _, memory = planning_memory(lost, [], m.target)
    assert (
        "target_not_visible_in_previous_observation"
        in memory["previous_observation_blockers"]
    )
    current, decision = frame(5, *samples[-1])
    m.remember_target(current, decision)
    assert m.target["confirmed_observations"] == 1
    current, decision = frame(6, 95, -21.75, samples[-1][2])
    m.remember_target(current, decision)
    assert m.target["confirmed_observations"] == 1


async def test_aim_centres_current_ray_and_rejects_zoom_that_crops_candidate():
    from uav_harness.brain.contracts import Detection
    from uav_harness.brain.geometry import project, candidate_ray

    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    obs.camera = obs.camera.model_copy(
        update={
            "width": 1280,
            "height": 720,
            "cx": 640,
            "cy": 360,
            "fx": 2203.61,
            "fy": 2203.61,
            "zoom_x": 2,
            "yaw_deg": 270.09,
            "pitch_deg": -18.78,
            "roll_deg": -0.33,
        }
    )
    obs.capabilities += ["zoom", "aim_target"]
    m = Mission(MissionRequest(query="接近", vehicle_id="v"), port, None, Policy())
    d = proposal(obs, {"action": "aim_target"})
    d.target = Detection(
        label="疑似人员",
        bbox=[890.625, 722.2222, 902.34375, 750],
        ground_contact_visible=False,
    )
    m.remember_target(obs, d)
    with pytest.raises(HarnessError, match="zoom would crop"):
        m.ground_command(obs, proposal(obs, {"action": "zoom", "zoom_x": 3}))
    command = m.ground_command(obs, d)
    assert command["world_reference"]
    ray = candidate_ray(obs, d.target)
    aimed = obs.camera.model_copy(
        update={"yaw_deg": command["yaw_deg"], "pitch_deg": command["pitch_deg"]}
    )
    pixel = project(aimed, (0, 0, 0), ray)
    assert pixel[:2] == pytest.approx((640, 360))
    m.remember_target(
        obs.model_copy(update={"observation_id": "lost"}),
        proposal(obs, {"action": "observe"}),
    )
    assert m.ground_command(obs, d) == command


def test_world_aim_compensates_for_body_attitude():
    from uav_harness.drone4d.port import world_aim_to_gimbal

    # Arbitrary normalized attitude; independent quaternion-vector rotation
    # verifies native gimbal forward direction after world-from-body transform.
    q = [0.04, -0.02, 0.7, 0.71]
    q = [v / math.sqrt(sum(x * x for x in q)) for v in q]
    az, el = world_aim_to_gimbal(q, 284.1, -22.5)
    body = [math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)]

    def cross(a, b):
        return [
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        ]

    uv = cross(q[:3], body)
    uuv = cross(q[:3], uv)
    actual = [body[i] + 2 * q[3] * uv[i] + 2 * uuv[i] for i in range(3)]
    assert math.degrees(math.atan2(actual[0], actual[1])) % 360 == pytest.approx(284.1)
    assert math.degrees(math.asin(actual[2])) == pytest.approx(-22.5)


async def test_visible_magnified_candidate_can_move_without_feet_but_cannot_finish():
    from uav_harness.brain.contracts import Detection

    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    obs.capabilities.append("inspect_target")
    obs.camera = obs.camera.model_copy(
        update={"zoom_x": 2, "yaw_deg": 90, "pitch_deg": -20}
    )
    policy = Policy(
        allow_candidate_inspection=True,
        inspection_step_m=15,
        stand_off_m=30,
        max_step_m=30,
    )
    m = Mission(MissionRequest(query="接近", vehicle_id="v"), port, None, policy)
    m.resolved_objective = "approach"
    d = proposal(obs, {"action": "inspect_target", "distance_m": 15})
    d.target = Detection(
        label="疑似人员", bbox=[490, 490, 510, 510], ground_contact_visible=False
    )
    m.remember_target(obs, d)
    cmd = m.ground_command(obs, d)
    assert cmd["offset_ned"] == pytest.approx((0, 15, 0))
    await port.execute(cmd)
    assert port.position == pytest.approx([0, 15, -50])
    assert m.target["geometry"] is None
    with pytest.raises(HarnessError):
        m.finish("found")
    m.policy.allow_candidate_inspection = False
    with pytest.raises(HarnessError, match="enabled sandbox policy"):
        m.ground_command(obs, d)
    m.policy.allow_candidate_inspection = True
    obs.camera.zoom_x = 1
    with pytest.raises(HarnessError, match="magnify"):
        m.ground_command(obs, d)
    obs.camera.zoom_x = 2
    d.target = None
    with pytest.raises(HarnessError, match="current-image"):
        m.ground_command(obs, d)


async def test_inspection_standoff_horizon_and_fence_bounds():
    from uav_harness.brain.contracts import Detection
    from uav_harness.brain.geometry import inspection_offset

    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    obs.capabilities.append("inspect_target")
    obs.camera = obs.camera.model_copy(update={"zoom_x": 2, "pitch_deg": -1})
    policy = Policy(allow_candidate_inspection=True, stand_off_m=30, max_radius_m=5)
    target = Detection(
        label="candidate", bbox=[490, 490, 510, 510], ground_contact_visible=False
    )
    with pytest.raises(HarnessError, match="ray unsuitable"):
        inspection_offset(obs, target, policy, 15)
    obs.camera.pitch_deg = -65
    with pytest.raises(HarnessError, match="too close"):
        inspection_offset(obs, target, policy, 15)
    obs.camera.pitch_deg = -20
    m = Mission(MissionRequest(query="接近", vehicle_id="v"), port, None, policy)
    m.resolved_objective = "approach"
    d = proposal(obs, {"action": "inspect_target", "distance_m": 15})
    d.target = target
    m.remember_target(obs, d)
    with pytest.raises(HarnessError, match="local radius"):
        m.ground_command(obs, d)
    policy.max_radius_m = 100
    m.resolved_objective = "find"
    with pytest.raises(HarnessError, match="approach mission"):
        m.ground_command(obs, d)
