import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from uav_harness import compiler as compiler_module
from uav_harness.api import create_app
from uav_harness.compiler import DeepSeekCompiler
from uav_harness.dashboard import offline_settings
from uav_harness.errors import HarnessError
from uav_harness.runtime import Harness


class WireStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def content():
    return json.dumps({'actions': [{'action_id': 'arm', 'vehicle_id': 'ap-1', 'action': 'vehicle.arm', 'params': {}}]})


def event(delta=None, finish=None):
    return ('data: '+json.dumps({'choices': [{'delta': delta or {}, 'finish_reason': finish}]})+'\n\n').encode()


@pytest.fixture
async def offline(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'stream-test-key')
    h = Harness(offline_settings(tmp_path))
    yield h
    await h.close()


async def test_stream_waits_through_heartbeats_and_accumulates_only_final_content(offline):
    text = content()
    stream = WireStream([b': keep-alive\n\n', event({'reasoning_content': 'private-thought-test'}), event({'content': text[:20]}), event({'content': text[20:]}), event(finish='stop'), b'data: [DONE]\n\n'], .005)
    requests, progress = [], []
    def reply(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    plan = await DeepSeekCompiler(httpx.MockTransport(reply)).compile('解锁ap-1', offline, on_progress=progress.append)
    assert plan.actions[0].action == 'vehicle.arm'
    assert json.loads(requests[0].content)['stream'] is True
    assert any(p['phase'] == 'waiting' for p in progress)
    assert any(p['phase'] == 'generating' for p in progress)
    assert 'private-thought-test' not in json.dumps(progress)
    assert stream.closed
    assert offline.jobs == {} and offline.simulators == {}


async def test_stream_eof_is_not_a_completed_plan(offline):
    stream = WireStream([event({'content': content()})])
    def reply(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    with pytest.raises(HarnessError, match='before completion'):
        await DeepSeekCompiler(httpx.MockTransport(reply)).compile('解锁', offline)


async def test_output_token_limit_is_reported_as_truncation(offline):
    stream = WireStream([event({'content': content()}), event(finish='length'), b'data: [DONE]\n\n'])
    def reply(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    with pytest.raises(HarnessError, match='truncated'):
        await DeepSeekCompiler(httpx.MockTransport(reply)).compile('解锁', offline)


async def test_total_deadline_stops_even_an_active_heartbeat_stream(offline, monkeypatch):
    monkeypatch.setattr(compiler_module, 'COMPILATION_DEADLINE_S', .04, raising=False)
    stream = WireStream([b': keep-alive\n\n']*100, .01)
    def reply(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    with pytest.raises(HarnessError, match='deadline'):
        await DeepSeekCompiler(httpx.MockTransport(reply)).compile('解锁', offline)
    assert stream.closed


def test_preview_stream_delivers_status_and_done_without_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'stream-test-key')
    def reply(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=WireStream([b': keep-alive\n\n', event({'content': content()}), event(finish='stop'), b'data: [DONE]\n\n']))
    with TestClient(create_app(offline_settings(tmp_path), DeepSeekCompiler(httpx.MockTransport(reply)), offline=True)) as client:
        response = client.post('/v1/intents/preview-stream', json={'query': '解锁ap-1'}, headers={'Idempotency-Key': 'stream-preview'})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert messages[0]['type'] == 'status'
        assert messages[-1]['type'] == 'done'
        assert messages[-1]['plan']['actions'][0]['vehicle_id'] == 'ap-1'
        assert client.get('/v1/dashboard').json()['jobs'] == []
        assert client.post('/v1/intents/preview-stream', json={'query': '解锁', 'execute': True}, headers={'Idempotency-Key': 'forbidden-stream'}).status_code == 409


async def test_sse_metadata_overhead_does_not_count_as_plan_size(offline):
    chunks = []
    for char in content():
        chunks.append(('data: '+json.dumps({'id': 'metadata-'+'x'*6000, 'choices': [{'delta': {'content': char}, 'finish_reason': None}]})+'\n\n').encode())
    chunks.extend([event(finish='stop'), b'data: [DONE]\n\n'])
    def reply(request):
        body = json.loads(request.content)
        assert body['reasoning_effort'] == 'low'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=WireStream(chunks))
    plan = await DeepSeekCompiler(httpx.MockTransport(reply)).compile('解锁', offline)
    assert plan.actions[0].vehicle_id == 'ap-1'
