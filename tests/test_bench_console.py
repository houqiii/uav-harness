import asyncio
import json
import os
import signal
import sys

import pytest

from uav_harness.bench_console import BenchConsole, choose_port, telemetry_line
from uav_harness.errors import HarnessError


def test_ambiguous_serial_devices_require_explicit_selection(monkeypatch):
    from types import SimpleNamespace
    from uav_harness import bench_console
    ports = [SimpleNamespace(device=f"/dev/cu.usbserial-{i}", description="USB", vid=123) for i in (1, 2)]
    monkeypatch.setattr(bench_console.list_ports, "comports", lambda: ports)
    with pytest.raises(HarnessError, match="多个串口"):
        choose_port()
    assert choose_port("/dev/cu.usbserial-2") == "/dev/cu.usbserial-2"
    monkeypatch.setattr(bench_console.list_ports, "comports", lambda: ports[:1])
    assert choose_port() == "/dev/cu.usbserial-1"
    monkeypatch.setattr(bench_console.list_ports, "comports", lambda: [])
    with pytest.raises(HarnessError, match="未找到"):
        choose_port()


async def test_console_invalid_and_readonly_commands_never_dispatch(harness):
    output = []
    console = BenchConsole(harness, output.append)
    peer = harness.simulators["model-1"]
    before = len(peer.commands)
    for line in ("motor 9", "motor -1", "motor 1 2", "motor x", "probe extra", "forward 5"):
        await console.execute(line)
    assert len(peer.commands) == before
    assert sum("[ERROR]" in line for line in output) == 6
    harness.settings.allow_control = False
    await console.execute("motor")
    assert len(peer.commands) == before
    assert "observation only" in output[-1]


async def test_console_identifies_stale_telemetry(harness):
    console = BenchConsole(harness, lambda line: None)
    line = telemetry_line(console.session)
    assert "armed=False" in line and "PWM=" in line and "missing/stale" not in line
    harness.simulators["model-1"].stop_telemetry = True
    await asyncio.sleep(.6)
    line = telemetry_line(console.session)
    assert "HEARTBEAT=missing/stale" in line and "SERVO_OUTPUT_RAW=missing/stale" in line
    assert "armed=False" not in line and "PWM=" not in line


@pytest.mark.skipif(os.name != "posix", reason="Mac/POSIX terminal entrypoint")
async def test_piped_console_runs_wire_actions_and_receives_telemetry():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uav_harness.bench_console", "--mock",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(b"status\nmotor 2\nprobe\nquit\n"), 25)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    text = stdout.decode()
    assert process.returncode == 0, stderr.decode()
    assert "[MOCK]" in text and "[RX " in text and "PWM=" in text
    assert "[TX] MAV_CMD_DO_MOTOR_TEST (209) params=[2, 1, 1231, 5.0, 1, 0, 0]" in text
    assert "[ACK] command=209 MAV_RESULT_ACCEPTED" in text
    assert "[TX] MAV_CMD_NAV_TAKEOFF (22) params=[0, 0, 1, 0, 0, 0, 1.5]" in text
    results = [json.loads(line.removeprefix("[RESULT] ")) for line in text.splitlines() if line.startswith("[RESULT]")]
    assert len(results) == 2 and all(r["status"] == "succeeded" for r in results)
    final = results[-1]["results"]["probe"]["final_state"]
    assert final["armed"] is False
    assert final["landed"] == 1
    assert not results[-1]["results"]["probe"]["altitude_reached"]


@pytest.mark.skipif(os.name != "posix", reason="Mac/POSIX signal cleanup")
async def test_ctrl_c_during_motor_waits_for_stop_and_persists_outcome(tmp_path):
    # Run with a protocol simulator but a persistent test directory, to inspect cleanup evidence.
    code = """import argparse, asyncio
from uav_harness.bench_console import run_console, settings_for
args = argparse.Namespace(mock=True, observe=False, system_id=1, component_id=1, board_id=1010)
try:
    asyncio.run(run_console(settings_for(args, __import__('sys').argv[1])))
except KeyboardInterrupt:
    pass
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        process.stdin.write(b"motor\n")
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 12)
            assert line, "console exited before starting motor"
            if b"[ACK] command=209" in line:
                break
        process.send_signal(signal.SIGINT)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    assert "等待已有任务收尾" in stdout.decode()
    import sqlite3
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as db:
        status, results = db.execute("SELECT status, results FROM jobs").fetchone()
        events = [json.loads(row[0]) for row in db.execute("SELECT data FROM events")]
    assert status in ("cancelled", "unknown")
    assert json.loads(results)["motor"]["status"] == "unknown"
    sent = [e for e in events if e["kind"] == "command_sent"]
    assert sum(e["command"] == 209 for e in sent) == 1
    assert all(e["params"][1] == 0 for e in sent if e["command"] == 400)
    # Verify the final received wire state, rather than only the console's exit code.
    from pymavlink import mavutil
    log = mavutil.mavlink_connection(str(next(tmp_path.glob("*.tlog"))))
    latest = {}
    try:
        while (msg := log.recv_match()) is not None:
            latest[msg.get_type()] = msg
    finally:
        log.close()
    assert not latest["HEARTBEAT"].base_mode & 128
    assert latest["EXTENDED_SYS_STATE"].landed_state == 1
    assert latest["SERVO_OUTPUT_RAW"].servo1_raw == 1051
