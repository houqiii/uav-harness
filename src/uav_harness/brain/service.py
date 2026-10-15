import asyncio
import secrets
import time
from .contracts import MissionRequest
from .hardware import HarnessPort
from .provider import QwenPlanner, provider_settings
from .runner import Mission, TERMINAL
from .simulation import SimulatedPort
from ..errors import HarnessError


class BrainService:
    def __init__(self, harness, offline, planner=None):
        self.harness = harness
        self.offline = offline
        self.planner = planner or QwenPlanner()
        self.missions = {}
        self.owners = {}
        self.ports = {}
        self.preview_frames = {}
        self.seed = secrets.randbits(32)
        self.lock = asyncio.Lock()

    def metadata(self):
        settings = self.harness.settings
        vehicles = []
        for vid, s in self.harness.sessions.items():
            if s.config.profile != "flight":
                continue
            camera = next(
                (c for c in settings.brain.cameras if c.vehicle_id == vid), None
            )
            vehicles.append(
                {
                    "vehicle_id": vid,
                    "backend": s.config.backend,
                    "camera_configured": bool(camera) or self.offline,
                    "hardware_height_limit_m": s.config.max_height_m,
                    "execution_enabled": self.offline
                    or (
                        settings.allow_control and settings.brain.allow_hardware_control
                    ),
                    "mission_id": self.owners.get(vid),
                }
            )
        return {
            "provider": provider_settings(),
            "mode": "geometric_simulation" if self.offline else "hardware",
            "policy": settings.brain.policy.model_dump(),
            "vehicles": vehicles,
            "missions": [m.snapshot() for m in list(self.missions.values())[-12:]],
            "simulation_seed": self.seed if self.offline else None,
            "hardware_control_enabled": not self.offline
            and settings.allow_control
            and settings.brain.allow_hardware_control,
        }

    def port(self, vid, owner, seed=None):
        s = self.harness.sessions.get(vid)
        if not s or s.config.profile != "flight":
            raise HarnessError(
                "VLM mission requires a configured flight-profile vehicle"
            )
        if self.offline:
            if seed is not None and vid in self.owners:
                raise HarnessError("cannot change environment of an active mission")
            if vid not in self.ports or seed is not None:
                self.ports[vid] = SimulatedPort(
                    vid, self.seed if seed is None else seed
                )
            return self.ports[vid]
        if seed is not None:
            raise HarnessError("simulation seed is not a hardware option")
        cfg = next(
            (c for c in self.harness.settings.brain.cameras if c.vehicle_id == vid),
            None,
        )
        if not cfg:
            raise HarnessError(
                "未配置此飞机的图像获取接口；在 brain.cameras 中配置 HTTP snapshot"
            )
        port = HarnessPort(self.harness, vid, cfg, owner)
        self.ports[vid] = port
        return port

    def fleet(self):
        result = []
        for vid, p in self.ports.items():
            try:
                result.append(
                    {
                        "vehicle_id": vid,
                        "telemetry": p.telemetry().model_dump(),
                        "mission_id": self.owners.get(vid),
                    }
                )
            except HarnessError:
                result.append({"vehicle_id": vid, "state": "telemetry_unavailable"})
        return result

    async def start(self, request: MissionRequest):
        if not provider_settings()["configured"]:
            raise HarnessError(
                "DASHSCOPE_API_KEY is not configured; no fallback model will be used"
            )
        async with self.lock:
            if request.vehicle_id in self.owners:
                raise HarnessError("vehicle already owned by an active VLM mission")
            port = self.port(request.vehicle_id, "pending", request.simulation_seed)
            policy = self.harness.settings.brain.policy.model_copy(deep=True)
            if not self.offline:
                height = self.harness.sessions[request.vehicle_id].config.max_height_m
                if policy.max_altitude_m > height or policy.search_altitude_m > height:
                    raise HarnessError(
                        f"任务高度范围超过飞控档案 {height:g} m；请由操作者调整配置，模型不能修改限高"
                    )
            m = Mission(request, port, self.planner, policy)
            if not self.offline:
                port.owner = m.mission_id
                if port.execution_enabled:
                    await self.harness.reserve(request.vehicle_id, m.mission_id)
            self.missions[m.mission_id] = m
            self.owners[request.vehicle_id] = m.mission_id
            m.fleet_context = self.fleet
            m.task = asyncio.create_task(self.run(m))
            self.prune()
            return m.snapshot()

    def prune(self):
        for mid, m in list(self.missions.items()):
            if len(self.missions) <= 20:
                break
            if (
                m.status in TERMINAL
                and m.status != "unknown"
                and (not m.task or m.task.done())
            ):
                self.missions.pop(mid)

    async def run(self, m):
        try:
            await m.run()
        finally:
            if m.status not in ("paused", "unknown"):
                self.owners.pop(m.request.vehicle_id, None)
                if not self.offline:
                    self.harness.release(m.request.vehicle_id, m.mission_id)

    def get(self, mid):
        if mid not in self.missions:
            raise HarnessError("mission not found")
        return self.missions[mid]

    async def control(self, mid, action):
        m = self.get(mid)
        if action == "pause":
            if m.status in TERMINAL or m.status == "paused":
                return m.snapshot()
            await m.pause()
        elif action == "resume":
            async with m.control_lock:
                if m.status != "paused":
                    raise HarnessError("only a paused mission can resume")
                if m.round >= m.policy.max_rounds:
                    raise HarnessError("mission round limit reached")
                m.status = "queued"
                m.task = asyncio.create_task(self.run(m))
        elif action == "cancel":
            if m.status in TERMINAL:
                return m.snapshot()
            await m.cancel()
            if m.status != "unknown":
                self.owners.pop(m.request.vehicle_id, None)
                if not self.offline:
                    self.harness.release(m.request.vehicle_id, m.mission_id)
        elif action == "reconcile":
            async with m.control_lock:
                if m.status != "unknown":
                    raise HarnessError(
                        "only an uncertain mission needs ground reconciliation"
                    )
                if m.task and not m.task.done():
                    raise HarnessError(
                        "mission cleanup is still running; wait before ground reconciliation"
                    )
                if not hasattr(m.port, "reconcile"):
                    raise HarnessError("this port has no hardware reconciliation")
                await m.port.reconcile()
                m.status = "cancelled"
                m.ended_at = time.time()
                m.event(
                    "reconciled",
                    "已用新鲜地面／未解锁状态核验并释放所有权；未重放任何动作",
                )
                self.owners.pop(m.request.vehicle_id, None)
                self.harness.release(m.request.vehicle_id, m.mission_id)
        else:
            raise HarnessError("unknown mission lifecycle action")
        return m.snapshot()

    async def preview(self, vid):
        active = self.missions.get(self.owners.get(vid))
        if active and active.observation:
            return active.observation
        p = self.port(vid, "preview")
        obs = await p.observe()
        self.preview_frames[vid] = obs
        return obs

    async def close(self):
        await asyncio.gather(
            *(m.cancel() for m in self.missions.values() if m.status not in TERMINAL),
            return_exceptions=True,
        )
        # Terminal status can precede native-hold cleanup. Drain it before closing links.
        await asyncio.gather(
            *(
                asyncio.shield(m.task)
                for m in self.missions.values()
                if m.task and not m.task.done()
            ),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(p.close() for p in self.ports.values()), return_exceptions=True
        )
