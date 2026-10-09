import asyncio
import pytest
from uav_harness.contracts import Plan
from uav_harness.errors import HarnessError, Uncertain
from uav_harness.journal import Journal
from uav_harness.runtime import Harness
from conftest import mock_settings


def arm_plan(vid="model-1"):
    return Plan(actions=[{"action_id":"arm","vehicle_id":vid,"action":"vehicle.arm","params":{}}])


async def test_idempotent_replay_and_conflicting_payload(harness):
    plan=arm_plan()
    row=await harness.submit(plan,"same")
    replay=await harness.submit(plan,"same")
    assert replay["job_id"]==row["job_id"] and replay["replayed"]
    with pytest.raises(HarnessError,match="different payload"):
        await harness.submit(arm_plan("ap-1"),"same")
    await harness.jobs[row["job_id"]]
    peer=harness.simulators["model-1"]
    assert sum(c["command"]==400 and c["param1"]==1 for c in peer.commands)==1
    await harness.sessions["model-1"].normal_disarm()


async def test_active_vehicle_has_one_job_owner(harness):
    row=await harness.submit(arm_plan(),"first")
    with pytest.raises(HarnessError,match="owned"):
        await harness.submit(arm_plan(),"second")
    await harness.jobs[row["job_id"]]
    await harness.sessions["model-1"].normal_disarm()


async def test_failed_dependency_never_dispatches_takeoff(harness):
    peer=harness.simulators["ap-1"];peer.reject[400]=4
    plan=Plan(actions=[
      {"action_id":"arm","vehicle_id":"ap-1","action":"vehicle.arm","params":{}},
      {"action_id":"takeoff","vehicle_id":"ap-1","action":"flight.takeoff","params":{"altitude_home_m":1},"depends_on":["arm"]}])
    row=await harness.submit(plan,"reject")
    await harness.jobs[row["job_id"]]
    result=harness.journal.get(row["job_id"])
    assert result["status"]=="failed"
    assert result["results"]["takeoff"]["status"]=="not_executed"
    assert not any(c["command"]==22 for c in peer.commands)


async def test_ack_arm_without_state_effect_is_unknown_and_blocks_replay(harness):
    peer=harness.simulators["model-1"];peer.ignore_effect.add(400)
    row=await harness.submit(arm_plan(),"uncertain")
    await harness.jobs[row["job_id"]]
    assert harness.journal.get(row["job_id"])["status"]=="unknown"
    with pytest.raises(Uncertain,match="prior outcome"):
        await harness.submit(arm_plan(),"new")
    reconciled=await harness.reconcile(row["job_id"])
    assert reconciled["status"]=="reconciled"


async def test_observation_config_cannot_execute(tmp_path):
    h=Harness(mock_settings(tmp_path,control=False));await h.open()
    try:
        with pytest.raises(HarnessError,match="observation"):
            await h.submit(arm_plan(),"read-only")
        assert all(not any(c["command"]==400 for c in p.commands) for p in h.simulators.values())
    finally:
        await h.close()


def test_restart_marks_unfinished_jobs_unknown_without_replaying(tmp_path):
    path=tmp_path/"journal.sqlite"
    j=Journal(path);job,_=j.create("key",arm_plan().model_dump());j.update(job,"running");j.close()
    recovered=Journal(path)
    assert recovered.get(job)["status"]=="unknown"
    assert recovered.unknown_vehicles()=={"model-1"}
    assert recovered.create("key",arm_plan().model_dump())==(job,False)
    recovered.close()


async def test_cancel_bench_probe_observes_cleanup_before_releasing_lease(harness):
    p=Plan(actions=[{"action_id":"probe","vehicle_id":"model-1","action":"bench.takeoff_probe","params":{},"timeout_s":60}])
    row=await harness.submit(p,"cancel-probe")
    await asyncio.sleep(.04)
    await harness.cancel(row["job_id"])
    await harness.jobs[row["job_id"]]
    peer=harness.simulators["model-1"]
    assert not peer.armed
    assert "model-1" not in harness.leases
    assert harness.journal.get(row["job_id"])["status"] in ("cancelled","unknown")
