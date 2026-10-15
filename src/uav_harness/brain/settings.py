from pydantic import Field, model_validator
from urllib.parse import urlsplit
from ..contracts import Contract, Id
from .contracts import Policy


class CameraConfig(Contract):
    vehicle_id: Id
    snapshot_url: str
    width: int | None = Field(default=None, gt=0, le=4096)
    height: int | None = Field(default=None, gt=0, le=4096)
    fx: float | None = Field(default=None, gt=0)
    fy: float | None = Field(default=None, gt=0)
    cx: float | None = None
    cy: float | None = None
    mount_pitch_deg: float | None = Field(default=None, ge=-90, le=90)
    mount_yaw_deg: float = 0
    mount_roll_deg: float = 0
    offset_body_frd: tuple[float, float, float] = (0, 0, 0)
    ground_down_m: float | None = None
    ground_uncertainty_m: float = Field(default=2, ge=0, le=20)
    timestamp_header: str = "X-Capture-Timestamp"
    trust_receipt_time: bool = False
    gimbal_device_id: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def calibration(self):
        u = urlsplit(self.snapshot_url)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
            or u.fragment
        ):
            raise ValueError(
                "camera must be an operator-configured HTTP(S) snapshot URL without embedded credentials"
            )
        values = (self.width, self.height, self.fx, self.fy)
        if any(x is not None for x in values) and not all(
            x is not None for x in values
        ):
            raise ValueError(
                "camera intrinsics require width, height, fx and fy together"
            )
        return self


class BrainConfig(Contract):
    allow_hardware_control: bool = False
    horizontal_space_cleared: bool = False
    policy: Policy = Field(default_factory=Policy)
    cameras: list[CameraConfig] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_cameras(self):
        if len({c.vehicle_id for c in self.cameras}) != len(self.cameras):
            raise ValueError("one configured camera per vehicle")
        return self
