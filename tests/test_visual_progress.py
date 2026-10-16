"""The model chooses actions; the harness preserves coordinate/tool contracts."""

import json

import httpx
import pytest

from uav_harness.brain.contracts import GimbalControl, Policy
from uav_harness.brain.provider import QwenPlanner, parse_decision, response_schema
from uav_harness.brain.simulation import SimulatedPort
from test_brain import decision


async def view():
    port = SimulatedPort("v", seed=1)
    port.position = [0, 0, -50]
    obs = await port.observe()
    obs.capabilities += ["zoom", "inspect_target"]
    return obs


@pytest.mark.parametrize(
    "action",
    [
        {"action": "aim_target"},
        {"action": "zoom", "zoom_x": 3.7},
        {"action": "inspect_target", "distance_m": 7},
    ],
)
async def test_one_frame_produces_one_unmodified_model_decision(monkeypatch, action):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    obs = await view()
    obs.gimbal_control = GimbalControl(
        yaw_deg=130,
        pitch_deg=-15,
        yaw_limits_deg=(-160, 160),
        pitch_limits_deg=(-90, 30),
        source="device_spec_and_telemetry",
    )
    requests = []
    # Even repeated camera actions remain the VLM's choice, visible in history.
    history = [
        {
            "decision": {"next_action": {"action": "aim_target"}},
            "result": {"status": "succeeded", "telemetry": obs.telemetry.model_dump()},
        }
    ] * 6

    def reply(request):
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        context = json.loads(body["messages"][1]["content"][0]["text"])
        assert context["observation"][
            "gimbal_control"
        ] == obs.gimbal_control.model_dump(mode="json")
        assert context["observation"]["camera"] == obs.camera.model_dump(mode="json")
        assert context["observation"]["telemetry"] == obs.telemetry.model_dump(
            mode="json"
        )
        requests.append(context)
        d = decision(obs, action, {"label": "候选", "bbox": [507, 490, 517, 510]})
        raw = d.model_dump()
        raw["target"]["bbox_coordinate_space"] = "normalized_1000"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(raw)}}
                ]
            },
        )

    result = await QwenPlanner(httpx.MockTransport(reply)).decide(
        "寻找并接近指定目标",
        obs,
        Policy(allow_candidate_inspection=True),
        history,
        None,
        lambda p: None,
    )
    assert result.next_action.model_dump() == action
    assert result.target.bbox == (507, 490, 517, 510)
    assert len(requests) == 1
    assert "proposal_feedback" not in requests[0]
    facts = requests[0]["active_perception_feedback"]
    assert facts["previous_aim_completed"]
    assert facts["consecutive_camera_only_actions"] == 6
    assert "next_step_if_current_candidate_is_still_visible" not in facts
    assert "max_zoom" not in json.dumps(requests[0])


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080), (720, 1280)])
async def test_normalized_model_bbox_is_not_scaled_again_at_different_resolutions(
    width, height
):
    obs = await view()
    obs.camera.width, obs.camera.height = width, height
    raw = decision(
        obs,
        {"action": "aim_target"},
        {"label": "candidate", "bbox": [695, 465, 705, 475]},
    ).model_dump()
    raw["target"]["bbox_coordinate_space"] = "normalized_1000"
    parsed = parse_decision(json.dumps(raw), obs)
    assert parsed.target.bbox == (695, 465, 705, 475)
    schema = response_schema()["$defs"]["Detection"]["properties"][
        "bbox_coordinate_space"
    ]
    assert schema["enum"] == ["normalized_1000"]
