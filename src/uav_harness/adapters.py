"""Business actions → platform commands, with completion from actual telemetry."""
from __future__ import annotations

import asyncio
import math
import time
from abc import ABC, abstractmethod
from .errors import HarnessError, Uncertain
from .transport import mav


class Adapter(ABC):
    def __init__(self, session):
        self.s = session

    def validate(self, action):
        s, c = self.s, self.s.config
        if action.action not in s.actions:
            raise HarnessError(f"{action.action} unavailable for {c.backend}/{c.profile}")
        if action.action in ("flight.takeoff", "bench.takeoff_probe") and action.params.altitude_home_m > c.max_height_m:
            raise HarnessError("takeoff exceeds configured height")
        if action.action == "flight.move_relative" and math.sqrt(sum(x*x for x in (action.params.x_m, action.params.y_m, action.params.z_m))) > c.max_relative_distance_m:
            raise HarnessError("relative movement exceeds configured distance")
        if action.action == "bench.motor_test" and action.timeout_s < action.params.duration_s + 8:
            raise HarnessError("motor test timeout must include command and stop observation")
        if action.action == "bench.takeoff_probe" and action.timeout_s < 45:
            raise HarnessError("takeoff probe needs 45 seconds including ground/disarm cleanup")

    async def execute(self, action):
        self.validate(action)
        self.s.require(control=True)
        name = action.action
        if name == "vehicle.arm":
            await self.s.normal_arm()
        elif name == "vehicle.disarm":
            await self.s.normal_disarm()
        elif name == "flight.takeoff":
            await self.takeoff(action.params.altitude_home_m)
        elif name == "flight.move_relative":
            await self.move(action.params)
        elif name == "flight.hold":
            await self.hold(action.params.duration_s)
        elif name == "flight.land":
            await self.land()
        elif name == "bench.motor_test":
            return await self.motor_test(action.params)
        elif name == "bench.takeoff_probe":
            return await self.takeoff_probe(action.params.altitude_home_m)
        return {"completion_scope": "observed_state", "state": self.s.snapshot()}

    async def wait_target(self, target, stable=.5):
        def reached():
            position, velocity = self.s.position()
            return math.dist(position, target) <= self.s.config.position_tolerance_m and math.sqrt(sum(v*v for v in velocity)) <= .3
        await self.s.wait(reached, 120, stable_s=stable)

    async def takeoff(self, altitude):
        s = self.s
        s.require_ground(disarmed=False)
        position, _ = s.position()
        home = s.get("HOME_POSITION", fresh=False)
        if not all(math.isfinite(v) for v in (home.x, home.y, home.z)):
            raise HarnessError("finite Home local coordinates required")
        target = (position[0], position[1], home.z-altitude)
        await self.prepare_takeoff()
        if not s.armed():
            await s.command(400, [1, 0])
            await s.wait(s.armed, 3)
        await s.command(22, self.takeoff_values(altitude, home))
        await self.wait_target(target)
        await s.wait(lambda: s.get("EXTENDED_SYS_STATE").landed_state == mav.MAV_LANDED_STATE_IN_AIR, 3)

    @abstractmethod
    async def prepare_takeoff(self): ...

    @abstractmethod
    def takeoff_values(self, altitude, home): ...

    @abstractmethod
    async def movement_mode(self, position, yaw): ...

    @abstractmethod
    async def land_mode(self): ...

    @abstractmethod
    async def pause_mode(self): ...

    async def move(self, params):
        s = self.s
        position, _ = s.position()
        if not s.armed() or s.grounded():
            raise HarnessError("relative movement requires airborne/armed")
        yaw = s.yaw()
        if params.frame == "local_enu":
            offset = (params.y_m, params.x_m, -params.z_m)
        else:
            offset = (params.x_m*math.cos(yaw)+params.y_m*math.sin(yaw),
                      params.x_m*math.sin(yaw)-params.y_m*math.cos(yaw), -params.z_m)
        target = tuple(p+d for p, d in zip(position, offset))
        home = s.get("HOME_POSITION", fresh=False)
        if not 0 <= home.z-target[2] <= s.config.max_height_m:
            raise HarnessError("movement target outside configured Home height")
        await self.movement_mode(position, yaw)
        s.target = (target, yaw)  # Freeze once; never accumulate relative offset per tick.
        await self.wait_target(target)

    async def hold(self, duration):
        position, _ = self.s.position()
        if not self.s.armed() or self.s.grounded():
            raise HarnessError("hold requires airborne/armed")
        await self.movement_mode(position, self.s.yaw())
        await self.wait_target(position, duration)

    async def land(self):
        s = self.s
        s.require(control=True)
        if s.grounded() and not s.armed():
            await s.stop_stream()
            return
        await self.land_mode()
        await s.stop_stream()
        # Wait for the controller's ground determination; never force disarm.
        await s.wait(s.grounded, 30)
        await s.normal_disarm()
        await s.wait(lambda: s.grounded() and not s.armed(), 3)

    async def pause(self):
        s = self.s
        s.require(control=True)
        if s.allowed_modes is not None and s.mode() not in s.allowed_modes:
            await s.stop_stream()
            raise Uncertain("manual mode change; automatic pause relinquished control")
        if s.grounded() and not s.armed():
            await s.stop_stream()
            return
        if s.config.profile == "model_bench":
            await self.land()
        else:
            await self.pause_mode()
            await s.stop_stream()

    async def motor_test(self, params):
        s = self.s
        s.require_ground()
        protocol = await s.read_parameter("MOT_PWM_TYPE")
        low = await s.read_parameter("MOT_PWM_MIN")
        high = await s.read_parameter("MOT_PWM_MAX")
        if protocol != 0 or not low <= params.pwm_us <= min(high, low+.3*(high-low)):
            raise HarnessError("motor probe requires attested normal PWM and bounded output")
        start = time.monotonic()
        try:
            await s.command(209, [params.motor_sequence, 1, params.pwm_us, params.duration_s, 1, 0, 0])
            await asyncio.sleep(params.duration_s + .5)
        finally:
            # Controller timer stops this test even if HTTP/task disappears.
            remaining = start+params.duration_s+1-time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            await s.wait(lambda: s.grounded() and not s.armed(), 3)
        outputs = [m for m, at in s.history if at >= start and m.get_type() == "SERVO_OUTPUT_RAW"]
        if not outputs or not any(getattr(m, f"servo{i}_raw") == params.pwm_us for m in outputs for i in range(1,9)):
            raise Uncertain("motor command accepted without output telemetry effect")
        return {"completion_scope": "bench_output_telemetry", "physical_rotation_verified": False,
                "output_pwm_us": params.pwm_us, "final_state": s.snapshot()}

    async def takeoff_probe(self, altitude):
        s = self.s
        s.require_ground()
        s.altitude_estimate()
        original = s.mode()[0]
        start = time.monotonic()
        baseline = await s.read_parameter("MOT_PWM_MIN")
        try:
            await s.normal_arm()
            start = time.monotonic()
            await s.command(22, [0, 0, 1, 0, 0, 0, altitude])
            await s.wait(lambda: any(getattr(m, f"servo{i}_raw") > baseline for m, at in s.history
                                    if at >= start and m.get_type() == "SERVO_OUTPUT_RAW"
                                    for i in range(1,9)), 3)
        finally:
            await self.land()
            await s.set_mode(original)
        return {"completion_scope": "bench_command_and_output_telemetry", "altitude_reached": False,
                "physical_rotation_verified": False, "final_state": s.snapshot()}


