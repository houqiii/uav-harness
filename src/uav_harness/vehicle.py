from __future__ import annotations

import asyncio
from collections import deque
import math
import time
from .errors import HarnessError, Uncertain
from .transport import json_safe, mav


FLIGHT_ACTIONS = {"vehicle.arm", "vehicle.disarm", "flight.takeoff",
                  "flight.move_relative", "flight.hold", "flight.land"}
BENCH_ACTIONS = {"vehicle.arm", "vehicle.disarm", "flight.land",
                 "bench.motor_test", "bench.takeoff_probe"}


class VehicleSession:
    def __init__(self, config, transport, settings):
        self.config, self.transport, self.settings = config, transport, settings
        self.latest = {}
        self.history = deque(maxlen=4000)
        self.parameters = {}
        self.fault = None
        self.boot_ms = None
        self.target = None
        self.stream_task = None
        self.stream_fault = None
        self.allowed_modes = None
        transport.listeners.append(self.receive)

    @property
    def actions(self):
        return BENCH_ACTIONS if self.config.profile == "model_bench" else FLIGHT_ACTIONS

    def receive(self, msg, stamp):
        if (msg.get_srcSystem(), msg.get_srcComponent()) != (self.config.system_id, self.config.component_id):
            return
        kind = msg.get_type()
        if kind == "HEARTBEAT" and msg.autopilot != self.autopilot:
            self.fault = "autopilot identity changed"
        if kind == "SYSTEM_TIME":
            if self.boot_ms is not None and msg.time_boot_ms + 1000 < self.boot_ms:
                self.fault = "controller rebooted; reconnect and reconcile jobs"
            self.boot_ms = msg.time_boot_ms
        self.latest[kind] = (msg, stamp)
        if kind == "PARAM_VALUE":
            self.parameters[msg.param_id] = (msg, stamp)
        if kind in ("SERVO_OUTPUT_RAW", "STATUSTEXT", "COMMAND_ACK"):
            self.history.append((msg, stamp))

    @property
    def autopilot(self):
        return mav.MAV_AUTOPILOT_ARDUPILOTMEGA if self.config.backend == "ardupilot" else mav.MAV_AUTOPILOT_PX4

    def get(self, kind, fresh=True):
        item = self.latest.get(kind)
        if not item or (fresh and time.monotonic()-item[1] > self.settings.telemetry_timeout_s):
            raise Uncertain(f"{self.config.vehicle_id}: missing/stale {kind}")
        return item[0]

    def require(self, control=False):
        if self.fault or self.transport.fault:
            raise Uncertain(self.fault or self.transport.fault)
        hb = self.get("HEARTBEAT")
        if hb.autopilot != self.autopilot or hb.type not in (2, 3, 4, 13, 14, 15, 29):
            raise HarnessError("expected multicopter/autopilot identity required")
        version = self.get("AUTOPILOT_VERSION", fresh=False)
        observed = (version.flight_sw_version >> 24, (version.flight_sw_version >> 16) & 255)
        expected = self.config.firmware or ((4, 5) if self.config.backend == "ardupilot" else (1, 16))
        if observed != expected:
            raise HarnessError(f"firmware mismatch: expected {expected}, got {observed}")
        if self.config.expected_board_id is not None and version.board_version >> 16 != self.config.expected_board_id:
            raise HarnessError("controller board ID mismatch")
        if control and not self.settings.allow_control:
            raise HarnessError("actions disabled by service configuration")
        return hb

    async def initialize(self):
        await self.wait(lambda: "HEARTBEAT" in self.latest, 8, check=False)
        if self.get("HEARTBEAT").autopilot != self.autopilot:
            raise HarnessError("configured backend does not match flight controller")
        await self.command(mav.MAV_CMD_REQUEST_MESSAGE, [148], control=False)
        await self.wait(lambda: "AUTOPILOT_VERSION" in self.latest, 3, check=False)
        self.require()
        # Telemetry requests replace the message subscriptions QGC used to provide.
        estimator = 193 if self.config.backend == "ardupilot" else 230
        for message_id, hz in ((1, 2), (30, 10), (32, 20), (245, 5), (estimator, 5), (36, 5)):
            await self.command(mav.MAV_CMD_SET_MESSAGE_INTERVAL, [message_id, 1e6/hz], control=False)
        await self.command(mav.MAV_CMD_REQUEST_MESSAGE, [242], control=False)

    async def command(self, command, values=(), control=True):
        if control:
            self.require(control=True)
        return await self.transport.command(self.config.system_id, self.config.component_id, command, values)

    async def read_parameter(self, name):
        start = time.monotonic()
        self.transport.tx.param_request_read_send(self.config.system_id, self.config.component_id, name.encode(), -1)
        await self.wait(lambda: name in self.parameters and self.parameters[name][1] >= start, 3)
        return self.parameters[name][0].param_value

    async def wait(self, predicate, timeout, check=True, stable_s=0):
        deadline, since = time.monotonic()+timeout, None
        while time.monotonic() < deadline:
            if check:
                self.require()
            if predicate():
                since = since or time.monotonic()
                if time.monotonic()-since >= stable_s:
                    return
            else:
                since = None
            await asyncio.sleep(.025)
        raise Uncertain("expected telemetry effect not observed before timeout")

    def grounded(self):
        return self.get("EXTENDED_SYS_STATE").landed_state == mav.MAV_LANDED_STATE_ON_GROUND

    def armed(self):
        return bool(self.get("HEARTBEAT").base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED)

    def require_ground(self, disarmed=True):
        self.require(control=True)
        if not self.grounded() or (disarmed and self.armed()):
            raise HarnessError("fresh ground/disarmed state required")

    def altitude_estimate(self):
        kind = "EKF_STATUS_REPORT" if self.config.backend == "ardupilot" else "ESTIMATOR_STATUS"
        flags = self.get(kind).flags
        if flags & 33 != 33 or flags & 1024:
            raise HarnessError("valid attitude/vertical position estimates required")
        return flags

    def position(self):
        self.require(control=True)
        if self.get("HEARTBEAT").system_status in (5, 6, 7):
            raise HarnessError("flight controller reports critical/emergency state")
        power = self.get("SYS_STATUS")
        if not self.config.min_battery_percent <= power.battery_remaining <= 100 or power.voltage_battery in (0, 65535):
            raise HarnessError("flight requires known battery within configured range")
        flags = self.altitude_estimate()
        if not flags & (8 | 16) or flags & 128:
            raise HarnessError("valid horizontal position estimate required")
        pos = self.get("LOCAL_POSITION_NED")
        values = (pos.x, pos.y, pos.z, pos.vx, pos.vy, pos.vz)
        if not all(math.isfinite(x) for x in values):
            raise HarnessError("nonfinite local position")
        if self.stream_fault:
            raise Uncertain(self.stream_fault)
        if self.allowed_modes is not None and self.mode() not in self.allowed_modes:
            raise Uncertain("mode changed outside Harness control; possible manual takeover")
        return values[:3], values[3:]

    def yaw(self):
        value = self.get("ATTITUDE").yaw
        if not math.isfinite(value):
            raise HarnessError("invalid yaw")
        return value

    def mode(self):
        custom = self.get("HEARTBEAT").custom_mode
        return (custom, 0) if self.config.backend == "ardupilot" else ((custom >> 16) & 255, (custom >> 24) & 255)

    async def set_mode(self, main, sub=0):
        previous = self.mode()
        self.allowed_modes = {previous, (main, sub)}
        await self.command(mav.MAV_CMD_DO_SET_MODE, [1, main, sub])
        await self.wait(lambda: self.mode() == (main, sub), 3)
        self.allowed_modes = {(main, sub)}

    async def normal_arm(self):
        self.require_ground()
        self.altitude_estimate()
        if self.config.profile == "model_bench":
            await self.set_mode(2)
        else:
            self.position()
            if self.config.backend == "ardupilot":
                await self.set_mode(4)
        await self.command(mav.MAV_CMD_COMPONENT_ARM_DISARM, [1, 0])
        await self.wait(self.armed, 3)

    async def normal_disarm(self):
        self.require(control=True)
        if not self.grounded():
            raise HarnessError("normal disarm requires ON_GROUND")
        if self.armed():
            await self.command(mav.MAV_CMD_COMPONENT_ARM_DISARM, [0, 0])
            await self.wait(lambda: not self.armed(), 3)

    async def start_stream(self, target, yaw):
        self.target = (tuple(target), yaw)
        if not self.stream_task or self.stream_task.done():
            self.stream_fault = None
            self.stream_task = asyncio.create_task(self._stream())

    async def _stream(self):
        try:
            while self.target:
                self.position()
                self.transport.setpoint(self.config.system_id, self.config.component_id, *self.target)
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except HarnessError as exc:
            self.stream_fault = str(exc)
            self.target = None

    async def stop_stream(self):
        self.target = None
        if self.stream_task:
            self.stream_task.cancel()
            await asyncio.gather(self.stream_task, return_exceptions=True)
            self.stream_task = None
        self.allowed_modes = None
        self.stream_fault = None

    def snapshot(self):
        def fields(kind, fresh=True):
            try:
                return json_safe(self.get(kind, fresh).to_dict())
            except HarnessError:
                return None
        try:
            self.require()
            connected, error = True, None
        except HarnessError as exc:
            connected, error = False, str(exc)
        return {"vehicle_id": self.config.vehicle_id, "backend": self.config.backend,
                "profile": self.config.profile, "connected": connected,
                "control_enabled": self.settings.allow_control, "error": error,
                "actions": sorted(self.actions), "heartbeat": fields("HEARTBEAT"),
                "landed": fields("EXTENDED_SYS_STATE"), "local_position_ned": fields("LOCAL_POSITION_NED"),
                "attitude": fields("ATTITUDE"), "home": fields("HOME_POSITION", False),
                "sys_status": fields("SYS_STATUS"),
                "firmware": fields("AUTOPILOT_VERSION", False),
                "completion_policy": "bench evidence only" if self.config.profile == "model_bench" else "observed flight telemetry"}
