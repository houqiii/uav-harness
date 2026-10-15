"""Bounded SSE content reader shared by model providers; no provider requests here."""

import json
from .errors import HarnessError

MAX_RESPONSE_BYTES = 524288
MAX_STREAM_BYTES = 8388608


async def read_content(response, report):
    """Consume API keepalives without treating them as output or resetting the total deadline."""
    if "text/event-stream" not in response.headers.get("content-type", ""):
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise HarnessError("Model response exceeds size limit")
        wire = json.loads(payload)
        choice = wire["choices"][0]
        if choice.get("finish_reason") == "length":
            raise HarnessError(
                "Model output truncated by token limit; no plan generated"
            )
        return choice["message"]["content"]

    parts, size, content_size, finished = [], 0, 0, False
    async for line in response.aiter_lines():
        size += len(line.encode("utf-8"))
        if size > MAX_STREAM_BYTES:
            raise HarnessError("Model response exceeds size limit")
        if line.startswith(":"):
            report("waiting")
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        if not payload:
            continue
        event = json.loads(payload)
        if "error" in event:
            raise HarnessError("Model stream failed; response body withheld")
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            if delta.get("reasoning_content"):
                report(
                    "reasoning"
                )  # State only; private reasoning is never stored or sent to the UI.
            if delta.get("content"):
                content_size += len(delta["content"].encode("utf-8"))
                if content_size > MAX_RESPONSE_BYTES:
                    raise HarnessError("Model response exceeds size limit")
                parts.append(delta["content"])
                report("generating")
            reason = choice.get("finish_reason")
            if reason == "length":
                raise HarnessError(
                    "Model output truncated by token limit; no plan generated"
                )
            if reason == "stop":
                finished = True
            elif reason is not None:
                raise HarnessError("Model did not finish a complete plan")
    if not finished:
        raise HarnessError("Model stream ended before completion; no plan generated")
    return "".join(parts)