class ArduPilotAdapter(Adapter):
    platform = "ardupilot"

    async def prepare_takeoff(self):
        await self.s.set_mode(4)  # GUIDED

    def takeoff_values(self, altitude, home):
        return [0, 0, 0, 0, 0, 0, altitude]  # metres above Home

    async def movement_mode(self, position, yaw):
        await self.s.set_mode(4)
        await self.s.start_stream(position, yaw)

    async def land_mode(self):
        await self.s.set_mode(9)

    async def pause_mode(self):
        await self.s.set_mode(17)


class PX4Adapter(Adapter):
    platform = "px4"

    async def prepare_takeoff(self):
        self.s.allowed_modes = {self.s.mode(), (4, 2), (4, 3)}

    def takeoff_values(self, altitude, home):
        return [0, 0, 0, math.nan, math.nan, math.nan, home.altitude/1000+altitude]  # AMSL

    async def movement_mode(self, position, yaw):
        self.s.allowed_modes = {self.s.mode(), (6, 0)}
        await self.s.start_stream(position, yaw)
        await asyncio.sleep(1.1)  # PX4 proof-of-life before OFFBOARD admission.
        await self.s.set_mode(6)

    async def land_mode(self):
        self.s.allowed_modes = {self.s.mode(), (4, 6)}
        await self.s.command(21, [0, 0, 0, math.nan, math.nan, math.nan, math.nan])
        await self.s.wait(lambda: self.s.mode() == (4, 6) or (self.s.grounded() and not self.s.armed()), 3)

    async def pause_mode(self):
        await self.s.set_mode(4, 3)  # AUTO.LOITER


def make_adapter(session):
    return (ArduPilotAdapter if session.config.backend == "ardupilot" else PX4Adapter)(session)
