"""The demo parses semantics through DeepSeek but never creates flight jobs."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from uav_harness.api import create_app
from uav_harness.compiler import DeepSeekCompiler
from uav_harness.dashboard import offline_settings


def messages(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def test_search_demo_uses_official_deepseek_and_keeps_flight_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'demo-test-key')
    monkeypatch.setenv('DEEPSEEK_BASE_URL', 'https://unwanted.invalid')
    observed = []

    def respond(request):
        observed.append(request)
        intent = {'task': 'search_and_observe_person', 'target': {'category': 'person', 'attribute': 'wearing_red_hat'}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(intent)}}]})

    with TestClient(create_app(offline_settings(tmp_path), DeepSeekCompiler(httpx.MockTransport(respond)), offline=True)) as client:
        response = client.post('/v1/demo/preview-stream', json={'query': '帮我找一个戴着红帽子的人'})
        assert response.status_code == 200
        done = messages(response)[-1]
        assert done['type'] == 'done'
        assert done['executed'] is False
        assert done['demo']['profile']['search_altitude_m'] == 50
        assert done['demo']['profile']['scan_step_deg'] == 30
        assert done['demo']['provenance'] == {'intent': 'deepseek', 'vision': 'preset', 'telemetry': 'simulated'}
        assert client.get('/v1/dashboard').json()['jobs'] == []
        assert all(v['max_height_m'] <= 20 for v in client.get('/v1/dashboard').json()['planning_context'])
        assert not client.app.state.harness.simulators
    assert str(observed[0].url) == 'https://api.deepseek.com/chat/completions'
    assert json.loads(observed[0].content)['stream'] is True


@pytest.mark.parametrize('output', [
    {'error': 'unsupported task'},
    {'task': 'search_and_observe_person', 'target': {'category': 'person', 'attribute': 'blue_coat'}},
    {'task': 'search_and_observe_person', 'target': {'category': 'person', 'attribute': 'wearing_red_hat'}, 'actions': ['arm']},
])
def test_search_demo_does_not_accept_other_tasks_or_raw_actions(tmp_path, monkeypatch, output):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'demo-test-key')
    def respond(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    with TestClient(create_app(offline_settings(tmp_path), DeepSeekCompiler(httpx.MockTransport(respond)), offline=True)) as client:
        response = client.post('/v1/demo/preview-stream', json={'query': 'test'})
        assert messages(response)[-1]['type'] == 'error'
        assert client.get('/v1/dashboard').json()['jobs'] == []


def test_search_demo_rejects_execution_and_empty_query(tmp_path):
    with TestClient(create_app(offline_settings(tmp_path), offline=True)) as client:
        assert client.post('/v1/demo/preview-stream', json={'query': '红帽子', 'execute': True}).status_code == 409
        assert client.post('/v1/demo/preview-stream', json={'query': '   '}).status_code == 422
