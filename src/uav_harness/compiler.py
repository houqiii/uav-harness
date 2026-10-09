"""DeepSeek proposes typed action plans; the Harness owns execution."""
import json
import os
from urllib.parse import urlsplit
import httpx
from .contracts import Plan
from .errors import HarnessError


class DeepSeekCompiler:
    def __init__(self, http_transport=None):
        self.http_transport = http_transport

    async def compile(self, query, harness):
        key = os.getenv("DEEPSEEK_API_KEY")
        endpoint = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        parsed = urlsplit(endpoint)
        if not key:
            raise HarnessError("DEEPSEEK_API_KEY is not configured")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HarnessError("DeepSeek endpoint must be a plain HTTPS base URL")
        context = [{"vehicle_id": s.config.vehicle_id, "backend": s.config.backend,
                    "profile": s.config.profile, "available_actions": sorted(s.actions),
                    "max_height_m": s.config.max_height_m,
                    "max_relative_distance_m": s.config.max_relative_distance_m}
                   for s in harness.sessions.values()]
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
        body = {"model": model, "temperature": 0, "max_tokens": 4096,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": prompt+json.dumps({"vehicles": context, "schema": Plan.model_json_schema()}, ensure_ascii=False)},
                             {"role": "user", "content": query}]}
        try:
            async with httpx.AsyncClient(timeout=45, transport=self.http_transport) as client:
                response = await client.post(endpoint+"/chat/completions", json=body,
                                             headers={"Authorization": "Bearer "+key})
                if response.status_code != 200:
                    raise HarnessError(f"DeepSeek request failed: HTTP {response.status_code}")
                raw = json.loads(response.json()["choices"][0]["message"]["content"])
            if isinstance(raw, dict) and "error" in raw:
                raise HarnessError("intent requires clarification or unavailable capability")
            plan = Plan.model_validate(raw)
            plan.query = query
            harness.validate(plan)
            return plan
        except HarnessError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise HarnessError(f"DeepSeek proposal invalid: {type(exc).__name__}") from None

