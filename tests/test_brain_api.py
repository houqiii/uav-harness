import json
import time
import httpx
from fastapi.testclient import TestClient
from uav_harness.api import create_app
from uav_harness.dashboard import offline_settings
from uav_harness.brain.provider import QwenPlanner


def test_online_api_runs_real_multimodal_loop_without_mavlink_jobs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "brain-api-test")
    seen = []

    def reply(req):
        body = json.loads(req.content)
        text = json.loads(body["messages"][-1]["content"][0]["text"])
        obs = text["observation"]
        seen.append(obs)
        action = (
            {"action": "takeoff", "altitude_m": 30}
            if len(seen) == 1
            else {"action": "finish", "outcome": "needs_operator"}
        )
        d = {
            "observation_id": obs["observation_id"],
            "task_goal": "find",
            "task_interpretation": "观察蓝色目标",
            "scene_summary": "根据新图像决定",
            "plan": ["起飞观察", "反馈后重新规划"],
            "next_action": action,
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(d)}}
                ]
            },
        )

    with TestClient(
        create_app(
            offline_settings(tmp_path),
            offline=True,
            planner=QwenPlanner(httpx.MockTransport(reply)),
        )
    ) as client:
        config = client.get("/v1/brain").json()
        assert config["mode"] == "geometric_simulation"
        response = client.post(
            "/v1/brain/missions", json={"query": "观察蓝色目标", "vehicle_id": "px4-1"}
        )
        assert response.status_code == 202
        mid = response.json()["mission_id"]
        for _ in range(200):
            state = client.get("/v1/brain/missions/" + mid).json()
            if state["status"] == "needs_operator":
                break
            time.sleep(0.02)
        assert state["status"] == "needs_operator"
        assert len(seen) == 2 and seen[1]["telemetry"]["position_ned"][2] == -30
        assert seen[0]["scan_headings_deg"] == []
        assert len(seen[1]["scan_headings_deg"]) == 1
        assert seen[1]["accepted_task_goal"] == "find"
        assert state["observation"]["image_url"].startswith("/v1/brain/")
        image = client.get(state["observation"]["image_url"])
        assert (
            image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        )
        assert client.get("/v1/dashboard").json()["jobs"] == []
        assert not client.app.state.harness.simulators
        assert (
            client.post("/v1/demo/preview-stream", json={"query": "红帽子"}).status_code
            == 410
        )


def test_brain_apis_require_auth_but_static_ui_stays_public(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_API_TOKEN", "brain-auth")
    with TestClient(create_app(offline_settings(tmp_path), offline=True)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/v1/brain").status_code == 401
        assert (
            client.post(
                "/v1/brain/missions", json={"query": "找人", "vehicle_id": "px4-1"}
            ).status_code
            == 401
        )


def test_missing_key_does_not_fabricate_vlm_and_simulated_preview_still_works(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with TestClient(create_app(offline_settings(tmp_path), offline=True)) as client:
        assert not client.get("/v1/brain").json()["provider"]["configured"]
        assert (
            client.post(
                "/v1/brain/missions", json={"query": "找人", "vehicle_id": "px4-1"}
            ).status_code
            == 409
        )
        response = client.post("/v1/brain/preview", json={"vehicle_id": "px4-1"})
        assert response.status_code == 200
        assert response.json()["source"] == "geometric_simulation"
        first = response.json()
        before = client.get(first["image_url"]).content
        client.app.state.brain.ports["px4-1"].position = [0, 0, -30]
        second = client.post("/v1/brain/preview", json={"vehicle_id": "px4-1"}).json()
        assert first["image_url"] != second["image_url"]
        assert before != client.get(second["image_url"]).content
