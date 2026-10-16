"""Observed Drone4D flight and gimbal tools, expressed in the shared NED contract."""

import asyncio
import math
import time
import uuid
from ..brain.contracts import Telemetry, Observation
from ..errors import HarnessError, Uncertain


def enu_to_ned(v):
    return (v[1], v[0], -v[2])


def heading_from_enu(yaw):
    return (90 - math.degrees(yaw)) % 360


def world_aim_to_gimbal(quaternion, heading_deg, pitch_deg):
    """World NED look direction -> native FLU gimbal azimuth/elevation.

    Matches Drone4D's world_from_body * Rz(azimuth) * Ry(-elevation),
    including aircraft roll/pitch. Native EO mount axes coincide with body FLU.
    """
    x, y, z, w = quaternion
    norm = math.sqrt(sum(v * v for v in quaternion))
    if norm < 1e-8:
        raise HarnessError("invalid aircraft attitude for camera aiming")
    x, y, z, w = (v / norm for v in (x, y, z, w))
    h, p = math.radians(heading_deg), math.radians(pitch_deg)
    world = (math.cos(p) * math.sin(h), math.cos(p) * math.cos(h), math.sin(p))
    rotation = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )
    body = [sum(rotation[j][i] * world[j] for j in range(3)) for i in range(3)]
    return math.atan2(body[1], body[0]), math.atan2(
        body[2], math.hypot(body[0], body[1])
    )


def telemetry_from_state(wire):
    s = wire["state"]
    p = s["pos_enu_m"]
    v = s["vel_enu_mps"]
    h = s["home_enu_m"]
    q = s["q_xyzw"]
    x, y, z, w = q
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return Telemetry(
        observed_at=time.time(),
        position_ned=enu_to_ned(p),
        velocity_ned=enu_to_ned(v),
        home_down_m=-h[2],
        home_east_m=h[0],
        home_north_m=h[1],
        heading_deg=heading_from_enu(yaw),
        armed=s["flight_state"] not in ("DISARMED", "LANDED", "UNKNOWN"),
        airborne=s["in_air"],
        battery_percent=((wire.get("state_ext") or {}).get("battery") or {}).get(
            "soc_pct"
        ),
        source="drone4d_fleetsim",
    )


