from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import Field, field_validator, model_validator
from ..contracts import Contract, Id


class Policy(Contract):
    allow_search_reposition: bool = False
    allow_candidate_inspection: bool = False
    inspection_step_m: float = Field(default=15, gt=0, le=15)
    search_altitude_m: float = Field(default=50, gt=0, le=100)
    min_altitude_m: float = Field(default=10, gt=0, le=100)
    max_altitude_m: float = Field(default=80, gt=0, le=100)
    scan_step_deg: float = Field(default=30, ge=10, le=90)
    max_step_m: float = Field(default=10, gt=0, le=30)
    stand_off_m: float = Field(default=20, ge=10, le=100)
    max_radius_m: float = Field(default=250, gt=0, le=500)
    max_rounds: int = Field(default=60, ge=1, le=100)
    max_duration_s: float = Field(default=900, gt=0, le=1800)
    max_observation_age_s: float = Field(default=30, gt=0, le=60)
    monitor_radius_m: float | None = Field(default=None, ge=10, le=100)
    distance_metric: Literal["horizontal", "slant"] = "horizontal"

    @model_validator(mode="after")
    def altitude_bounds(self):
        if not self.min_altitude_m <= self.search_altitude_m <= self.max_altitude_m:
            raise ValueError("search altitude must be within altitude bounds")
        return self


class MissionRequest(Contract):
    query: str = Field(min_length=1, max_length=5000)
    vehicle_id: Id
    objective: Literal["auto", "find", "approach"] = "auto"
    simulation_seed: int | None = Field(default=None, ge=0, le=2**32 - 1)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入任务")
        return value.strip()


class Telemetry(Contract):
    observed_at: float
    position_ned: tuple[float, float, float]
    velocity_ned: tuple[float, float, float] = (0, 0, 0)
    home_down_m: float = 0
    home_north_m: float = 0
    home_east_m: float = 0
    heading_deg: float
    armed: bool
    airborne: bool
    battery_percent: float | None = None
    source: str


class Camera(Contract):
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)
    zoom_x: float | None = Field(default=None, ge=1, le=5)
    fx: float = Field(gt=0)
    fy: float = Field(gt=0)
    cx: float
    cy: float
    yaw_deg: float
    pitch_deg: float
    roll_deg: float = 0
    ground_down_m: float | None = None
    ground_uncertainty_m: float = Field(default=2, ge=0, le=20)
    source: str
    timing: Literal["simulated_capture", "camera_timestamp", "receipt_estimate"]
    position_ned: tuple[float, float, float] | None = None


class SearchCameraDefaults(Contract):
    pitch_deg: float = Field(ge=-85, le=-5)
    zoom_x: float = Field(ge=1, le=5)


class Observation(Contract):
    observation_id: Id
    vehicle_id: Id
    captured_at: float
    telemetry: Telemetry
    camera: Camera
    image: bytes = Field(repr=False, exclude=True)
    capabilities: list[str]
    source: Literal["geometric_simulation", "http_camera", "drone4d_native"]
    fleet: list[dict] = Field(default_factory=list, max_length=32)
    scan_headings_deg: list[int] = Field(default_factory=list, max_length=12)
    accepted_task_goal: Literal["find", "approach"] | None = None
    search_camera_defaults: SearchCameraDefaults | None = None
    visited_positions_ned: list[tuple[float, float, float]] = Field(
        default_factory=list, max_length=64
    )


class Detection(Contract):
    label: str = Field(min_length=1, max_length=150)
    bbox: tuple[float, float, float, float]
    ground_contact_visible: bool = False

    @field_validator("bbox")
    @classmethod
    def box(cls, v):
        x1, y1, x2, y2 = v
        if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
            raise ValueError("bbox must be ordered 0..1000 image coordinates")
        return v


class Observe(Contract):
    action: Literal["observe"]


class Takeoff(Contract):
    action: Literal["takeoff"]
    altitude_m: float = Field(gt=0, le=100)


class Yaw(Contract):
    action: Literal["yaw"]
    heading_deg: float = Field(ge=0, lt=360)


class Gimbal(Contract):
    action: Literal["gimbal"]
    pitch_deg: float = Field(ge=-85, le=-5)
    yaw_deg: float | None = Field(default=None, ge=0, lt=360)


class AimTarget(Contract):
    """Point at the current image candidate using calibrated camera geometry.

    Changes pointing, not detail/resolution. A candidate already near the image
    center does not need repeated alignment before inspection.
    """

    action: Literal["aim_target"]


class Zoom(Contract):
    """Choose absolute optical zoom within the camera's supported range.

    Magnifies the current view without moving the aircraft. The planner chooses
    the factor from current imagery; no scene-specific factor is supplied.
    """

    action: Literal["zoom"]
    zoom_x: float = Field(ge=1, le=5)


class Approach(Contract):
    action: Literal["approach"]
    step_m: float = Field(gt=0, le=30)


class InspectTarget(Contract):
    """Fly a short horizontal step along the current candidate's visual bearing.

    Active perception for a visible but uncertain candidate: use after camera
    inspection when getting closer will resolve detail. Does not require prior
    identity confirmation or visible feet. Subject to advertised range bounds.
    """

    action: Literal["inspect_target"]
    distance_m: float = Field(gt=0, le=15)


class SearchMove(Contract):
    action: Literal["search_move"]
    heading_deg: float = Field(ge=0, lt=360)
    distance_m: float = Field(gt=0, le=30)


class Altitude(Contract):
    action: Literal["altitude"]
    altitude_m: float = Field(gt=0, le=100)


class Hold(Contract):
    action: Literal["hold"]
    duration_s: float = Field(default=1, gt=0, le=5)


class Finish(Contract):
    action: Literal["finish"]
    outcome: Literal["found", "not_found", "needs_operator"]


Tool = Annotated[
    Union[
        Observe,
        Takeoff,
        Yaw,
        Gimbal,
        AimTarget,
        Zoom,
        Approach,
        InspectTarget,
        SearchMove,
        Altitude,
        Hold,
        Finish,
    ],
    Field(discriminator="action"),
]


class Decision(Contract):
    observation_id: Id
    task_goal: Literal["find", "approach"] = Field(
        description="Final user objective, not current phase: find means locate only; approach means locate then physically approach/observe within a requested distance, including while still searching."
    )
    task_interpretation: str = Field(min_length=1, max_length=500)
    scene_summary: str = Field(min_length=1, max_length=700)
    plan: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=8
    )
    target: Detection | None = Field(
        default=None,
        description="Current localized candidate, including tiny tentative matches; null only when no candidate is visible. Identity confirmation is not required.",
    )
    next_action: Tool
