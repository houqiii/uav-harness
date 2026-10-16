"""One owned sandbox session with a shared VLM mission registry and independent safety boundary."""

import asyncio
import math
import logging
import json
import httpx
import time
from ..brain.service import BrainService
from ..brain.provider import provider_settings
from ..brain.runner import Mission
from ..errors import HarnessError
from .capture import CaptureBroker
from .client import DroneClient
from .port import Drone4DPort
from .scene import search_scene, visual_styles
from .evaluation import EpisodeEvaluation
from ..brain.runner import TERMINAL


class Drone4DService(BrainService):
    def __init__(self, harness, planner=None, *, state_dir=None):
        super().__init__(harness, True, planner)
        self.config = harness.settings.drone4d
        self.client = DroneClient(self.config, state_dir or harness.settings.state_dir)
        self.capture = CaptureBroker(self.config.capture_timeout_s)
        self.state = "disconnected"
        self.error = None
        self.info = {}
        self.remote_meta = {}
        self.connect_task = None
        self.poll_task = None
        self._closing = False
        self.lifecycle_lock = asyncio.Lock()
        self._last_poll_error = None
        self.bridge_tokens = set()
        self._truth_target_ned = None
        self._evaluations = {}
        self._episode_mid = None

    def session_metadata(self):
        s = self.client.session
        return {
            "enabled": self.config.enabled,
            "state": self.state,
            "error": self.error,
            "endpoint": self.config.base_url,
            "sid": s.sid if s else None,
            "world_id": self.config.world_id,
            "remote_build": self.remote_meta.get("build"),
            "session": {
                k: v for k, v in self.info.items() if k not in ("token", "endpoints")
            },
            "renderer_ready": time.time() - self.capture.last_renderer_at < 4
            and bool(self.capture.renderer_status.get("ready")),
            "renderer": self.capture.renderer_status,
            "web_ready": (self.config.web_dir / "index.html").is_file(),
            "admission_wait_until": self.client.admission_wait_until,
            "telemetry_error": self._last_poll_error,
            "t_sim_ns": next(
                (p.latest["t_sim_ns"] for p in self.ports.values() if p.latest), None
            ),
            "vehicles": [
                {"vehicle_id": vid, "telemetry": self.safe_telemetry(p)}
                for vid, p in self.ports.items()
            ],
            "scene_seed": self.config.scene_seed,
            "evaluation": self._evaluations[self._episode_mid].snapshot(
                self.missions[self._episode_mid].status
            )
            if self._episode_mid in self._evaluations
            and self._episode_mid in self.missions
            else None,
        }

    @staticmethod
    def safe_telemetry(p):
        try:
            return p.telemetry().model_dump()
        except HarnessError:
            return None

    def metadata(self):
        return {
            "provider": provider_settings(),
            "mode": "drone4d",
            "policy": self.config.mission_policy.model_dump(),
            "vehicles": [
                {
                    "vehicle_id": vid,
                    "backend": "Drone4D FleetSim",
                    "camera_configured": True,
                    "execution_enabled": True,
                    "hardware_height_limit_m": 80,
                    "mission_id": self.owners.get(vid),
                }
                for vid in self.ports
            ],
            "missions": [m.snapshot() for m in list(self.missions.values())[-12:]],
            "hardware_control_enabled": False,
            "sandbox": self.session_metadata(),
        }

    async def connect(self):
        async with self.lifecycle_lock:
            if self._closing:
                raise HarnessError("sandbox service is closing")
            if not self.config.enabled:
                raise HarnessError("Drone4D 未启用；使用 --sandbox 启动 Dashboard")
            if not (self.config.web_dir / "index.html").is_file():
                raise HarnessError(
                    "Drone4D 原生视图未构建；先运行 .venv/bin/python scripts/prepare-drone4d.py"
                )
            if self.state == "ready" or (
                self.connect_task and not self.connect_task.done()
            ):
                return self.session_metadata()
            self.capture.last_renderer_at = 0
            self.capture.renderer_status = {}
            self.client.cancel_create.clear()
            self.state = "connecting"
            self.error = None
            self.connect_task = asyncio.create_task(self._connect())
            return self.session_metadata()

    async def _connect(self):
        try:
            if not self.client.session:
                self.remote_meta = await self.client.connect()
            await self.client.create(
                search_scene(self.config.source_dir, self.config.scene_seed)
            )
            self.info = await self.client.call(self.client.session.state)
            items = await self.client.call(self.client.session.vehicles.list)
            self.ports = {v["id"]: Drone4DPort(self, v["id"]) for v in items["items"]}
            for p in self.ports.values():
                for attempt in range(60):
                    try:
                        await p.refresh()
                        if p.latest["state"].get("lifecycle") != "READY":
                            raise HarnessError(
                                "waiting for Drone4D vehicle lifecycle READY"
                            )
                        break
                    except (HarnessError, KeyError):
                        if attempt == 59:
                            raise
                        await asyncio.sleep(0.5)
            if self.client.recovered:
                for p in self.ports.values():
                    await p.hold()
            # Fixed initial search setup; subsequent actions are VLM decisions.
            for p in self.ports.values():
                await p.command(
                    "gimbal",
                    mode="angles",
                    pitch_rad=math.radians(self.config.initial_camera_pitch_deg),
                    yaw_rad=0,
                )
                await p.command("zoom", sensor="eo", zoom_x=self.config.initial_zoom_x)
            await self.load_evaluation_target()
            self.state = "ready"
            self.poll_task = asyncio.create_task(self.poll())
        except Exception as exc:
            logging.getLogger(__name__).exception("Drone4D session connection failed")
            self.ports = {}
            self.state = "failed"
            self.error = str(exc)
            # Retain an already-created session for retry; initialization failures
            # must not burn another public creation quota or discard its credentials.
            if not self.client.session:
                try:
                    await self.client.close()
                except Exception:
                    pass

    async def poll(self):
        tick = 0
        while not self._closing and self.client.session:
            try:
                for p in list(self.ports.values()):
                    await p.refresh()
                    await p.renew_payload()
                    for mid, m in self.missions.items():
                        if (
                            m.request.vehicle_id == p.vehicle_id
                            and m.status not in TERMINAL
                            and mid in self._evaluations
                        ):
                            self._evaluations[mid].update(p.telemetry())
                if tick % 20 == 0:
                    await self.client.call(self.client.renew_if_due)
                    self.info = await self.client.call(self.client.session.state)
                    await self.client.call(self.client.persist)
                self._last_poll_error = None
            except HarnessError as e:
                self._last_poll_error = str(e)
            tick += 1
            await asyncio.sleep(0.5)

    def fleet(self):
        return [
            {
                "vehicle_id": vid,
                "telemetry": self.safe_telemetry(p),
                "mission_id": self.owners.get(vid),
            }
            for vid, p in self.ports.items()
        ]

    def port(self, vid, owner, seed=None):
        if self.state != "ready" or vid not in self.ports:
            raise HarnessError("请先创建 Drone4D 独立沙盒并等待飞机就绪")
        if seed is not None:
            raise HarnessError("Drone4D 场景种子由沙盒配置管理")
        return self.ports[vid]

    async def start(self, request):
        async with self.lifecycle_lock:
            return await self._start(request)

    async def _start(self, request):
        if not provider_settings()["configured"]:
            raise HarnessError(
                "DASHSCOPE_API_KEY is not configured; no fake VLM fallback"
            )
        async with self.lock:
            if request.vehicle_id in self.owners:
                raise HarnessError("sandbox vehicle already owned by a mission")
            port = self.port(request.vehicle_id, "pending", request.simulation_seed)
            if (
                time.time() - self.capture.last_renderer_at > 4
                or not self.capture.renderer_status.get("ready")
            ):
                raise HarnessError("原生云台 renderer 未在线；请等待沙盒 3D 视图载入")
            await port.refresh()
            m = Mission(
                request,
                port,
                self.planner,
                self.config.mission_policy.model_copy(deep=True),
            )
            self.missions[m.mission_id] = m
            self.owners[request.vehicle_id] = m.mission_id
            m.fleet_context = self.fleet
            m.frame_prefix = "/v1/sandbox/brain"
            if self._truth_target_ned and m.policy.monitor_radius_m:
                evaluation = EpisodeEvaluation(
                    self._truth_target_ned, port.telemetry(), m.policy.monitor_radius_m
                )
                self._evaluations[m.mission_id] = evaluation
                self._episode_mid = m.mission_id
                m.evaluation = lambda: evaluation.snapshot(m.status)
            m.task = asyncio.create_task(self.run(m))
            self.prune()
            return m.snapshot()

    async def load_evaluation_target(self):
        self._truth_target_ned = None
        try:
            row = await self.client.call(self.client.session.targets.get, "subject-a")
            e, n, z = row["pos"]
            if z is None:
                response = await self.client.call(
                    self.client.native_request,
                    "POST",
                    f"/api/world/{self.config.world_id}/query",
                    headers={},
                    params={},
                    content=json.dumps(
                        {"op": "ground_dtm", "points": [[e, n]]}
                    ).encode(),
                )
                response.raise_for_status()
                z = response.json()["z_m"][0]
            if z is not None:
                self._truth_target_ned = (n, e, -z)
        except (
            HarnessError,
            httpx.HTTPError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            # Missing scoring data is reported as unavailable, never fabricated.
            pass

    async def run(self, m):
        try:
            await super().run(m)
        finally:
            if m.mission_id in self._evaluations:
                try:
                    self._evaluations[m.mission_id].update(m.port.telemetry())
                except HarnessError:
                    pass

    async def reset(self, seed):
        async with self.lifecycle_lock:
            if self.state != "ready" or not self.client.session:
                raise HarnessError("先连接沙盒，再重置场景")
            await super().close()
            if self.poll_task:
                self.poll_task.cancel()
                await asyncio.gather(self.poll_task, return_exceptions=True)
            self.config.scene_seed = seed
            await self.client.call(
                self.client.session.restore, search_scene(self.config.source_dir, seed)
            )
            await self.client.call(self.client.session.clock, "play")
            self.ports = {}
            self.owners.clear()
            self.preview_frames.clear()
            self._episode_mid = None
            self.capture.last_renderer_at = 0
            self.state = "connecting"
            self.connect_task = asyncio.create_task(self._connect())
            return self.session_metadata()

    async def preview(self, vid):
        async with self.lifecycle_lock:
            return await self._preview(vid)

    async def _preview(self, vid):
        active = self.missions.get(self.owners.get(vid))
        if active and active.observation:
            return active.observation
        p = self.port(vid, "preview")
        obs = await p.observe()
        self.preview_frames[vid] = obs
        return obs

    async def disconnect(self):
        async with self.lifecycle_lock:
            self.client.cancel_create.set()
            if self.connect_task and not self.connect_task.done():
                await asyncio.shield(self.connect_task)
            return await self._disconnect()

    async def _disconnect(self):
        self.state = "disconnecting"
        await super().close()
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        await self.client.close()
        self.ports = {}
        self.state = "disconnected"
        self.info = {}
        self.preview_frames.clear()
        self.owners.clear()
        self.bridge_tokens.clear()
        return self.session_metadata()

    def accepts_token(self, token):
        return bool(self.client.session) and (
            token == self.client.session.token or token in self.bridge_tokens
        )

    def remember_token(self, token):
        if token:
            if len(self.bridge_tokens) >= 128:
                self.bridge_tokens.clear()
            self.bridge_tokens.add(token)

    def bridge_config(self):
        if self.state != "ready" or not self.client.session:
            raise HarnessError("sandbox not ready")
        s = self.client.session
        self.remember_token(s.token)
        return {
            "sid": s.sid,
            "token": s.token,
            "expires_unix_ns": str(s.expires_unix_ns),
            "world": s.world,
            "template": s.info.get("template_id"),
            "styles": visual_styles(),
            "vehicle_ids": list(self.ports),
            "api_origin": "",
            "session": self.info,
        }

    async def close(self):
        self._closing = True
        self.client.cancel_create.set()
        if self.connect_task and not self.connect_task.done():
            # SDK connection work must drain before cleanup; to_thread cancellation doesn't stop creation.
            await asyncio.shield(self.connect_task)
        await self.disconnect()
