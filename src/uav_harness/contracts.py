from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Id = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")]
Seconds = Annotated[float, Field(gt=0, le=300)]


class Empty(Contract):
    pass


class TakeoffParams(Contract):
    altitude_home_m: float = Field(gt=0, le=100)


class MoveParams(Contract):
    frame: Literal["local_enu", "body_flu"] = "local_enu"
    x_m: float = Field(ge=-100, le=100)
    y_m: float = Field(ge=-100, le=100)
    z_m: float = Field(ge=-100, le=100)


class HoldParams(Contract):
    duration_s: Seconds


class MotorParams(Contract):
    motor_sequence: int = Field(default=1, ge=1, le=8, strict=True)
    pwm_us: int = Field(default=1231, ge=1000, le=1300, strict=True)
    duration_s: float = Field(default=5, gt=0, le=5)


class ProbeParams(Contract):
    altitude_home_m: float = Field(default=1.5, gt=0, le=2)


class ActionBase(Contract):
    action_id: Id
    vehicle_id: Id
    depends_on: list[Id] = Field(default_factory=list)
    timeout_s: Seconds = 60


class Arm(ActionBase):
    action: Literal["vehicle.arm"]
    params: Empty = Field(default_factory=Empty)


class Disarm(ActionBase):
    action: Literal["vehicle.disarm"]
    params: Empty = Field(default_factory=Empty)


class Takeoff(ActionBase):
    action: Literal["flight.takeoff"]
    params: TakeoffParams


class Move(ActionBase):
    action: Literal["flight.move_relative"]
    params: MoveParams


class Hold(ActionBase):
    action: Literal["flight.hold"]
    params: HoldParams

    @model_validator(mode="after")
    def duration_fits(self):
        if self.params.duration_s >= self.timeout_s:
            raise ValueError("hold duration must be shorter than action timeout")
        return self


class Land(ActionBase):
    action: Literal["flight.land"]
    params: Empty = Field(default_factory=Empty)


class MotorTest(ActionBase):
    action: Literal["bench.motor_test"]
    params: MotorParams = Field(default_factory=MotorParams)


class TakeoffProbe(ActionBase):
    action: Literal["bench.takeoff_probe"]
    params: ProbeParams = Field(default_factory=ProbeParams)


Action = Annotated[Union[Arm, Disarm, Takeoff, Move, Hold, Land, MotorTest, TakeoffProbe],
                   Field(discriminator="action")]


class Plan(Contract):
    query: str = Field(default="", max_length=10000)
    actions: list[Action] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def graph(self):
        nodes = {a.action_id: a for a in self.actions}
        if len(nodes) != len(self.actions):
            raise ValueError("duplicate action IDs")
        ancestors = {}
        pending = set(nodes)
        for a in self.actions:
            if len(set(a.depends_on)) != len(a.depends_on) or not set(a.depends_on) <= nodes.keys():
                raise ValueError("invalid dependencies")
        while pending:
            ready = [k for k in pending if set(nodes[k].depends_on) <= ancestors.keys()]
            if not ready:
                raise ValueError("dependency cycle")
            for k in ready:
                ancestors[k] = set(nodes[k].depends_on)
                for dep in nodes[k].depends_on:
                    ancestors[k].update(ancestors[dep])
                pending.remove(k)
        for i, left in enumerate(self.actions):
            for right in self.actions[i + 1:]:
                if left.vehicle_id == right.vehicle_id and left.action_id not in ancestors[right.action_id] and right.action_id not in ancestors[left.action_id]:
                    raise ValueError("actions on one vehicle must be dependency-ordered")
        return self

