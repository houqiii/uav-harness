import asyncio
import math
import pytest
from uav_harness.contracts import Plan
from uav_harness.errors import HarnessError, Uncertain
from conftest import ROOT


async def wait_job(h, row):
    await h.jobs[row["job_id"]]
    return h.journal.get(row["job_id"])


async def test_mixed_ardupilot_px4_wire_plan_and_completion(harness):
    plan=Plan.model_validate_json((ROOT/"examples/heterogeneous-plan.json").read_text())
    result=await wait_job(harness,await harness.submit(plan,"mixed"))
    assert result["status"]=="succeeded",result
    assert all(v["status"]=="succeeded" for v in result["results"].values())
    ap,px=harness.simulators["ap-1"],harness.simulators["px4-1"]
    ap_takeoff=next(c for c in ap.commands if c["command"]==22)
    px_takeoff=next(c for c in px.commands if c["command"]==22)
    assert ap_takeoff["param7"]==2
    assert px_takeoff["param7"]==102  # Home AMSL 100m + relative 2m.
    assert math.isnan(px_takeoff["param5"])
    assert any(c["command"]==176 and c["param2"]==4 for c in ap.commands)
    assert any(c["command"]==176 and c["param2"]==6 for c in px.commands)
    assert all(c["target_system"]==1 for c in ap.commands)
    assert all(c["target_system"]==2 for c in px.commands)
    assert any(p["x"]==0 and p["y"]==1 and p["z"]==-2 for p in ap.setpoints)
    assert any(p["x"]==0 and p["y"]==1 and p["z"]==-2 for p in px.setpoints)
    assert all(p["coordinate_frame"]==1 and p["type_mask"]==2552 for p in ap.setpoints+px.setpoints)
    assert not ap.armed and not px.armed


async def test_ack_without_mode_effect_does_not_arm(harness):
    peer=harness.simulators["ap-1"]
    peer.ignore_effect.add(176)
    with pytest.raises(Uncertain):
        await harness.sessions["ap-1"].normal_arm()
    assert not any(c["command"]==400 for c in peer.commands)


async def test_foreign_addressed_ack_not_accepted_or_retried(harness):
    s=harness.sessions["ap-1"];peer=harness.simulators["ap-1"]
    peer.ack_target=(255,190)
    before=len(peer.commands)
    with pytest.raises(Uncertain):
        await s.command(512,[148],control=False)
    assert len(peer.commands)==before+1
    with pytest.raises(Uncertain):
        await s.command(512,[148],control=False)
    assert len(peer.commands)==before+1


async def test_model_profile_excludes_position_flight_before_dispatch(harness):
    p=Plan(actions=[{"action_id":"move", "vehicle_id":"model-1", "action":"flight.move_relative",
                     "params":{"x_m":1,"y_m":0,"z_m":0}}])
    before=len(harness.simulators["model-1"].commands)
    with pytest.raises(HarnessError,match="unavailable"):
        await harness.submit(p,"unavailable")
    assert len(harness.simulators["model-1"].commands)==before


async def test_model_takeoff_probe_records_output_and_ground_cleanup(harness):
    p=Plan.model_validate_json((ROOT/"examples/model-probe.json").read_text())
    result=await wait_job(harness,await harness.submit(p,"probe"))
    assert result["status"]=="succeeded",result
    evidence=result["results"]["probe"]
    assert evidence["altitude_reached"] is False
    assert evidence["physical_rotation_verified"] is False
    peer=harness.simulators["model-1"]
    takeoff=next(c for c in peer.commands if c["command"]==22)
    assert takeoff["param3"]==1 and takeoff["param7"]==1.5
    assert all(c["param2"]==0 for c in peer.commands if c["command"]==400)
    assert peer.main==0 and not peer.armed


async def test_motor_probe_has_real_wire_timeout_and_stop(harness):
    p=Plan(actions=[{"action_id":"motor", "vehicle_id":"model-1", "action":"bench.motor_test",
                     "params":{"pwm_us":1231,"duration_s":.15},"timeout_s":10}])
    result=await wait_job(harness,await harness.submit(p,"motor"))
    assert result["status"]=="succeeded",result
    assert result["results"]["motor"]["physical_rotation_verified"] is False
    command=next(c for c in harness.simulators["model-1"].commands if c["command"]==209)
    assert command["param1"]==1 and command["param2"]==1 and command["param3"]==1231
    assert not harness.simulators["model-1"].armed


async def test_stale_position_blocks_movement_without_sending(harness):
    s=harness.sessions["ap-1"];peer=harness.simulators["ap-1"]
    peer.stop_position=True
    await asyncio.sleep(.6)
    before=len(peer.commands)
    with pytest.raises(Uncertain,match="LOCAL_POSITION"):
        s.position()
    assert len(peer.commands)==before


async def test_body_frame_freezes_target_using_observed_yaw(harness):
    peer=harness.simulators["ap-1"]
    peer.yaw=math.pi/2
    plan=Plan(actions=[
      {"action_id":"t","vehicle_id":"ap-1","action":"flight.takeoff","params":{"altitude_home_m":1}},
      {"action_id":"m","vehicle_id":"ap-1","action":"flight.move_relative","params":{"frame":"body_flu","x_m":1,"y_m":0,"z_m":0},"depends_on":["t"]},
      {"action_id":"l","vehicle_id":"ap-1","action":"flight.land","params":{},"depends_on":["m"]}])
    result=await wait_job(harness,await harness.submit(plan,"body"))
    assert result["status"]=="succeeded",result
    active=[p for p in peer.setpoints if p["y"]>.5]
    assert active and all(abs(p["x"])<1e-5 and p["y"]==pytest.approx(1) for p in active)


async def test_manual_mode_change_is_not_overridden_by_pause(harness):
    s=harness.sessions["ap-1"];peer=harness.simulators["ap-1"]
    peer.armed=True;peer.position[2]=-1;peer.target[2]=-1
    s.allowed_modes={(4,0)}
    peer.main=0
    await asyncio.sleep(.1)
    before=len(peer.commands)
    with pytest.raises(Uncertain,match="manual"):
        await harness.adapters["ap-1"].pause()
    assert len(peer.commands)==before
