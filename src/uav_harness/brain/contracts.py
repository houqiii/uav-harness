from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import Field, field_validator, model_validator
from ..contracts import Contract, Id


class Policy(Contract):
    search_altitude_m: float = Field(default=50, gt=0, le=100)
    min_altitude_m: float = Field(default=10, gt=0, le=100)
    max_altitude_m: float = Field(default=80, gt=0, le=100)
    scan_step_deg: float = Field(default=30, ge=10, le=90)
    max_step_m: float = Field(default=10, gt=0, le=30)
    stand_off_m: float = Field(default=20, ge=10, le=100)
    max_radius_m: float = Field(default=250, gt=0, le=500)
    max_rounds: int = Field(default=40, ge=1, le=100)
    max_duration_s: float = Field(default=900, gt=0, le=1800)
    max_observation_age_s: float = Field(default=30, gt=0, le=60)

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


class Zoom(Contract):
    action: Literal["zoom"]
    zoom_x: float = Field(ge=1, le=5)


class Approach(Contract):
    action: Literal["approach"]
    step_m: float = Field(gt=0, le=30)


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
    Union[Observe, Takeoff, Yaw, Gimbal, Zoom, Approach, Altitude, Hold, Finish],
    Field(discriminator="action"),
]


class Decision(Contract):
    observation_id: Id
    task_goal: Literal["find", "approach"]
    task_interpretation: str = Field(min_length=1, max_length=500)
    scene_summary: str = Field(min_length=1, max_length=700)
    plan: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=8
    )
    target: Detection | None = None
    next_action: Tool
