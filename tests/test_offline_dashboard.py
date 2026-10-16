import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import mock_settings
from uav_harness.api import create_app
from uav_harness.compiler import DeepSeekCompiler
from uav_harness.journal import Journal
from uav_harness.transport import MAVLinkTransport


def motor_plan():
    return {'actions': [{'action_id': 'm', 'vehicle_id': 'model-1', 'action': 'bench.motor_test', 'params': {'duration_s': 1}}]}


def no_links(monkeypatch):
    async def fail(self):
        raise AssertionError('offline dashboard must not open any MAVLink link')
    monkeypatch.setattr(MAVLinkTransport, 'open', fail)


def test_offline_starts_without_devices_and_cannot_execute(tmp_path, monkeypatch):
    no_links(monkeypatch)
    settings = mock_settings(tmp_path)
    settings.links[0].kind = 'serial'
    settings.links[0].device = '/not/connected'
    with TestClient(create_app(settings, offline=True)) as client:
        assert client.get('/').status_code == 200
        state = client.get('/v1/dashboard').json()
        assert state['mode'] == 'offline'
        assert not state['control_enabled']
        assert all(not v['connected'] and not v['control_enabled'] for v in state['vehicles'])
        assert all(v['heartbeat'] is None and v['servo_output'] is None for v in state['vehicles'])
        assert client.post('/v1/plans/validate', json=motor_plan()).status_code == 200
        headers = {'Idempotency-Key': 'offline-no-execution'}
        for path, body in [('/v1/plans', motor_plan()), ('/v1/actions', motor_plan()['actions'][0]), ('/v1/intents', {'query': '测试电机', 'execute': True})]:
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == 409
            assert 'offline' in response.json()['error']
        assert client.get('/v1/dashboard').json()['jobs'] == []
        assert not client.app.state.harness.simulators
    assert settings.allow_control  # no mutation of the caller's real-flight configuration


def test_real_compiler_produces_dependency_ordered_plan_without_telemetry(tmp_path, monkeypatch):
    no_links(monkeypatch)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-upstream-test')
    observed = []
    actions = [
        {'action_id': 'arm', 'vehicle_id': 'ap-1', 'action': 'vehicle.arm'},
        {'action_id': 'takeoff', 'vehicle_id': 'ap-1', 'action': 'flight.takeoff', 'depends_on': ['arm'], 'params': {'altitude_home_m': 2}},
        {'action_id': 'forward', 'vehicle_id': 'ap-1', 'action': 'flight.move_relative', 'depends_on': ['takeoff'], 'params': {'frame': 'body_flu', 'x_m': 3, 'y_m': 0, 'z_m': 0}},
        {'action_id': 'land', 'vehicle_id': 'ap-1', 'action': 'flight.land', 'depends_on': ['forward']},
    ]
    def reply(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'actions': actions})}}]})
    compiler = DeepSeekCompiler(httpx.MockTransport(reply))
    with TestClient(create_app(mock_settings(tmp_path), compiler, offline=True)) as client:
        response = client.post('/v1/intents', json={'query': 'ap-1 起飞2米，向机头前进3米，然后降落', 'execute': False}, headers={'Idempotency-Key': 'reasoning-preview'})
        assert response.status_code == 200, response.text
        plan = response.json()['plan']
        assert plan['actions'][2]['params']['frame'] == 'body_flu'
        assert plan['actions'][3]['depends_on'] == ['forward']
        assert not response.json()['executed']
        assert 'flight.move_relative' in observed[0]['messages'][0]['content']
        assert client.get('/v1/dashboard').json()['jobs'] == []
        assert not client.app.state.harness.simulators


def test_offline_does_not_touch_real_job_journal(tmp_path, monkeypatch):
    no_links(monkeypatch)
    real_dir = tmp_path/'hardware'
    real_dir.mkdir()
    journal = Journal(real_dir/'jobs.sqlite3')
    job_id, _ = journal.create('hardware-pending', motor_plan())
    journal.close()
    config = mock_settings(real_dir)
    with TestClient(create_app(config, offline=True)) as client:
        assert client.app.state.harness.settings.state_dir != real_dir
        assert client.get('/v1/dashboard').json()['jobs'] == []
    import sqlite3
    with sqlite3.connect(real_dir/'jobs.sqlite3') as db:
        assert db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0] == 'queued'


@pytest.mark.parametrize('extra_args', [[], ['--offline'], ['--no-sandbox', '--offline']])
def test_launcher_opens_preview_without_usb(tmp_path, monkeypatch, extra_args):
    from uav_harness import dashboard
    no_links(monkeypatch)
    from serial.tools import list_ports
    monkeypatch.setattr(list_ports, 'comports', lambda: [])
    monkeypatch.setattr(sys, 'argv', ['dashboard', *extra_args, '--state-dir', str(tmp_path)])
    served = []
    def serve(app, **kwargs):
        with TestClient(app) as client:
            state = client.get('/v1/dashboard').json()
            assert state['mode'] == 'offline'
            assert {'ardupilot', 'px4'} <= {v['backend'] for v in state['vehicles']}
            assert any('flight.move_relative' in v['actions'] for v in state['vehicles'])
            enabled = '--no-sandbox' not in extra_args
            assert client.get('/v1/sandbox').json()['enabled'] is enabled
            brain = client.get('/v1/brain').json()
            assert brain['mode'] == ('hardware_disconnected' if enabled else 'geometric_simulation')
            assert all(v['execution_enabled'] is not enabled for v in brain['vehicles'])
            served.append(state)
    monkeypatch.setattr(dashboard.uvicorn, 'run', serve)
    dashboard.main()
    assert len(served) == 1
