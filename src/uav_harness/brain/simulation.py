"""Geometric camera and kinematic vehicle. No scripted detections, decisions, or completion."""

import asyncio
import io
import math
import random
import secrets
import time
import uuid
from PIL import Image, ImageDraw
from .contracts import Camera, Observation, Telemetry
from .geometry import project
from ..errors import HarnessError


class SimulatedPort:
    mode = "geometric_simulation"

    def __init__(self, vehicle_id, seed=None):
        self.vehicle_id = vehicle_id
        self.seed = seed if seed is not None else secrets.randbits(32)
        rng = random.Random(self.seed)
        self.position = [0.0, 0.0, 0.0]
        self.heading = 0.0
        self.pitch = -35.0
        self.gimbal_yaw = None
        self.velocity = [0.0, 0.0, 0.0]
        self.executions = []
        self.hidden = False
        self.people = []
        for color in ("red", "blue"):
            angle = math.radians(rng.uniform(20, 340))
            distance = rng.uniform(55, 85)
            self.people.append(
                (distance * math.cos(angle), distance * math.sin(angle), color)
            )

    def telemetry(self):
        return Telemetry(
            observed_at=time.time(),
            position_ned=tuple(self.position),
            velocity_ned=tuple(self.velocity),
            heading_deg=self.heading,
            armed=self.position[2] < -0.1,
            airborne=self.position[2] < -0.1,
            battery_percent=90,
            source="kinematic_simulation",
        )

    def camera(self):
        return Camera(
            width=1024,
            height=768,
            fx=1236,
            fy=1236,
            cx=512,
            cy=384,
            yaw_deg=self.heading if self.gimbal_yaw is None else self.gimbal_yaw,
            pitch_deg=self.pitch,
            ground_down_m=0,
            ground_uncertainty_m=0.3,
            source="geometric_camera",
            timing="simulated_capture",
            position_ned=(
                self.position[0],
                self.position[1],
                min(self.position[2], -0.5),
            ),
        )

    def camera_pose(self):
        return self.camera().model_dump()

    async def observe(self):
        camera = self.camera()
        image = self.render(camera)
        return Observation(
            observation_id=uuid.uuid4().hex,
            vehicle_id=self.vehicle_id,
            captured_at=time.time(),
            telemetry=self.telemetry(),
            camera=camera,
            image=image,
            source=self.mode,
            capabilities=[
                "takeoff",
                "yaw",
                "gimbal",
                "approach",
                "search_move",
                "altitude",
                "hold",
                "observe",
                "finish",
            ],
        )

    def render(self, c):
        image = Image.new("RGB", (c.width, c.height), (146, 161, 123))
        draw = ImageDraw.Draw(image)
        # Render actual projected ground lines; camera motion changes the view.
        pos = c.position_ned or tuple(self.position)
        for axis in (0, 1):
            for offset in range(-300, 301, 20):
                coords = []
                for t in range(-300, 301, 3):
                    p = project(c, pos, (offset, t, 0) if axis == 0 else (t, offset, 0))
                    if p and -1000 < p[0] < 2024 and -1000 < p[1] < 1768:
                        coords.append((p[0], p[1]))
                    elif len(coords) > 1:
                        draw.line(coords, fill=(124, 141, 105), width=1)
                        coords = []
                if len(coords) > 1:
                    draw.line(coords, fill=(124, 141, 105), width=1)
        for n, e, color in self.people:
            if self.hidden and color == "red":
                continue
            feet = project(c, pos, (n, e, 0))
            head = project(c, pos, (n, e, -1.75))
            if not feet or not head:
                continue
            x, y = feet[:2]
            hx, hy = head[:2]
            h = math.hypot(x - hx, y - hy)
            if h < 4 or x < -50 or x > c.width + 50 or y < -50 or y > c.height + 50:
                continue
            w = max(2, h * 0.17)
            middle = (hy + y) * 0.5
            draw.line(
                [(hx - w, hy + h * 0.22), (x - w, middle), (x - w * 0.7, y)],
                fill=(37, 43, 49),
                width=max(2, int(w)),
            )
            draw.line(
                [(hx + w, hy + h * 0.22), (x + w, middle), (x + w * 0.7, y)],
                fill=(37, 43, 49),
                width=max(2, int(w)),
            )
            top, bottom = sorted((hy + h * 0.12, middle))
            draw.rectangle((hx - w, top, hx + w, bottom), fill=(222, 222, 206))
            draw.ellipse(
                (hx - w * 0.6, hy - h * 0.04, hx + w * 0.6, hy + h * 0.16),
                fill=(206, 158, 122),
            )
            hat = (213, 32, 35) if color == "red" else (27, 72, 213)
            draw.ellipse((hx - w, hy - h * 0.12, hx + w, hy + h * 0.06), fill=hat)
        draw.rectangle((0, 0, c.width, 28), fill=(26, 39, 41))
        draw.text(
            (12, 8),
            "GEOMETRIC CAMERA  |  SYNTHETIC IMAGE  |  NOT A REAL FLIGHT",
            fill=(221, 232, 227),
        )
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        return out.getvalue()

    async def execute(self, command):
        self.executions.append(command.copy())
        action = command["action"]
        if action in ("takeoff", "altitude"):
            target = [*self.position[:2], -command["altitude_m"]]
        elif action == "move":
            target = [p + d for p, d in zip(self.position, command["offset_ned"])]
        elif action == "yaw":
            self.heading = command["heading_deg"]
            target = self.position.copy()
        elif action == "gimbal":
            self.pitch = command["pitch_deg"]
            self.gimbal_yaw = command.get("yaw_deg")
            target = self.position.copy()
        elif action in ("hold", "observe"):
            target = self.position.copy()
        else:
            raise HarnessError("unsupported simulated actuator command")
        start = self.position.copy()
        distance = math.dist(start, target)
        steps = max(1, int(distance / 2))
        if distance > 0:
            self.velocity = [(q - p) / distance * 4 for p, q in zip(start, target)]
        try:
            for i in range(steps):
                self.position = [
                    p + (q - p) * (i + 1) / steps for p, q in zip(start, target)
                ]
                await asyncio.sleep(0.015)
        finally:
            self.velocity = [0.0, 0.0, 0.0]
        return {
            "status": "succeeded",
            "command": command,
            "telemetry": self.telemetry().model_dump(),
            "completion_scope": "kinematic_simulation",
        }

    async def hold(self):
        return await self.execute({"action": "hold"})

    async def close(self):
        pass
