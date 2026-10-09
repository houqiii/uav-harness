import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from uav_harness.api import create_app
from uav_harness.compiler import DeepSeekCompiler
from uav_harness.errors import HarnessError
from conftest import mock_settings


def arm():
    return {"action_id":"arm","vehicle_id":"model-1","action":"vehicle.arm","params":{}}


def test_openapi_and_idempotent_atomic_command_api(tmp_path):
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        assert client.get("/health").json()["qgc_required"] is False
        assert len(client.get("/v1/vehicles").json()["vehicles"])==3
        assert "/v1/actions" in client.get("/openapi.json").json()["paths"]
        assert client.post("/v1/actions",json=arm()).status_code==422
        row=client.post("/v1/actions",json=arm(),headers={"Idempotency-Key":"arm-one"})
        assert row.status_code==202,row.text
        duplicate=client.post("/v1/actions",json=arm(),headers={"Idempotency-Key":"arm-one"})
        assert duplicate.json()["job_id"]==row.json()["job_id"]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=client.get("/v1/jobs/"+row.json()["job_id"]).json()
            if job["status"] not in ("queued","running"):
                break
            time.sleep(.05)
        assert job["status"]=="succeeded",job
        cleanup={"action_id":"disarm","vehicle_id":"model-1","action":"vehicle.disarm","params":{}}
        assert client.post("/v1/actions",json=cleanup,headers={"Idempotency-Key":"disarm-one"}).status_code==202


def test_token_authentication_when_configured(tmp_path,monkeypatch):
    monkeypatch.setenv("HARNESS_API_TOKEN","test-local-token")
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        assert client.get("/health").status_code==401
        assert client.get("/health",headers={"Authorization":"Bearer test-local-token"}).status_code==200


def test_nonloopback_service_requires_token(tmp_path,monkeypatch):
    monkeypatch.delenv("HARNESS_API_TOKEN",raising=False)
    settings=mock_settings(tmp_path);settings.api_host="0.0.0.0"
    with pytest.raises(HarnessError,match="TOKEN"):
        create_app(settings)


async def test_deepseek_preview_validates_without_sending_control(harness,monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY","test-credential-only")
    observed=[]
    def respond(request):
        body=json.loads(request.content);observed.append(body)
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"query":"proposal","actions":[arm()]})}}]})
    before=sum(len(p.commands) for p in harness.simulators.values())
    plan=await DeepSeekCompiler(httpx.MockTransport(respond)).compile("解锁模型机",harness)
    assert plan.query=="解锁模型机" and plan.actions[0].action=="vehicle.arm"
    assert sum(len(p.commands) for p in harness.simulators.values())==before
    assert "available_actions" in observed[0]["messages"][0]["content"]
    assert "test-credential-only" not in json.dumps(observed)


async def test_deepseek_cannot_invent_raw_control_tools(harness,monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY","test-credential-only")
    def respond(request):
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"actions":[{"action_id":"x","vehicle_id":"model-1","action":"mavlink.raw","params":{"command":400}}]})}}]})
    with pytest.raises(HarnessError,match="invalid"):
        await DeepSeekCompiler(httpx.MockTransport(respond)).compile("解锁",harness)


async def test_provider_error_body_and_key_never_leak(harness,monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY","test-credential-only")
    def respond(request):
        return httpx.Response(401,text="test-credential-only")
    with pytest.raises(HarnessError) as error:
        await DeepSeekCompiler(httpx.MockTransport(respond)).compile("解锁",harness)
    assert "HTTP 401" in str(error.value)
    assert "test-credential-only" not in str(error.value)


def test_intent_api_to_compiler_scheduler_and_wire_with_mock_provider(tmp_path,monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY","test-credential-only")
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"actions":[arm()]})}}]})
    compiler=DeepSeekCompiler(httpx.MockTransport(respond))
    with TestClient(create_app(mock_settings(tmp_path),compiler)) as client:
        headers={"Idempotency-Key":"intent-one"}
        preview=client.post("/v1/intents",json={"query":"解锁模型机"},headers={"Idempotency-Key":"preview"})
        assert preview.status_code==200 and preview.json()["executed"] is False
        assert not any(c["command"]==400 for c in client.app.state.harness.simulators["model-1"].commands)
        execution=client.post("/v1/intents",json={"query":"解锁模型机","execute":True},headers=headers)
        assert execution.status_code==200,execution.text
        job_id=execution.json()["job"]["job_id"]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=client.get("/v1/jobs/"+job_id).json()
            if job["status"] not in ("queued","running"):
                break
            time.sleep(.05)
        assert job["status"]=="succeeded",job
        assert client.app.state.harness.simulators["model-1"].armed
        replay=client.post("/v1/intents",json={"query":"解锁模型机","execute":True},headers=headers)
        assert replay.json()["job"]["job_id"]==job_id
        assert len(calls)==2  # preview + execute; repeat intent doesn't recompile or rearm.
        mismatch=client.post("/v1/intents",json={"query":"降落模型机","execute":True},headers=headers)
        assert mismatch.status_code==409
        assert client.post("/v1/actions",json={"action_id":"d","vehicle_id":"model-1","action":"vehicle.disarm","params":{}},headers={"Idempotency-Key":"intent-cleanup"}).status_code==202

