"""Operator-side ground-truth scoring. Never export target coordinates to the planner."""

import math


class EpisodeEvaluation:
    def __init__(self, target_ned, telemetry, radius):
        self._target = tuple(target_ned)
        self._start = tuple(telemetry.position_ned)
        self._last = self._start
        self.initial_distance = math.dist(self._start, self._target)
        self.path_m = 0.0
        self.radius = radius
        self.current = telemetry

    def update(self, telemetry):
        self.path_m += math.dist(self._last, telemetry.position_ned)
        self._last = tuple(telemetry.position_ned)
        self.current = telemetry

    def snapshot(self, status):
        distance = math.dist(self.current.position_ned, self._target)
        hovering = (
            self.current.airborne
            and math.sqrt(sum(v * v for v in self.current.velocity_ned)) < 0.3
        )
        return {
            "source": "sandbox_ground_truth_operator_only",
            "initial_distance_m": self.initial_distance,
            "current_slant_distance_m": distance,
            "flown_path_m": self.path_m,
            "net_displacement_m": math.dist(self._start, self.current.position_ned),
            "monitor_radius_m": self.radius,
            "hovering": hovering,
            "passed": status == "succeeded"
            and 180 <= self.initial_distance <= 230
            and distance <= self.radius
            and hovering,
        }
