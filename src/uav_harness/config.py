from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .contracts import Contract, Id


class LinkConfig(Contract):
    name: Id
    kind: Literal["serial", "udp", "simulator"]
    device: str | None = None
    baud: int = Field(default=57600, ge=9600, le=3000000)
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=0, ge=0, le=65535)
    peer_host: str = "127.0.0.1"
    peer_port: int | None = Field(default=None, ge=1, le=65535)
    observers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def serial_device(self):
        if self.kind == "serial" and not self.device:
            raise ValueError("serial link requires device")
        if self.kind != "serial" and self.device:
            raise ValueError("device is only used by serial links")
        return self


class VehicleConfig(Contract):
    vehicle_id: Id
    link: Id
    backend: Literal["ardupilot", "px4"]
    profile: Literal["flight", "model_bench"] = "flight"
    system_id: int = Field(ge=1, le=254)
    component_id: int = Field(default=1, ge=1, le=255)
    firmware: tuple[int, int] | None = None
    expected_board_id: int | None = None
    max_height_m: float = Field(default=20, gt=0, le=100)
    max_relative_distance_m: float = Field(default=30, gt=0, le=100)
    position_tolerance_m: float = Field(default=.3, gt=0, le=2)
    min_battery_percent: int = Field(default=20, ge=0, le=100)

    @model_validator(mode="after")
    def model_platform(self):
        if self.profile == "model_bench" and self.backend != "ardupilot":
            raise ValueError("model_bench is the attested ArduCopter bench profile")
        return self


class Settings(Contract):
    links: list[LinkConfig] = Field(min_length=1)
    vehicles: list[VehicleConfig] = Field(min_length=1)
    allow_control: bool = False
    source_system: int = Field(default=245, ge=1, le=254)
    source_component: int = Field(default=191, ge=1, le=255)
    telemetry_timeout_s: float = Field(default=2, gt=0, le=5)
    ack_timeout_s: float = Field(default=3, gt=0, le=10)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1024, le=65535)
    state_dir: Path = Path("state")

    @model_validator(mode="after")
    def identities(self):
        names = {x.name for x in self.links}
        ids = {v.vehicle_id for v in self.vehicles}
        targets = {(v.link, v.system_id, v.component_id) for v in self.vehicles}
        if len(names) != len(self.links) or len(ids) != len(self.vehicles) or len(targets) != len(self.vehicles):
            raise ValueError("duplicate link, vehicle or target identity")
        if any(v.link not in names or v.system_id == self.source_system for v in self.vehicles):
            raise ValueError("unknown link or GCS/vehicle system ID collision")
        endpoints = [(x.bind_host, x.bind_port) for x in self.links if x.kind != "serial" and x.bind_port]
        devices = [x.device for x in self.links if x.kind == "serial"]
        if len(set(endpoints)) != len(endpoints) or len(set(devices)) != len(devices):
            raise ValueError("a socket or serial device must have a single owning link")
        for x in self.links:
            if x.kind == "simulator" and sum(v.link == x.name for v in self.vehicles) != 1:
                raise ValueError("one protocol simulator per link")
        return self

    @classmethod
    def load(cls, path):
        return cls.model_validate(json.loads(Path(path).read_text()))

