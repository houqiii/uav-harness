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
        assert 140 <= math.dist(point[:2], [-31.9, -337.1]) <= 165
        # Initial yaw is east (90 degrees); keep every seed well outside that view.
        bearing = math.degrees(math.atan2(point[0] + 31.9, point[1] + 337.1)) % 360
        assert abs((bearing - 90 + 180) % 360 - 180) > 60
        assert not scene["tasks"]
        positions.add(tuple(point[:2]))
    assert len(positions) > 1
    assert search_scene(tmp_path, 17) == search_scene(tmp_path, 17)


@pytest.mark.parametrize("failure", [InvalidDecision, RetryablePlannerError])
async def test_invalid_vlm_json_recovers_with_fresh_observation_without_dispatch(failure):
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
    m = Mission(MissionRequest(query="找到后接近", vehicle_id="v"), port, None, Policy())
    m.resolved_objective = "approach"
    m.target = {
        "visible": True, "confirmed_observations": 2,
        "geometry": {"position_ned": [100, 0, 0], "horizontal_distance_m": 100, "uncertainty_m": 2},
    }
    assert not m.scanned
    command = m.ground_command(obs, proposal(obs, {"action": "approach", "step_m": 10}))
    assert command["action"] == "move"
    assert command["offset_ned"] == pytest.approx((10, 0, 0))
