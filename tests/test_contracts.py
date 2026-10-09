import json
import pytest
from pydantic import ValidationError
from uav_harness.config import Settings
from uav_harness.contracts import Plan
from conftest import ROOT, mock_settings


def arm(aid="arm", vid="ap-1", deps=None):
    return {"action_id": aid, "vehicle_id": vid, "action": "vehicle.arm", "depends_on": deps or [], "params": {}}


def test_heterogeneous_example_is_a_valid_ordered_graph():
    plan = Plan.model_validate_json((ROOT/"examples/heterogeneous-plan.json").read_text())
    assert len(plan.actions) == 6


@pytest.mark.parametrize("actions", [[arm(), arm()], [arm("a",deps=["b"]),arm("b",deps=["a"])],
                                    [arm(deps=["missing"])], [arm("a"),arm("b")]])
def test_invalid_graphs_rejected(actions):
    with pytest.raises(ValidationError):
        Plan(actions=actions)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 101])
def test_invalid_takeoff_values_rejected(value):
    with pytest.raises(ValidationError):
        Plan(actions=[{"action_id":"t", "vehicle_id":"ap-1", "action":"flight.takeoff", "params":{"altitude_home_m":value}}])


def test_unknown_raw_command_is_not_an_action():
    with pytest.raises(ValidationError):
        Plan(actions=[{"action_id":"raw", "vehicle_id":"ap-1", "action":"mavlink.command", "params":{"command":400}}])


def test_same_serial_device_cannot_have_two_owners():
    with pytest.raises(ValidationError):
        Settings(links=[{"name":"a","kind":"serial","device":"/dev/example"},
                        {"name":"b","kind":"serial","device":"/dev/example"}],
                 vehicles=[{"vehicle_id":"a","link":"a","backend":"ardupilot","system_id":1}])


def test_model_profile_cannot_silently_apply_to_px4(tmp_path):
    raw=mock_settings(tmp_path).model_dump()
    raw["vehicles"][1]["profile"]="model_bench"
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)
