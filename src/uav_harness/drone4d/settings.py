import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from ..contracts import Contract


class Drone4DConfig(Contract):
    enabled: bool = False
    base_url: str = Field(
        default_factory=lambda: os.getenv(
            "DRONE4D_URL", "https://drone4d.agentnetwork.org.cn"
        )
    )
    source_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("DRONE4D_SOURCE", str(Path.cwd().parent / "ANet-Drone4D"))
        )
    )
    web_dir: Path = Path("state/drone4d-web")
    world_id: str = "synthcity"
    capture_timeout_s: float = Field(default=30, gt=0, le=90)
    admission_timeout_s: float = Field(default=1200, gt=0, le=3600)
    action_timeout_s: float = Field(default=120, gt=0, le=300)
    initial_camera_pitch_deg: float = Field(default=-35, ge=-85, le=-5)
    initial_zoom_x: float = Field(default=1.5, ge=1, le=2)
    speed_mps: float = Field(default=3, gt=0, le=8)

    @model_validator(mode="after")
    def endpoint(self):
        u = urlsplit(self.base_url)
        if (
            u.scheme not in ("https", "http")
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError(
                "Drone4D requires an operator-configured HTTP(S) base URL without credentials"
            )
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote Drone4D endpoints require HTTPS")
        self.base_url = self.base_url.rstrip("/")
        return self
