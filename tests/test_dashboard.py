import math
import time

import pytest
from fastapi.testclient import TestClient
from conftest import mock_settings
from uav_harness.api import create_app
from uav_harness.transport import mav


def test_dashboard_serves_packaged_ui_and_fresh_vehicle_data(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        page = client.get('/')
        assert page.status_code == 200 and '指令工作台' in page.text
        assert client.get('/assets/app.js').status_code == 200
        data = client.get('/v1/dashboard').json()
        assert data['mode'] == 'simulator'
        assert data['compiler']['configured'] is False
        assert data['jobs'] == []
        model = next(v for v in data['vehicles'] if v['vehicle_id'] == 'model-1')
        assert model['servo_output']['servo1_raw'] == 1051
        assert model['gimbals'] == []


def test_ui_can_load_without_token_but_data_stays_authenticated(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_API_TOKEN', 'dashboard-test')
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/v1/dashboard').status_code == 401
        response = client.get('/v1/dashboard', headers={'Authorization': 'Bearer dashboard-test'})
        assert response.status_code == 200
        assert 'dashboard-test' not in response.text


async def test_payload_component_on_same_system_does_not_become_flight_controller(harness):
    session = harness.sessions['model-1']
    sim = harness.simulators['model-1']
    gimbal = mav.MAVLink(sim, srcSystem=session.config.system_id, srcComponent=154)
    angle = math.radians(-30)
    gimbal.gimbal_device_attitude_status_send(0, 0, 1, 32, [math.cos(angle/2), 0, math.sin(angle/2), 0], 0, 0, 0, 0)
    gimbal.video_stream_information_send(1, 1, 0, 1, 30, 1920, 1080, 1000000, 0, 80, b'camera', b'rtsp://192.168.1.2/live')
    import asyncio
    async with asyncio.timeout(2):
        while not session.snapshot()['gimbals']:
            await asyncio.sleep(.01)
        while not session.snapshot()['video_streams']:
            await asyncio.sleep(.01)
    snapshot = session.snapshot()
    device = snapshot['gimbals'][0]
    assert device['component_id'] == 154 and device['connected']
    assert device['attitude_deg']['pitch'] == pytest.approx(-30, abs=.01)
    assert device['yaw_frame'] == 'vehicle'
    assert snapshot['video_streams'][0]['uri'] == 'rtsp://192.168.1.2/live'
    assert session.get('HEARTBEAT').get_srcComponent() == 1
    assert harness.sessions['ap-1'].snapshot()['gimbals'] == []


async def test_payload_stale_and_invalid_quaternions_are_not_valid_angles(harness):
    session = harness.sessions['model-1']
    from uav_harness.payload import PayloadTelemetry
    collector = PayloadTelemetry(session.config.system_id, session.settings)
    msg = mav.MAVLink_gimbal_device_attitude_status_message(0, 0, 1, 32, [0, 0, 0, 0], 0, 0, 0, 0)
    msg._header.srcSystem = session.config.system_id
    msg._header.srcComponent = 154
    collector.receive(msg, time.monotonic())
    assert collector.snapshot()['gimbals'][0]['attitude_deg'] is None
    collector.receive(msg, time.monotonic()-10)
    device = collector.snapshot()['gimbals'][0]
    assert not device['connected'] and device['attitude_deg'] is None
    msg._header.srcSystem = 99
    collector.receive(msg, time.monotonic())
    assert not collector.snapshot()['gimbals'][0]['connected']


def test_dashboard_recovers_recent_persisted_jobs(tmp_path):
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        plan = {'query': 'test', 'actions': [{'action_id': 'm', 'vehicle_id': 'model-1', 'action': 'bench.motor_test', 'params': {'duration_s': .2}}]}
        response = client.post('/v1/plans', json=plan, headers={'Idempotency-Key': 'ui-plan'})
        assert response.status_code == 202
        job = client.get('/v1/dashboard').json()['jobs'][0]
        assert job['job_id'] == response.json()['job_id']
        assert job['plan'] == response.json()['plan']
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if client.get('/v1/jobs/'+job['job_id']).json()['status'] == 'succeeded':
                break
            time.sleep(.02)
        else:
            pytest.fail('motor cleanup did not finish')
    with TestClient(create_app(mock_settings(tmp_path))) as client:
        recovered = client.get('/v1/dashboard').json()['jobs'][0]
        assert recovered['job_id'] == job['job_id']
        assert recovered['status'] == 'succeeded'
        assert not any(c['command'] == 209 for c in client.app.state.harness.simulators['model-1'].commands)


def test_ui_preview_and_validation_never_dispatch_and_plan_execution_does_not_recompile(tmp_path, monkeypatch):
    import httpx
    import json
    from uav_harness.compiler import DeepSeekCompiler
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-upstream-only')
    calls = []

    def respond(request):
        calls.append(request)
        plan = {'actions': [{'action_id': 'm', 'vehicle_id': 'model-1', 'action': 'bench.motor_test', 'params': {'duration_s': .2}}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(plan)}}]})

    with TestClient(create_app(mock_settings(tmp_path), DeepSeekCompiler(httpx.MockTransport(respond)))) as client:
        preview = client.post('/v1/intents', json={'query': '模型机单电机测试', 'execute': False}, headers={'Idempotency-Key': 'ui-preview'}).json()
        checked = client.post('/v1/plans/validate', json=preview['plan'])
        assert checked.status_code == 200
        assert client.get('/v1/dashboard').json()['jobs'] == []
        sim = client.app.state.harness.simulators['model-1']
        assert not any(c['command'] == 209 for c in sim.commands)
        executed = client.post('/v1/plans', json=preview['plan'], headers={'Idempotency-Key': 'ui-execute'})
        assert executed.status_code == 202
        assert executed.json()['plan'] == preview['plan']
        assert len(calls) == 1
        replay = client.post('/v1/plans', json=preview['plan'], headers={'Idempotency-Key': 'ui-execute'})
        assert replay.json()['job_id'] == executed.json()['job_id']
