"""Server-side receding-horizon mission: fresh observation -> one decision -> actual result."""

import asyncio
import math
import time
import uuid
from .geometry import ground_target, approach_offset
from .provider import RetryablePlannerError
from ..errors import HarnessError, Uncertain


TERMINAL = {
    "succeeded",
    "not_found",
    "needs_operator",
    "failed",
    "cancelled",
    "unknown",
    "preview_ready",
}


class Mission:
    def __init__(self, request, port, planner, policy):
        self.mission_id = uuid.uuid4().hex
        self.request, self.port, self.planner, self.policy = (
            request,
            port,
            planner,
            policy,
        )
        self.status = "created"
        self.round = 0
        self.events = []
        self.history = []
        self.frames = {}
        self.observation = None
        self.decision = None
        self.target = None
        self.scanned = set()
        self.started_at = time.time()
        self.task = None
        self.stop_reason = None
        self.progress = {}
        self.fleet_context = []
        self.event_sequence = 0
        self.frame_detections = {}
        self.frame_prefix = "/v1/brain"
        self.resolved_objective = None
        self.control_lock = asyncio.Lock()
        self.ended_at = None
        self.visited_positions = []
        self.evaluation = None  # Operator-only; never included in model observations.
        self.invalid_decisions = 0

    def event(self, kind, message, **data):
        self.event_sequence += 1
        self.events.append(
            {
                "sequence": self.event_sequence,
                "at": time.time(),
                "kind": kind,
                "message": message,
                **data,
            }
        )
        self.events = self.events[-200:]

    def snapshot(self):
        obs = self.observation
        observation = obs.model_dump() if obs else None
        if observation:
            observation["image_url"] = (
                f"{self.frame_prefix}/missions/{self.mission_id}/frames/{obs.observation_id}"
            )
        try:
            live = self.port.telemetry().model_dump()
        except HarnessError:
            live = None
        return {
            "mission_id": self.mission_id,
            "query": self.request.query,
            "vehicle_id": self.request.vehicle_id,
            "objective": self.request.objective,
            "resolved_objective": self.resolved_objective,
            "evaluation": self.evaluation() if callable(self.evaluation) else None,
            "mode": self.port.mode,
            "status": self.status,
            "round": self.round,
            "started_at": self.started_at,
            "elapsed_s": round((self.ended_at or time.time()) - self.started_at, 1),
            "policy": self.policy.model_dump(),
            "observation": observation,
            "decision": self.decision.model_dump() if self.decision else None,
            "target": self.target,
            "scan_headings": sorted(self.scanned),
            "events": self.events,
            "progress": self.progress,
            "live_telemetry": live,
            "simulation_seed": getattr(self.port, "seed", None),
            "unresolved_job_ids": list(getattr(self.port, "interrupted_jobs", [])),
            "frames": [
                {
                    "observation_id": f.observation_id,
                    "captured_at": f.captured_at,
                    "camera": f.camera.model_dump(),
                    "source": f.source,
                    "detection": self.frame_detections.get(f.observation_id),
                    "image_url": f"{self.frame_prefix}/missions/{self.mission_id}/frames/{f.observation_id}",
                }
                for f in self.frames.values()
            ],
        }

    def remember_target(self, obs, decision):
        if not decision.target:
            if self.target:
                self.target["visible"] = False
            return
        grounded = None
        try:
            grounded = ground_target(obs, decision.target)
        except HarnessError:
            pass
        previous = self.target
        same = False
        if previous and grounded and previous.get("geometry"):
            old = previous["geometry"]
            same = previous["label"] == decision.target.label and math.dist(
                old["position_ned"][:2], grounded["position_ned"][:2]
            ) <= max(5, old["uncertainty_m"] + grounded["uncertainty_m"])
        elif previous and previous.get("observation_id") != obs.observation_id:
            box = previous["bbox"]
            new = decision.target.bbox
            same = (
                previous["label"] == decision.target.label
                and max(abs(a - b) for a, b in zip(box, new)) < 40
                and previous.get("camera_yaw_deg") == obs.camera.yaw_deg
                and math.dist(
                    previous.get("capture_position_ned", obs.telemetry.position_ned),
                    obs.telemetry.position_ned,
                )
                < 0.2
            )
        self.target = {
            "label": decision.target.label,
            "bbox": list(decision.target.bbox),
            "visible": True,
            "observation_id": obs.observation_id,
            "camera_yaw_deg": obs.camera.yaw_deg,
            "capture_position_ned": list(obs.telemetry.position_ned),
            "confirmed_observations": previous["confirmed_observations"] + 1
            if same
            else 1,
            "geometry": grounded,
            "last_seen_at": obs.captured_at,
        }

    def validate_fresh(self, obs):
        now = time.time()
        current = self.port.telemetry()
        if (
            now - obs.captured_at > self.policy.max_observation_age_s
            or now - obs.telemetry.observed_at > self.policy.max_observation_age_s
        ):
            raise HarnessError(
                "observation expired; acquire a fresh image before motion"
            )
        yaw = abs((current.heading_deg - obs.telemetry.heading_deg + 180) % 360 - 180)
        if math.dist(current.position_ned, obs.telemetry.position_ned) > 0.8 or yaw > 3:
            raise HarnessError(
                "vehicle pose changed during inference; reobserve before dispatch"
            )
        if hasattr(self.port, "camera_pose"):
            pose = self.port.camera_pose()
            if any(
                abs((pose[k] - getattr(obs.camera, k) + 180) % 360 - 180) > 2
                for k in ("yaw_deg", "pitch_deg", "roll_deg")
            ):
                raise HarnessError(
                    "camera pose changed during inference; reobserve before dispatch"
                )

    def ground_command(self, obs, decision):
        a = decision.next_action
        p = self.policy
        t = obs.telemetry
        if a.action not in obs.capabilities:
            raise HarnessError("requested device capability unavailable")
        if a.action in ("takeoff", "altitude"):
            if not p.min_altitude_m <= a.altitude_m <= p.max_altitude_m:
                raise HarnessError("altitude outside configured mission bounds")
            if a.action == "takeoff" and t.airborne:
                raise HarnessError("already airborne; use altitude adjustment")
            if a.action == "altitude" and not t.airborne:
                raise HarnessError("altitude adjustment requires airborne vehicle")
            current_alt = t.home_down_m - t.position_ned[2]
            if a.action == "altitude" and a.altitude_m < current_alt:
                if self.resolved_objective == "find":
                    raise HarnessError(
                        "find-only task does not permit descent toward target"
                    )
                if obs.camera.ground_down_m is None:
                    raise HarnessError("ground clearance unknown; descent unavailable")
                clearance = obs.camera.ground_down_m - (t.home_down_m - a.altitude_m)
                if clearance < p.min_altitude_m + obs.camera.ground_uncertainty_m:
                    raise HarnessError(
                        "descent would violate conservative ground clearance"
                    )
            return a.model_dump()
        if a.action in ("yaw", "gimbal", "approach") and not t.airborne:
            raise HarnessError("take off before airborne search tools")
        if a.action == "search_move":
            if not t.airborne:
                raise HarnessError("take off before exploratory translation")
            if (
                self.resolved_objective == "find"
                and self.target
                and self.target.get("confirmed_observations", 0) >= 2
            ):
                raise HarnessError(
                    "find-only target already confirmed; hold and report instead of translating"
                )
            step = min(a.distance_m, p.max_step_m)
            angle = math.radians(a.heading_deg)
            offset = (step * math.cos(angle), step * math.sin(angle), 0)
            endpoint = [v + d for v, d in zip(t.position_ned, offset)]
            known = self.target.get("geometry") if self.target else None
            if known:
                a0 = t.position_ned[:2]
                delta = offset[:2]
                target2 = known["position_ned"][:2]
                fraction = min(
                    1,
                    max(
                        0,
                        sum((q - a) * d for q, a, d in zip(target2, a0, delta))
                        / (step * step),
                    ),
                )
                closest = [a + fraction * d for a, d in zip(a0, delta)]
                if math.dist(closest, target2) < p.stand_off_m + known["uncertainty_m"]:
                    raise HarnessError(
                        "exploration would cross known target safety standoff"
                    )
            if (
                math.hypot(endpoint[0] - t.home_north_m, endpoint[1] - t.home_east_m)
                > p.max_radius_m
            ):
                raise HarnessError("exploration outside configured local radius")
            if (
                not p.min_altitude_m
                <= t.home_down_m - t.position_ned[2]
                <= p.max_altitude_m
            ):
                raise HarnessError("exploration altitude outside mission bounds")
            return {"action": "move", "offset_ned": offset}
        if a.action == "approach":
            if self.resolved_objective != "approach":
                raise HarnessError(
                    "task objective permits finding only, not approaching"
                )
            if (
                not self.target
                or not self.target["visible"]
                or self.target["confirmed_observations"] < 2
            ):
                raise HarnessError(
                    "target requires two consistent fresh observations before approach"
                )
            geometry = self.target.get("geometry")
            if not geometry:
                raise HarnessError(
                    "ground geometry unavailable; observe or request operator input"
                )
            offset = approach_offset(obs, geometry, p, a.step_m)
            target = [x + y for x, y in zip(t.position_ned, offset)]
            if (
                math.hypot(target[0] - t.home_north_m, target[1] - t.home_east_m)
                > p.max_radius_m
            ):
                raise HarnessError("movement outside configured local radius")
            return {"action": "move", "offset_ned": offset}
        return a.model_dump()

    def finish(self, outcome):
        if outcome == "found":
            if (
                not self.target
                or not self.target["visible"]
                or self.target["confirmed_observations"] < 2
            ):
                raise HarnessError(
                    "cannot finish found without a confirmed currently visible target"
                )
            if self.resolved_objective == "approach":
                g = self.target.get("geometry")
                if self.policy.monitor_radius_m is not None:
                    key = (
                        "slant_distance_m"
                        if self.policy.distance_metric == "slant"
                        else "horizontal_distance_m"
                    )
                    if (
                        not g
                        or g.get(key, float("inf")) + g["uncertainty_m"]
                        > self.policy.monitor_radius_m
                    ):
                        raise HarnessError(
                            "monitoring distance not reached; move and/or adjust altitude using fresh observations"
                        )
                elif (
                    not g
                    or g["horizontal_distance_m"]
                    > self.policy.stand_off_m + g["uncertainty_m"] + 3
                ):
                    raise HarnessError(
                        "observation standoff not yet reached; cannot declare approach complete"
                    )
            self.status = "succeeded"
        elif outcome == "not_found":
            if len(self.scanned) < 12:
                raise HarnessError(
                    "full-circle coverage not yet observed; cannot declare not_found"
                )
            self.status = "not_found"
        else:
            self.status = "needs_operator"

    async def run(self):
        self.task = asyncio.current_task()
        self.stop_reason = None
        self.ended_at = None
        try:
            remaining = self.policy.max_duration_s - (time.time() - self.started_at)
            async with asyncio.timeout(max(0, remaining)):
                await self.run_loop()
        except TimeoutError:
            self.status = (
                "unknown"
                if getattr(self.port, "interrupted_jobs", [])
                else "needs_operator"
            )
            try:
                await self.port.hold()
                self.event("limit", "任务时限已到，已取消等待并保持位置")
            except Exception as exc:
                self.status = "unknown"
                self.event("error", "位置保持未确认：" + str(exc))
        except asyncio.CancelledError:
            self.status = "pausing"
            try:
                await self.port.hold()
                self.status = (
                    "unknown"
                    if getattr(self.port, "interrupted_jobs", [])
                    else ("paused" if self.stop_reason == "pause" else "cancelled")
                )
                self.event(
                    self.status,
                    "已保持位置；中断动作结果仍需地面核验"
                    if self.status == "unknown"
                    else "已中止本轮规划／执行并保持位置",
                )
            except Exception as exc:
                self.status = "unknown"
                self.event("error", "位置保持未确认：" + str(exc))
        except Exception as exc:
            self.status = "unknown" if isinstance(exc, Uncertain) else "failed"
            self.event("error", str(exc))
            try:
                await self.port.hold()
            except Exception as hold_error:
                self.status = "unknown"
                self.event("error", "位置保持未确认：" + str(hold_error))

        finally:
            if self.status != "paused":
                self.ended_at = time.time()

    async def run_loop(self):
        while self.round < self.policy.max_rounds:
            self.status = "observing"
            obs = await self.port.observe()
            self.observation = obs
            if callable(self.fleet_context):
                obs.fleet = self.fleet_context()
            self.frames[obs.observation_id] = obs
            while len(self.frames) > 16:
                expired = next(iter(self.frames))
                self.frames.pop(expired)
                self.frame_detections.pop(expired, None)
            horizontal_fov = math.degrees(
                2 * math.atan(obs.camera.width / (2 * obs.camera.fx))
            )
            if (
                obs.telemetry.airborne
                and horizontal_fov >= self.policy.scan_step_deg
                and -60 <= obs.camera.pitch_deg <= -10
            ):
                self.scanned.add(int(round(obs.camera.yaw_deg / 30)) % 12 * 30)
            obs.scan_headings_deg = sorted(self.scanned)
            obs.accepted_task_goal = self.resolved_objective
            if (
                not self.visited_positions
                or math.dist(obs.telemetry.position_ned, self.visited_positions[-1])
                >= 10
            ):
                self.visited_positions.append(obs.telemetry.position_ned)
                self.visited_positions = self.visited_positions[-64:]
            obs.visited_positions_ned = list(self.visited_positions)
            self.event(
                "observation",
                "取得新图像与拍摄时遥测",
                observation_id=obs.observation_id,
                camera_yaw_deg=obs.camera.yaw_deg,
                source=obs.source,
            )
            self.status = "thinking"
            self.progress = {"phase": "waiting"}
            self.round += 1
            try:
                d = await self.planner.decide(
                    self.request.query + "\nTask objective: " + self.request.objective,
                    obs,
                    self.policy,
                    self.history,
                    self.target,
                    lambda e: self.progress.update(e),
                )
                self.invalid_decisions = 0
            except RetryablePlannerError as exc:
                self.invalid_decisions += 1
                self.event("rejected", str(exc))
                if self.invalid_decisions > 2:
                    raise
                self.history.append(
                    {"result": {"status": "rejected", "error": str(exc)}}
                )
                self.history = self.history[-8:]
                await self.port.hold()
                continue
            if self.stop_reason:
                raise asyncio.CancelledError()
            self.decision = d
            if d.observation_id == obs.observation_id:
                self.frame_detections[obs.observation_id] = (
                    d.target.model_dump() if d.target else None
                )
            self.event(
                "decision",
                d.scene_summary,
                action=d.next_action.model_dump(),
                plan=d.plan,
                round=self.round,
                observation_id=d.observation_id,
            )
            try:
                if d.observation_id != obs.observation_id:
                    raise HarnessError("decision observation mismatch")
                self.validate_fresh(obs)
                if self.resolved_objective is None:
                    if (
                        self.request.objective != "auto"
                        and d.task_goal != self.request.objective
                    ):
                        raise HarnessError(
                            "VLM task goal conflicts with operator objective"
                        )
                    self.resolved_objective = d.task_goal
                elif d.task_goal != self.resolved_objective:
                    raise HarnessError(
                        "task goal cannot silently change after initial interpretation"
                    )
                self.remember_target(obs, d)
                if not getattr(self.port, "execution_enabled", True):
                    self.status = "preview_ready"
                    self.event(
                        "preview",
                        "VLM 已给出本轮动作提议；实机控制未在配置中启用，不派发动作",
                    )
                    return
                if d.next_action.action == "finish":
                    self.finish(d.next_action.outcome)
                    await self.port.hold()
                    self.event(
                        "finished", "任务结束，已执行位置保持", outcome=self.status
                    )
                    return
                command = self.ground_command(obs, d)
                self.status = "executing"
                self.event("dispatch", "执行本轮短动作", command=command)
                result = await self.port.execute(command)
                self.history.append({"decision": d.model_dump(), "result": result})
                self.history = self.history[-8:]
                self.event("result", "收到动作实际结果", result=result)
            except Uncertain:
                raise
            except HarnessError as exc:
                result = {"status": "rejected", "error": str(exc)}
                self.history.append({"decision": d.model_dump(), "result": result})
                self.history = self.history[-8:]
                self.event("rejected", str(exc), result=result)
                await self.port.hold()
            await asyncio.sleep(0.05)
        self.status = "needs_operator"
        await self.port.hold()
        self.event("limit", "达到决策轮数或任务时限，已保持位置")

    async def stop(self, reason):
        async with self.control_lock:
            if self.status in TERMINAL:
                return
            self.stop_reason = reason
            if self.task and not self.task.done():
                if self.status != "pausing":
                    self.task.cancel()
                await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            # Handles a task cancelled before run() started, and ending a paused task.
            if self.status not in TERMINAL and self.status != "paused":
                await self.port.hold()
                self.status = "paused" if reason == "pause" else "cancelled"
            elif self.status == "paused" and reason == "cancel":
                await self.port.hold()
                self.status = "cancelled"
                self.ended_at = time.time()

    async def pause(self):
        await self.stop("pause")

    async def cancel(self):
        await self.stop("cancel")
