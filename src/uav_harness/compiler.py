"""DeepSeek proposes typed action plans; the Harness owns execution."""
import asyncio
import json
import time
import os
import httpx
from .contracts import Plan
from .errors import HarnessError
from .model_stream import read_content


DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
COMPILATION_DEADLINE_S = 180


def planning_context(harness):
    return [{"vehicle_id": s.config.vehicle_id, "backend": s.config.backend,
             "profile": s.config.profile, "available_actions": sorted(s.actions),
             "max_height_m": s.config.max_height_m,
             "max_relative_distance_m": s.config.max_relative_distance_m}
            for s in harness.sessions.values()]


class DeepSeekCompiler:
    def __init__(self, http_transport=None):
        self.http_transport = http_transport

    async def compile(self, query, harness, on_progress=None):
        context = planning_context(harness)
        prompt = """Translate the user's intent into one JSON object matching the supplied Plan schema.
Use only the listed vehicles and their available_actions. Do not invent MAVLink command IDs.
Actions on each vehicle must be dependency-ordered. Dependencies refer to action_id.
For flight.takeoff, altitude_home_m is metres above Home. For flight.move_relative:
local_enu x=east,y=north,z=up; body_flu x=forward,y=left,z=up, frozen at dispatch.
For model_bench use bench.takeoff_probe only for an explicitly requested bench test;
it proves command/output telemetry and performs land/disarm cleanup, not actual flight.
bench.takeoff_probe includes arming; never prepend vehicle.arm to that probe.
If the request needs unavailable capabilities, return {"error":"reason"}.
Never output parameter writes, force arm/disarm, raw code or shell commands.
The output is a proposal; actual readiness and completion are decided by the Harness.
"""
        raw = await self.propose(query, prompt+json.dumps({"vehicles": context, "schema": Plan.model_json_schema()}, ensure_ascii=False), on_progress)
        try:
            plan = Plan.model_validate(raw)
            plan.query = query
            harness.validate(plan)
            return plan
        except ValueError as exc:
            raise HarnessError(f"DeepSeek proposal invalid: {type(exc).__name__}") from None

    async def propose(self, query, prompt, on_progress=None):
        key = os.getenv("DEEPSEEK_API_KEY")
        # This compiler only uses the user's official DeepSeek account. Inherited
        # gateway URLs and USTC credentials must never redirect its requests.
        endpoint = DEEPSEEK_ENDPOINT
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
        effort = os.getenv("DEEPSEEK_REASONING_EFFORT", "low")
        if effort not in ("low", "high", "max", "none"):
            raise HarnessError("DEEPSEEK_REASONING_EFFORT must be low/high/max/none")
        if not key:
            raise HarnessError("DEEPSEEK_API_KEY is not configured")
        body = {"model": model, "temperature": 0, "max_tokens": 4096, "stream": True,
                "thinking": {"type": "disabled" if effort == "none" else "enabled"},
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": prompt},
                             {"role": "user", "content": query}]}
        if effort != "none":
            body["reasoning_effort"] = effort
        started, last_report = time.monotonic(), [None, 0]

        def report(phase):
            now = time.monotonic()
            if on_progress and (last_report[0] != phase or now-last_report[1] >= .25):
                last_report[:] = [phase, now]
                on_progress({"phase": phase, "elapsed_s": round(now-started, 1)})

        try:
            async with asyncio.timeout(COMPILATION_DEADLINE_S):
                report("connecting")
                async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5),
                                             follow_redirects=False, trust_env=False,
                                             transport=self.http_transport) as client:
                    async with client.stream("POST", endpoint+"/chat/completions", json=body,
                                             headers={"Authorization": "Bearer "+key}) as response:
                        if response.status_code != 200:
                            raise HarnessError(f"DeepSeek request failed: HTTP {response.status_code}")
                        report("waiting")
                        content = await read_content(response, report)
                        raw = json.loads(content)
            if isinstance(raw, dict) and "error" in raw:
                raise HarnessError("intent requires clarification or unavailable capability")
            return raw
        except HarnessError:
            raise
        except TimeoutError:
            raise HarnessError(f"DeepSeek generation deadline exceeded ({COMPILATION_DEADLINE_S}s); no plan generated") from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise HarnessError(f"DeepSeek proposal invalid: {type(exc).__name__}") from None
