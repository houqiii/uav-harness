"""Camera proposals must advance perception without inventing a flight plan."""

import json
import time

import httpx
import pytest

from uav_harness.brain.contracts import Detection, Policy
from uav_harness.brain.provider import QwenPlanner, visual_proposal_feedback
from uav_harness.brain.simulation import SimulatedPort
from uav_harness.errors import HarnessError
from test_brain import decision


async def view(zoom=1):
    p = SimulatedPort("v", seed=1)
    p.position = [0, 0, -50]
    obs = await p.observe()
    obs.camera.zoom_x = zoom
    obs.camera.pitch_deg = -20
    obs.capabilities += ["zoom", "inspect_target"]
    return obs


def aim_history(obs):
    return [
        {
            "decision": {"next_action": {"action": "aim_target"}},
            "result": {"status": "succeeded", "telemetry": obs.telemetry.model_dump()},
        }
    ]


async def test_repeat_aim_is_unnecessary_when_imperfectly_centred_box_can_be_magnified():
    obs = await view()
    target = Detection(label="疑似人员", bbox=[560, 460, 570, 480])
    d = decision(obs, {"action": "aim_target"}, target.model_dump())
    feedback = visual_proposal_feedback(obs, Policy(), aim_history(obs), d)
    assert feedback["status"] == "not_dispatched"
    assert feedback["max_zoom_x_with_10_percent_frame_margin"] > 2
    # Initial off-centre detection is still allowed to centre once.
    assert visual_proposal_feedback(obs, Policy(), [], d) is None
    # Edge clipping, real aircraft displacement, and no current detection are
    # different situations and must not turn into automatic candidate motion.
    d.target.bbox = (940, 450, 950, 470)
    assert visual_proposal_feedback(obs, Policy(), aim_history(obs), d) is None
    d.target = target.model_copy(update={"bbox": (560, 460, 570, 480)})
    history = aim_history(obs)
    obs.telemetry.position_ned = (10, 0, -50)
    assert visual_proposal_feedback(obs, Policy(), history, d) is None
    d.target = None
    assert visual_proposal_feedback(obs, Policy(), history, d) is None


async def test_magnified_candidate_can_progress_without_feet_but_cannot_bypass_inspection_bounds():
    obs = await view(2)
    d = decision(
        obs,
        {"action": "aim_target"},
        {
            "label": "疑似人员",
            "bbox": [490, 490, 510, 510],
            "ground_contact_visible": False,
        },
    )
    policy = Policy(allow_candidate_inspection=True)
    feedback = visual_proposal_feedback(obs, policy, aim_history(obs), d)
    assert "inspect_target" in feedback["revision"]
    assert (
        "inspect_target"
        not in visual_proposal_feedback(obs, Policy(), aim_history(obs), d)["revision"]
    )
    obs.camera.ground_down_m = None
    assert (
        "inspect_target"
        not in visual_proposal_feedback(obs, policy, aim_history(obs), d)["revision"]
    )


async def test_qwen_repairs_duplicate_proposal_on_same_frame_instead_of_dispatching_it(
    monkeypatch,
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    obs = await view()
    requests = []

    def reply(request):
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"][0]["text"])
        requests.append(context)
        action = (
            {"action": "aim_target"}
            if len(requests) == 1
            else {"action": "zoom", "zoom_x": 2}
        )
        d = decision(obs, action, {"label": "疑似人员", "bbox": [560, 460, 570, 480]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": d.model_dump_json()},
                    }
                ]
            },
        )

    result = await QwenPlanner(httpx.MockTransport(reply)).decide(
        "找人并接近",
        obs,
        Policy(),
        aim_history(obs),
        None,
        lambda p: None,
    )
    assert result.next_action.action == "zoom"
    assert len(requests) == 2
    assert (
        requests[0]["observation"]["observation_id"]
        == requests[1]["observation"]["observation_id"]
    )
    assert requests[1]["proposal_feedback"]["status"] == "not_dispatched"
    assert requests[1]["active_perception_feedback"]["previous_aim_completed"]
    assert requests[1]["proposal_feedback"]["rejected_proposal"]["target"]["bbox"] == [
        560,
        460,
        570,
        480,
    ]


async def test_persistent_nonprogress_stops_after_bounded_repairs(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    obs = await view()
    calls = []

    def reply(request):
        calls.append(time.monotonic())
        d = decision(
            obs,
            {"action": "aim_target"},
            {"label": "疑似人员", "bbox": [560, 460, 570, 480]},
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": d.model_dump_json()},
                    }
                ]
            },
        )

    with pytest.raises(HarnessError, match="无进展"):
        await QwenPlanner(httpx.MockTransport(reply)).decide(
            "找人并接近",
            obs,
            Policy(),
            aim_history(obs),
            None,
            lambda p: None,
        )
    assert len(calls) == 3


async def test_zoom_repair_preserves_candidate_in_frame_instead_of_starting_another_aim_loop():
    obs = await view()
    d = decision(
        obs,
        {"action": "zoom", "zoom_x": 5},
        {
            "label": "疑似人员",
            "bbox": [375, 425, 385, 435],
        },
    )
    feedback = visual_proposal_feedback(obs, Policy(), aim_history(obs), d)
    assert feedback["max_zoom_x_with_10_percent_frame_margin"] < 5
    d.next_action.zoom_x = 2
    assert visual_proposal_feedback(obs, Policy(), aim_history(obs), d) is None