class Drone4DPort:
    mode = "drone4d"
    execution_enabled = True

    def __init__(self, service, vehicle_id):
        self.service = service
        self.vehicle_id = vehicle_id
        self.latest = None
        self.latest_at = 0
        self.last_camera = None
        self.stopping = False
        self.command_task = None
        self.latest_sensors = None
        self.sensors_at = 0
        self.capture_signature = None
        self.gimbal_intent = None
        self.gimbal_renewed_ns = 0
        self.payload_lock = asyncio.Lock()

    def telemetry(self):
        if not self.latest or time.monotonic() - self.latest_at > 3:
            raise Uncertain(
                "Drone4D telemetry missing/stale; no inferred aircraft state"
            )
        return telemetry_from_state(self.latest)

    async def refresh(self):
        c = self.service.client
        self.latest = await c.call(c.session.vehicles.state, self.vehicle_id)
        self.latest_at = time.monotonic()
        if time.monotonic() - self.sensors_at > 1:
            self.latest_sensors = await c.call(
                c.session.vehicles.sensors, self.vehicle_id
            )
            self.sensors_at = time.monotonic()
        return self.telemetry()

    def pose_signature(self):
        t = self.telemetry()
        if not self.latest_sensors or time.monotonic() - self.sensors_at > 3:
            raise Uncertain("fresh Drone4D gimbal telemetry unavailable")
        g = self.latest_sensors["payload"]["gimbal"]
        x, y, z, w = self.latest["state"]["q_xyzw"]
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        pitch = math.asin(max(-1, min(1, 2 * (w * y - z * x))))
        signature = (
            t.heading_deg,
            math.degrees(roll),
            math.degrees(pitch),
            math.degrees(g["pitch_rad"]),
            math.degrees(g["yaw_rad"]),
        )
        eo = next(
            (
                s
                for s in self.latest_sensors["payload"].get("sensors", [])
                if s.get("sensor") == "eo"
            ),
            None,
        )
        return signature + ((math.degrees(eo["hfov_rad"]),) if eo else ())

    def camera_pose(self):
        if not self.last_camera:
            raise HarnessError("Drone4D camera pose unavailable")
        now = self.pose_signature()
        if self.capture_signature and (
            len(now) != len(self.capture_signature)
            or any(
                abs((a - b + 180) % 360 - 180) > 2
                for a, b in zip(now, self.capture_signature)
            )
        ):
            raise HarnessError(
                "Drone4D camera pose changed during inference; reobserve"
            )
        return self.last_camera.model_dump()

    def zoom_available(self):
        return any(
            s.get("type") == "eo_zoom" and s.get("spec_ref") == "eo_gx40@1.0.0"
            for s in (self.latest_sensors or {}).get("sensors", [])
        )

    async def observe(self):
        await self.refresh()
        frame = await self.service.capture.capture(self.vehicle_id)
        t = await self.refresh()
        # Match native rendering's simulation instant to actual telemetry (allow small interpolation delay).
        sim_age = (int(self.latest["t_sim_ns"]) - frame["t_sim_ns"]) / 1e9
        if not -1 <= sim_age <= 3:
            raise HarnessError(
                "Drone4D rendered frame is not synchronized with simulation telemetry"
            )
        if (
            math.dist(t.position_ned, frame["camera"].position_ned or t.position_ned)
            > 0.8
        ):
            raise HarnessError(
                "Drone4D aircraft moved during capture; acquire stationary frame"
            )
        self.last_camera = frame["camera"]
        if self.zoom_available():
            eo = next(
                (
                    s
                    for s in self.latest_sensors["payload"].get("sensors", [])
                    if s.get("sensor") == "eo"
                ),
                {},
            )
            zoom = (eo.get("f_mm") or 0) / 4.8
            if 1 <= zoom <= 5:
                self.last_camera = self.last_camera.model_copy(update={"zoom_x": zoom})
        self.capture_signature = self.pose_signature()
        return Observation(
            observation_id=uuid.uuid4().hex,
            vehicle_id=self.vehicle_id,
            captured_at=frame["captured_at"],
            telemetry=t,
            camera=self.last_camera,
            image=frame["image"],
            source="drone4d_native",
            search_camera_defaults={
                "pitch_deg": self.service.config.initial_camera_pitch_deg,
                "zoom_x": self.service.config.initial_zoom_x,
            },
            capabilities=[
                "takeoff",
                "yaw",
                "gimbal",
                "aim_target",
                "altitude",
                "approach",
                "observe",
                "hold",
                "finish",
            ]
            + (
                ["search_move"]
                if self.service.config.mission_policy.allow_search_reposition
                else []
            )
            + (["zoom"] if self.zoom_available() else [])
            + (
                ["inspect_target"]
                if self.service.config.mission_policy.allow_candidate_inspection
                else []
            ),
        )

    async def command(self, name, **args):
        if name != "gimbal":
            return await self._send_command(name, **args)
        async with self.payload_lock:
            result = await self._send_command(name, **args)
            self.gimbal_intent = dict(args)
            self.gimbal_renewed_ns = int(self.latest["t_sim_ns"]) if self.latest else 0
            return result

    async def renew_payload(self):
        # Drone4D operator pointing expires after 60 simulation seconds.
        # Renew the last accepted intent; never replace the planner's chosen angle.
        async with self.payload_lock:
            if (
                self.gimbal_intent
                and self.latest
                and (
                    int(self.latest["t_sim_ns"]) < self.gimbal_renewed_ns
                    or int(self.latest["t_sim_ns"]) - self.gimbal_renewed_ns
                    >= 20_000_000_000
                )
            ):
                await self._send_command("gimbal", **self.gimbal_intent)
                self.gimbal_renewed_ns = int(self.latest["t_sim_ns"])

    async def _send_command(self, name, **args):
        c = self.service.client
        rpc = asyncio.create_task(
            c.call(c.session.command, self.vehicle_id, name, timeout_s=10, **args)
        )
        self.command_task = rpc
        try:
            result = await asyncio.shield(rpc)
        except asyncio.CancelledError:
            # A cancelled to_thread call can still send. Keep ownership through
            # repeated cancellations until both the RPC and final stop settle.
            async def cleanup():
                try:
                    await c.call(
                        c.session.command, self.vehicle_id, "hover", timeout_s=10
                    )
                except HarnessError:
                    pass
                await asyncio.gather(rpc, return_exceptions=True)
                try:
                    await self.hold()
                except HarnessError:
                    raise Uncertain(
                        "Drone4D interrupted SDK call drained but hover unconfirmed"
                    ) from None

            await settle_cleanup(cleanup())
            raise
        finally:
            self.command_task = None
        if result.get("status") in ("failed", "rejected", "cancelled"):
            raise HarnessError(
                "Drone4D command "
                + name
                + " "
                + str(result.get("detail", result.get("message", "rejected")))
            )
        return result

    async def wait(self, predicate, timeout=None):
        timeout = timeout or self.service.config.action_timeout_s
        start = time.monotonic()
        stable = None
        while time.monotonic() - start < timeout:
            t = await self.refresh()
            if predicate(t):
                stable = stable or time.monotonic()
                if time.monotonic() - stable >= 0.5:
                    return t
            else:
                stable = None
            await asyncio.sleep(0.5)
        raise Uncertain(
            "Drone4D actual telemetry did not confirm action before deadline"
        )

    async def execute(self, cmd):
        action = cmd["action"]
        t = await self.refresh()
        receipt = None
        try:
            if action == "observe":
                return {
                    "status": "succeeded",
                    "completion_scope": "new_native_frame_requested",
                }
            if action == "takeoff":
                receipt = await self.command(
                    "takeoff", alt_m=cmd["altitude_m"], auto_arm=True
                )
                await self.wait(
                    lambda t: (
                        t.airborne
                        and abs(t.home_down_m - t.position_ned[2] - cmd["altitude_m"])
                        < 1
                    )
                )
            elif action in ("move", "altitude", "yaw"):
                target = list(t.position_ned)
                if action == "move":
                    target = [p + d for p, d in zip(target, cmd["offset_ned"])]
                if action == "altitude":
                    target[2] = t.home_down_m - cmd["altitude_m"]
                args = {
                    "pos": [target[1], target[0], -target[2]],
                    "speed_mps": self.service.config.speed_mps,
                    "tol_m": 0.5,
                    "route": "direct" if action == "yaw" else "safe_transit",
                }
                # Native FastGuard treats an instantaneous yaw error >90 degrees
                # as ELAND. Implement one planner yaw as settled <=60-degree
                # actuator segments, preserving the original position/altitude.
                heading = t.heading_deg
                receipts = []
                for _ in range(6):
                    if action == "yaw":
                        delta = (cmd["heading_deg"] - heading + 180) % 360 - 180
                        heading = (heading + max(-60, min(60, delta))) % 360
                        args["yaw_rad"] = math.atan2(
                            math.sin(math.radians(90 - heading)),
                            math.cos(math.radians(90 - heading)),
                        )
                    receipt = await self.command("goto", **args)
                    receipts.append(receipt)
                    settled = await self.wait(
                        lambda s: (
                            math.dist(s.position_ned, target) < 0.8
                            and math.sqrt(sum(v * v for v in s.velocity_ned)) < 0.4
                            and (
                                action != "yaw"
                                or abs((s.heading_deg - heading + 180) % 360 - 180) < 3
                            )
                        )
                    )
                    if action != "yaw":
                        break
                    heading = settled.heading_deg
                    if abs((cmd["heading_deg"] - heading + 180) % 360 - 180) < 3:
                        receipt = {"segments": receipts}
                        break
                else:
                    raise Uncertain(
                        "Drone4D segmented yaw did not reach requested heading"
                    )
            elif action == "gimbal":
                relative = (
                    0
                    if cmd.get("yaw_deg") is None
                    else ((cmd["yaw_deg"] - t.heading_deg + 180) % 360 - 180)
                )
                pitch_command = cmd["pitch_deg"]
                if cmd.get("world_reference"):
                    azimuth, elevation = world_aim_to_gimbal(
                        self.latest["state"]["q_xyzw"], cmd["yaw_deg"], cmd["pitch_deg"]
                    )
                    relative, pitch_command = (
                        -math.degrees(azimuth),
                        math.degrees(elevation),
                    )
                    if not -90 <= pitch_command <= 30 or abs(relative) > 150:
                        raise HarnessError(
                            "world camera aim outside native gimbal limits"
                        )
                receipt = await self.command(
                    "gimbal",
                    mode="angles",
                    pitch_rad=math.radians(pitch_command),
                    yaw_rad=-math.radians(relative),
                )
                await self.wait(
                    lambda _: (
                        abs(
                            math.degrees(
                                self.latest_sensors["payload"]["gimbal"]["pitch_rad"]
                            )
                            - pitch_command
                        )
                        < 2
                        and abs(
                            math.degrees(
                                self.latest_sensors["payload"]["gimbal"]["yaw_rad"]
                            )
                            + relative
                        )
                        < 2
                    ),
                    15,
                )
            elif action == "zoom":
                if not self.zoom_available():
                    raise HarnessError("calibrated optical zoom unavailable")
                receipt = await self.command("zoom", sensor="eo", zoom_x=cmd["zoom_x"])
                # The selected GX40 specification has a 4.8 mm wide-angle focal length.
                await self.wait(
                    lambda _: any(
                        s.get("sensor") == "eo"
                        and abs((s.get("f_mm") or 0) - 4.8 * cmd["zoom_x"]) < 0.05
                        for s in self.latest_sensors["payload"]["sensors"]
                    ),
                    15,
                )
            elif action == "hold":
                await self.hold()
            else:
                raise HarnessError("unsupported Drone4D tool")
            if action not in ("gimbal", "zoom", "hold"):
                await self.hold()
            return {
                "status": "succeeded",
                "receipt": receipt,
                "telemetry": (await self.refresh()).model_dump(),
                "completion_scope": "drone4d_observed_flight_state",
            }
        except asyncio.CancelledError:
            await settle_cleanup(self.hold())
            raise

    async def hold(self):
        t = await self.refresh()
        state = self.latest["state"]
        if (
            not t.airborne
            and state["flight_state"] in ("DISARMED", "LANDED")
            and state.get("ctrl_mode") == "IDLE"
        ):
            return {"status": "succeeded", "completion_scope": "observed_on_ground"}
        if not t.airborne and state["flight_state"] in ("PREFLIGHT", "READY"):
            receipt = await self.command("disarm")
            await self.wait(
                lambda t: (
                    not t.airborne
                    and self.latest["state"]["flight_state"] == "DISARMED"
                ),
                15,
            )
            return {
                "status": "succeeded",
                "receipt": receipt,
                "completion_scope": "drone4d_observed_disarm",
            }
        receipt = await self.command("hover")
        await self.wait(
            lambda t: (
                self.latest["state"].get("ctrl_mode") == "HOLD"
                and self.latest["state"]["flight_state"]
                not in ("TAKING_OFF", "PREFLIGHT", "READY")
                and math.sqrt(sum(v * v for v in t.velocity_ned)) < 0.3
            ),
            15,
        )
        return {
            "status": "succeeded",
            "receipt": receipt,
            "completion_scope": "drone4d_observed_hover",
        }

    async def close(self):
        pass


async def settle_cleanup(coroutine):
    task = asyncio.create_task(coroutine)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            # A second cancellation does not revoke the outstanding stop.
            continue
