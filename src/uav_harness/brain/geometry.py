"""Calibrated image rays and bounded target-relative motion, never model-guessed metres."""

import math
from .contracts import Detection
from ..errors import HarnessError


def basis(camera):
    yaw, pitch, roll = map(
        math.radians, (camera.yaw_deg, camera.pitch_deg, camera.roll_deg)
    )
    cy, sy, cp, sp, cr, sr = (
        math.cos(yaw),
        math.sin(yaw),
        math.cos(pitch),
        math.sin(pitch),
        math.cos(roll),
        math.sin(roll),
    )
    forward = (cp * cy, cp * sy, -sp)
    right0 = (-sy, cy, 0)
    down0 = (sp * cy, sp * sy, cp)
    right = tuple(cr * r + sr * d for r, d in zip(right0, down0))
    down = tuple(-sr * r + cr * d for r, d in zip(right0, down0))
    return forward, right, down


def candidate_ray(obs, detection):
    """World-space bearing only; a candidate pixel cannot establish range."""
    c = obs.camera
    left, top, right, bottom = detection.bbox
    u = (left + right) / 2000 * c.width
    v = (top + bottom) / 2000 * c.height
    forward, horizontal, down = basis(c)
    ray = [
        forward[i] + (u - c.cx) / c.fx * horizontal[i] + (v - c.cy) / c.fy * down[i]
        for i in range(3)
    ]
    norm = math.sqrt(sum(x * x for x in ray))
    return [x / norm for x in ray]


def project(camera, position, point):
    forward, right, down = basis(camera)
    delta = [a - b for a, b in zip(point, position)]
    z = sum(a * b for a, b in zip(delta, forward))
    if z <= 0.1:
        return None
    return (
        camera.cx + camera.fx * sum(a * b for a, b in zip(delta, right)) / z,
        camera.cy + camera.fy * sum(a * b for a, b in zip(delta, down)) / z,
        z,
    )


def ground_target(obs, detection):
    d = (
        Detection.model_validate(detection)
        if isinstance(detection, dict)
        else detection
    )
    c = obs.camera
    if c.ground_down_m is None or not d.ground_contact_visible:
        raise HarnessError(
            "ground geometry unavailable: calibrated ground plane and visible ground contact required"
        )
    u = (d.bbox[0] + d.bbox[2]) / 2000 * c.width
    v = d.bbox[3] / 1000 * c.height
    f, r, down = basis(c)
    ray = [
        f[i] + (u - c.cx) / c.fx * r[i] + (v - c.cy) / c.fy * down[i] for i in range(3)
    ]
    origin = c.position_ned or obs.telemetry.position_ned
    height = c.ground_down_m - origin[2]
    if height <= 0 or ray[2] < 0.12:
        raise HarnessError("ground ray unreliable near horizon or below ground")
    t = height / ray[2]
    position = [p + t * d for p, d in zip(origin, ray)]
    distance = math.dist(position[:2], obs.telemetry.position_ned[:2])
    uncertainty = max(1, distance * (c.ground_uncertainty_m / max(height, 1) + 0.025))
    return {
        "position_ned": position,
        "horizontal_distance_m": distance,
        "slant_distance_m": math.dist(position, obs.telemetry.position_ned),
        "uncertainty_m": uncertainty,
        "bearing_deg": math.degrees(
            math.atan2(
                position[1] - obs.telemetry.position_ned[1],
                position[0] - obs.telemetry.position_ned[0],
            )
        )
        % 360,
        "source": "calibrated_ground_projection",
        "observation_id": obs.observation_id,
    }


def approach_offset(obs, target, policy, requested):
    distance = target["horizontal_distance_m"]
    available = distance - target["uncertainty_m"] - policy.stand_off_m
    step = min(requested, policy.max_step_m, available)
    if step < 0.3:
        raise HarnessError(
            "at conservative standoff; hold or finish instead of approaching"
        )
    north, east = (
        target["position_ned"][i] - obs.telemetry.position_ned[i] for i in (0, 1)
    )
    return (north / distance * step, east / distance * step, 0)


def inspection_offset(obs, detection, policy, requested):
    """Sandbox-only active perception under explicit ground/person assumptions.

    A ray to a <=3m tall ground person plus 2deg angular error gives a
    conservative horizontal lower bound. This is NOT target localization and
    must never satisfy completion or replace visible-ground projection.
    """
    c = obs.camera
    if not policy.allow_candidate_inspection or c.ground_down_m is None:
        raise HarnessError(
            "candidate inspection requires enabled sandbox policy and calibrated ground"
        )
    if not c.zoom_x or c.zoom_x < 1.5:
        raise HarnessError("magnify the visible candidate before inspection motion")
    ray = candidate_ray(obs, detection)
    horizontal = math.hypot(ray[0], ray[1])
    depression = math.atan2(ray[2], horizontal)
    if not math.radians(5) <= depression <= math.radians(70):
        raise HarnessError("candidate ray unsuitable for bounded inspection")
    origin = c.position_ned or obs.telemetry.position_ned
    available_height = c.ground_down_m - origin[2] - c.ground_uncertainty_m - 3
    if available_height <= 0:
        raise HarnessError("insufficient height bound for candidate inspection")
    lower_range = available_height / math.tan(depression + math.radians(2))
    step = min(
        requested,
        policy.inspection_step_m,
        policy.max_step_m,
        lower_range - policy.stand_off_m,
    )
    if step < 0.3:
        raise HarnessError(
            "candidate too close for ungrounded inspection; obtain range evidence or hold"
        )
    return (ray[0] / horizontal * step, ray[1] / horizontal * step, 0)
