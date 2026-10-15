"""Ports for configured HTTP snapshots and the existing journaled MAVLink Harness."""

import asyncio
import io
import math
import time
import uuid
import httpx
from PIL import Image
from .contracts import Camera, Observation, Telemetry
from .geometry import basis
from ..contracts import Plan
from ..errors import HarnessError, Uncertain


class SnapshotCamera:
    def __init__(
        self, config, telemetry, attitude, http_transport=None, body_attitude=None
    ):
        self.config, self.telemetry, self.attitude = config, telemetry, attitude
        self.http_transport = http_transport
        self.body_attitude = body_attitude or attitude

    def pose(self, t):
        c = self.config
        angles = self.attitude()
        body_angles = self.body_attitude()
        if not angles or not body_angles:
            raise HarnessError("fresh camera/body orientation unavailable")
        body = Camera(
            width=1,
            height=1,
            fx=1,
            fy=1,
            cx=0,
            cy=0,
            yaw_deg=t.heading_deg,
            pitch_deg=body_angles["pitch"],
            roll_deg=body_angles["roll"],
            source="body",
            timing="receipt_estimate",
        )
        b = basis(body)
        if c.gimbal_device_id is not None:
            yaw, pitch, roll = angles["yaw"], angles["pitch"], angles["roll"]
        else:
            if c.mount_pitch_deg is None:
                raise HarnessError(
                    "fixed camera mounting orientation must be configured"
                )
            mount = body.model_copy(
                update={
                    "yaw_deg": c.mount_yaw_deg,
                    "pitch_deg": c.mount_pitch_deg,
                    "roll_deg": c.mount_roll_deg,
                }
            )
            world = [
                tuple(sum(b[k][i] * axis[k] for k in range(3)) for i in range(3))
                for axis in basis(mount)
            ]
            yaw = math.degrees(math.atan2(world[0][1], world[0][0])) % 360
            pitch = math.degrees(math.asin(max(-1, min(1, -world[0][2]))))
            roll = math.degrees(math.atan2(world[1][2], world[2][2]))
        return {
            "yaw_deg": yaw,
            "pitch_deg": pitch,
            "roll_deg": roll,
            "position_ned": tuple(
                t.position_ned[i]
                + sum(b[k][i] * c.offset_body_frd[k] for k in range(3))
                for i in range(3)
            ),
        }

    async def capture(self, vehicle_id):
        c = self.config
        before = self.telemetry()
        pose_before = self.pose(before)
        started = time.time()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(4, connect=2),
                trust_env=False,
                follow_redirects=False,
                transport=self.http_transport,
            ) as client:
                async with client.stream(
                    "GET",
                    c.snapshot_url,
                    params={"_capture": uuid.uuid4().hex},
                    headers={"Cache-Control": "no-cache"},
                ) as response:
                    if response.status_code != 200:
                        raise HarnessError(
                            f"camera snapshot HTTP {response.status_code}"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            raise HarnessError("camera image too large")
                    raw_stamp = response.headers.get(c.timestamp_header)
            ended = time.time()
            after = self.telemetry()
            pose_after = self.pose(after)
            if raw_stamp is None and not c.trust_receipt_time:
                raise HarnessError(
                    "camera timestamp missing; configure capture timestamp or explicitly trust snapshot receipt timing"
                )
            try:
                stamp = float(raw_stamp) if raw_stamp is not None else ended
            except ValueError:
                raise HarnessError("invalid camera timestamp") from None
            if (
                not math.isfinite(stamp)
                or stamp < started - 0.25
                or stamp > ended + 0.1
            ):
                raise HarnessError("camera timestamp stale or outside capture interval")
            if (
                math.dist(before.position_ned, after.position_ned) > 0.4
                or abs((before.heading_deg - after.heading_deg + 180) % 360 - 180) > 2
            ):
                raise HarnessError(
                    "vehicle moved during capture; cannot bind image to telemetry"
                )
            if (
                any(
                    abs((pose_before[k] - pose_after[k] + 180) % 360 - 180) > 2
                    for k in ("yaw_deg", "pitch_deg", "roll_deg")
                )
                or math.dist(pose_before["position_ned"], pose_after["position_ned"])
                > 0.4
            ):
                raise HarnessError(
                    "camera pose rotated/moved during capture; retry a stationary observation"
                )
            image = Image.open(io.BytesIO(data))
            if image.width > 4096 or image.height > 4096:
                raise HarnessError("camera image dimensions exceed limit")
            if c.width is not None and (image.width, image.height) != (
                c.width,
                c.height,
            ):
                raise HarnessError("camera image size does not match calibration")
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=90)
            calibrated = c.width is not None
            camera = Camera(
                width=image.width,
                height=image.height,
                fx=c.fx or image.width,
                fy=c.fy or image.width,
                cx=c.cx if c.cx is not None else image.width / 2,
                cy=c.cy if c.cy is not None else image.height / 2,
                **pose_before,
                ground_down_m=c.ground_down_m if calibrated else None,
                ground_uncertainty_m=c.ground_uncertainty_m,
                source="calibrated_http_snapshot"
                if calibrated
                else "uncalibrated_http_snapshot",
                timing="camera_timestamp"
                if raw_stamp is not None
                else "receipt_estimate",
            )
            return Observation(
                observation_id=uuid.uuid4().hex,
                vehicle_id=vehicle_id,
                captured_at=stamp,
                telemetry=before,
                camera=camera,
                image=output.getvalue(),
                source="http_camera",
                capabilities=[],
            )
        except HarnessError:
            raise
        except (
            httpx.HTTPError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            raise HarnessError(f"camera capture failed: {type(exc).__name__}") from None


class HarnessPort:
    mode = "hardware"

    def __init__(self, harness, vehicle_id, config, owner):
        self.harness = harness
        self.vehicle_id = vehicle_id
        self.session = harness.sessions[vehicle_id]
        self.config = config
        self.owner = owner
        self.active_job = None
        self.expected_modes = {self.session.mode()}
        self.interrupted_jobs = []
        self.relinquished = False
        self.cleanup_active = False
        self.execution_enabled = (
            harness.settings.allow_control
            and harness.settings.brain.allow_hardware_control
        )
        self.camera_source = SnapshotCamera(
            config,
            self.telemetry,
            self.camera_attitude,
            body_attitude=self.body_attitude,
        )

    def telemetry(self):
        s = self.session
        s.require()
        allowed = (
            s.allowed_modes
            if (self.active_job or self.cleanup_active) and s.allowed_modes is not None
            else self.expected_modes
        )
        if self.relinquished or s.mode() not in allowed:
            self.relinquished = True
            raise Uncertain("manual flight-mode change; brain relinquishes control")
        p = s.get("LOCAL_POSITION_NED")
        a = s.get("ATTITUDE")
        h = s.get("HOME_POSITION", fresh=False)
        b = s.get("SYS_STATUS")
        stamps = [
            s.latest[k][1] for k in ("LOCAL_POSITION_NED", "ATTITUDE", "HEARTBEAT")
        ]
        return Telemetry(
            observed_at=time.time() - (time.monotonic() - min(stamps)),
            position_ned=(p.x, p.y, p.z),
            velocity_ned=(p.vx, p.vy, p.vz),
            home_down_m=h.z,
            home_north_m=h.x,
            home_east_m=h.y,
            heading_deg=math.degrees(a.yaw) % 360,
            armed=s.armed(),
            airborne=not s.grounded(),
            battery_percent=b.battery_remaining if b.battery_remaining >= 0 else None,
            source=s.config.backend + "_mavlink",
        )

    def body_attitude(self):
        a = self.session.get("ATTITUDE")
        return {"pitch": math.degrees(a.pitch), "roll": math.degrees(a.roll)}

    def camera_pose(self):
        return self.camera_source.pose(self.telemetry())

    def camera_attitude(self):
        if self.config.gimbal_device_id is not None:
            for d in self.session.payload.snapshot()["gimbals"]:
                if (
                    d["device_id"] == self.config.gimbal_device_id
                    and d["connected"]
                    and not d["failure_flags"]
                    and d["attitude_deg"]
                    and d["yaw_frame"] != "invalid"
                ):
                    a = d["attitude_deg"].copy()
                    if d["yaw_frame"] == "vehicle":
                        a["yaw"] = (a["yaw"] + self.telemetry().heading_deg) % 360
                    return a
            return None
        a = self.session.get("ATTITUDE")
        return {"pitch": math.degrees(a.pitch), "roll": math.degrees(a.roll)}

    async def observe(self):
        obs = await self.camera_source.capture(self.vehicle_id)
        obs.capabilities = ["takeoff", "yaw", "altitude", "hold", "observe", "finish"]
        if self.harness.settings.brain.horizontal_space_cleared:
            obs.capabilities.append("approach")
        if self.config.gimbal_device_id is not None:
            obs.capabilities.append("gimbal")
        return obs

    async def execute(self, command):
        if not self.execution_enabled:
            raise HarnessError("hardware brain execution is disabled in configuration")
        self.telemetry()  # Preserve manual takeover across action boundaries.
        action = command["action"]
        params = {}
        if action == "observe":
            return {
                "status": "succeeded",
                "completion_scope": "new_observation_requested",
            }
        if action == "gimbal":
            await self.gimbal(command)
            return {"status": "succeeded", "completion_scope": "observed_gimbal_pose"}
        if action == "takeoff":
            name = "flight.takeoff"
            params = {"altitude_home_m": command["altitude_m"]}
        elif action == "yaw":
            name = "flight.yaw"
            params = {"heading_deg": command["heading_deg"]}
        elif action == "move":
            n, e, d = command["offset_ned"]
            name = "flight.move_relative"
            params = {"frame": "local_enu", "x_m": e, "y_m": n, "z_m": -d}
        elif action == "altitude":
            t = self.telemetry()
            name = "flight.move_relative"
            params = {
                "frame": "local_enu",
                "x_m": 0,
                "y_m": 0,
                "z_m": command["altitude_m"] - (t.home_down_m - t.position_ned[2]),
            }
        elif action == "hold":
            if not self.telemetry().airborne:
                return {"status": "succeeded", "completion_scope": "observed_on_ground"}
            name = "flight.hold"
            params = {"duration_s": command.get("duration_s", 0.5)}
        else:
            raise HarnessError("unsupported hardware tool")
        plan = Plan.model_validate(
            {
                "actions": [
                    {
                        "action_id": uuid.uuid4().hex,
                        "vehicle_id": self.vehicle_id,
                        "action": name,
                        "params": params,
                        "timeout_s": 120,
                    }
                ]
            }
        )
        row = await self.harness.submit(
            plan, "brain-" + uuid.uuid4().hex, mission_owner=self.owner
        )
        self.active_job = row["job_id"]
        try:
            await asyncio.shield(self.harness.jobs[self.active_job])
            result = self.harness.journal.get(self.active_job)
            self.expected_modes = {
                ((4, 3) if self.session.config.backend == "px4" else (17, 0))
            }
            if result["status"] == "unknown":
                self.interrupted_jobs.append(self.active_job)
                raise Uncertain(
                    "hardware action outcome unknown; reconcile before retry"
                )
            if result["status"] != "succeeded":
                raise HarnessError(
                    "hardware action failed; inspect Harness job " + self.active_job
                )
            if action == "takeoff":
                completed_modes = (
                    {(4, 2), (4, 3)}
                    if self.session.config.backend == "px4"
                    else {(4, 0)}
                )
                if self.session.mode() not in completed_modes:
                    self.relinquished = True
                    raise Uncertain(
                        "manual flight-mode change after takeoff; brain relinquishes control"
                    )
                await self.harness.adapters[self.vehicle_id].pause()

            return {
                "status": "succeeded",
                "job_id": self.active_job,
                "telemetry": self.telemetry().model_dump(),
                "completion_scope": "observed_flight_telemetry",
            }
        except asyncio.CancelledError:
            await self.harness.cancel(self.active_job)
            await asyncio.shield(self.harness.jobs[self.active_job])
            if self.harness.journal.get(self.active_job)["status"] == "unknown":
                self.interrupted_jobs.append(self.active_job)
            self.expected_modes = {
                ((4, 3) if self.session.config.backend == "px4" else (17, 0))
            }
            raise
        finally:
            self.active_job = None

    async def gimbal(self, command):
        if self.config.gimbal_device_id is None:
            raise HarnessError("gimbal manager not configured")
        if not self.camera_attitude():
            raise HarnessError("gimbal pose unavailable")
        yaw = command.get("yaw_deg")
        started = time.monotonic()
        await self.session.command(
            1000,
            [
                command["pitch_deg"],
                math.nan if yaw is None else yaw,
                math.nan,
                math.nan,
                16,
                0,
                self.config.gimbal_device_id,
            ],
        )

        def reached():
            self.telemetry()
            pose = self.camera_attitude()
            fresh = any(
                stamp >= started and key[1] == self.config.gimbal_device_id
                for key, (_, stamp) in self.session.payload.gimbals.items()
            )
            return (
                fresh
                and pose
                and abs(pose["pitch"] - command["pitch_deg"]) < 2
                and (yaw is None or abs((pose["yaw"] - yaw + 180) % 360 - 180) < 2)
            )

        await self.session.wait(reached, 5, stable_s=0.3)

    async def hold(self):
        if not self.execution_enabled:
            return {"status": "observation_only"}
        try:
            self.telemetry()
        except Uncertain:
            await self.session.stop_stream()
            raise
        # A stop is cleanup, not a new admitted flight job. Unknown actions remain unknown.
        self.cleanup_active = True
        try:
            was_grounded = not self.telemetry().airborne and not self.telemetry().armed
            await self.harness.adapters[self.vehicle_id].pause()
            self.expected_modes = (
                {self.session.mode()}
                if was_grounded
                else {((4, 3) if self.session.config.backend == "px4" else (17, 0))}
            )

            def stationary():
                t = self.telemetry()
                return (not t.armed and not t.airborne) or math.sqrt(
                    sum(v * v for v in t.velocity_ned)
                ) < 0.3

            await self.session.wait(stationary, 5, stable_s=0.5)
            return {
                "status": "succeeded",
                "completion_scope": "observed_native_hold",
                "interrupted_jobs": self.interrupted_jobs.copy(),
            }
        finally:
            self.cleanup_active = False

    async def reconcile(self):
        s = self.session
        s.require()
        if s.armed() or not s.grounded():
            raise HarnessError(
                "fresh ground/disarmed state required to release an uncertain mission"
            )
        for row in self.harness.journal.db.execute(
            "SELECT id FROM jobs WHERE status='unknown'"
        ).fetchall():
            job = self.harness.journal.get(row[0])
            if any(a["vehicle_id"] == self.vehicle_id for a in job["plan"]["actions"]):
                await self.harness.reconcile(row[0])
        self.interrupted_jobs.clear()

    async def close(self):
        pass
