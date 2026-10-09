"""Interactive model-bench commands and telemetry over one owned MAVLink link."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid

from serial.tools import list_ports

from .config import Settings
from .contracts import Plan
from .errors import HarnessError
from .runtime import Harness
from .transport import mav


HELP = """输入命令后按回车：
  motor [序号]   单电机 1231 µs、5 秒；默认序号 1
  probe          普通解锁 → 垂直起飞指令探针 → Land/解除武装/恢复模式
  land           Land 并等待地面/未解锁
  status         完整遥测快照
  watch / quiet  开启/暂停每秒遥测显示
  help / quit    帮助/退出（等待已有任务收尾）
"""


def choose_port(explicit=None):
    if explicit:
        return explicit
    ports = [p for p in list_ports.comports()
             if p.vid is not None or p.device.startswith(
                 ("/dev/cu.usb", "/dev/cu.SLAB", "/dev/cu.wch", "/dev/ttyUSB", "/dev/ttyACM"))]
    if len(ports) == 1:
        return ports[0].device
    choices = "\n".join(f"  {p.device} ({p.description})" for p in ports)
    if ports:
        raise HarnessError(f"找到多个串口，请用 --port 指定：\n{choices}")
    raise HarnessError("未找到 USB 串口。插入飞控接口后重试，或用 --port 指定设备路径。")


def settings_for(args, state_dir):
    link = {"name": "bench", "kind": "simulator" if args.mock else "serial"}
    if not args.mock:
        link.update(device=choose_port(args.port), baud=args.baud)
    return Settings.model_validate({
        "allow_control": not args.observe, "state_dir": str(state_dir), "links": [link],
        "vehicles": [{"vehicle_id": "model-1", "link": "bench", "backend": "ardupilot",
                      "profile": "model_bench", "system_id": args.system_id,
                      "component_id": args.component_id, "firmware": [4, 5],
                      "expected_board_id": args.board_id, "max_height_m": 2}],
    })


def action_plan(line):
    words = shlex.split(line)
    if not words:
        return None
    command = words[0].lower()
    if command == "motor" and len(words) <= 2:
        action, params, timeout = "bench.motor_test", {
            "motor_sequence": int(words[1]) if len(words) == 2 else 1,
            "pwm_us": 1231, "duration_s": 5}, 20
    elif command == "probe" and len(words) == 1:
        action, params, timeout = "bench.takeoff_probe", {"altitude_home_m": 1.5}, 60
    elif command == "land" and len(words) == 1:
        action, params, timeout = "flight.land", {}, 45
    else:
        raise ValueError("未知命令或参数；输入 help 查看用法。")
    return Plan(actions=[{"action_id": command, "vehicle_id": "model-1", "action": action,
                          "params": params, "timeout_s": timeout}])


def enum_name(group, value):
    entry = mav.enums[group].get(value)
    return entry.name if entry else str(value)


def protocol_line(event):
    if event["kind"] == "command_sent":
        cmd = event["command"]
        return f"[TX] {enum_name('MAV_CMD', cmd)} ({cmd}) params={event['params']}"
    if event["kind"] == "command_ack":
        message = event["message"]
        return f"[ACK] command={message['command']} {enum_name('MAV_RESULT', message['result'])}"
    if event["kind"] == "link_fault":
        return f"[LINK] {event['error']}"
    return None


def telemetry_line(session):
    fields = []
    for kind in ("HEARTBEAT", "EXTENDED_SYS_STATE", "ATTITUDE", "SYS_STATUS", "SERVO_OUTPUT_RAW"):
        try:
            msg = session.get(kind)
        except HarnessError:
            fields.append(f"{kind}=missing/stale")
            continue
        if kind == "HEARTBEAT":
            fields.append(f"mode={enum_name('COPTER_MODE', msg.custom_mode)}({msg.custom_mode}) armed={bool(msg.base_mode & 128)}")
        elif kind == "EXTENDED_SYS_STATE":
            fields.append(f"landed={enum_name('MAV_LANDED_STATE', msg.landed_state)}")
        elif kind == "ATTITUDE":
            fields.append("rpy_deg=" + "/".join(f"{math.degrees(v):.1f}" for v in (msg.roll, msg.pitch, msg.yaw)))
        elif kind == "SYS_STATUS":
            voltage = "unknown" if msg.voltage_battery in (0, 65535) else f"{msg.voltage_battery / 1000:.2f}V"
            remaining = f"{msg.battery_remaining}%" if 0 <= msg.battery_remaining <= 100 else "unknown"
            fields.append(f"battery={voltage}/{remaining}")
        else:
            fields.append("PWM=" + "/".join(str(getattr(msg, f"servo{i}_raw")) for i in range(1, 9)))
    try:
        session.require()
    except HarnessError as exc:
        fields.insert(0, f"unavailable={exc}")
    return "[RX " + time.strftime("%H:%M:%S") + "] " + " | ".join(fields)


def result_summary(row):
    results = {}
    for name, value in row["results"].items():
        results[name] = {k: v for k, v in value.items() if k != "final_state"}
        if (state := value.get("final_state")):
            heartbeat, landed = state.get("heartbeat"), state.get("landed")
            results[name]["final_state"] = {
                "connected": state["connected"],
                "armed": bool(heartbeat["base_mode"] & 128) if heartbeat else None,
                "mode": heartbeat["custom_mode"] if heartbeat else None,
                "landed": landed["landed_state"] if landed else None,
            }
    return {"job_id": row["job_id"], "status": row["status"], "results": results}


async def stdin_lines():
    """Read terminal or piped lines without leaving a blocking input thread on exit."""
    loop, queue, buffer = asyncio.get_running_loop(), asyncio.Queue(), bytearray()
    fd = sys.stdin.fileno()

    def readable():
        chunk = os.read(fd, 4096)
        if not chunk:
            if buffer:
                queue.put_nowait(bytes(buffer).decode("utf-8", errors="replace"))
                buffer.clear()
            queue.put_nowait(None)
            loop.remove_reader(fd)
            return
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            queue.put_nowait(line.decode("utf-8", errors="replace"))

    loop.add_reader(fd, readable)
    try:
        while (line := await queue.get()) is not None:
            yield line.strip()
    finally:
        loop.remove_reader(fd)


class BenchConsole:
    def __init__(self, harness, output=print):
        self.harness, self.output = harness, output
        self.session = harness.sessions["model-1"]
        self.watching = True
        for link in harness.transports.values():
            original = link.event

            def event(value, original=original):
                original(value)
                if (line := protocol_line(value)):
                    self.output(line)

            link.event = event
            link.listeners.append(self.message)

    def message(self, msg, stamp):
        if (msg.get_srcSystem(), msg.get_srcComponent()) != (self.session.config.system_id, self.session.config.component_id):
            return
        if msg.get_type() == "STATUSTEXT":
            self.output(f"[FC] severity={msg.severity} {msg.text}")

    async def telemetry(self):
        while True:
            if self.watching:
                self.output(telemetry_line(self.session))
            await asyncio.sleep(1)

    async def execute(self, line):
        """Return False on quit. Actions stay serialized and use persisted Harness admission."""
        if line in ("quit", "exit", "q"):
            return False
        if line == "status":
            self.output(json.dumps(self.session.snapshot(), ensure_ascii=False, indent=2))
        elif line in ("watch", "quiet"):
            self.watching = line == "watch"
            self.output("遥测显示已开启" if self.watching else "遥测显示已暂停；仍在接收和记录")
        elif line in ("help", "?"):
            self.output(HELP)
        elif line:
            try:
                plan = action_plan(line)
                row = await self.harness.submit(plan, "console-" + uuid.uuid4().hex)
                self.output(f"[JOB] {row['job_id']} running")
                # The runtime owns cancellation/cleanup even when the terminal is interrupted.
                await asyncio.shield(self.harness.jobs[row["job_id"]])
                result = self.harness.journal.get(row["job_id"])
                self.output("[RESULT] " + json.dumps(result_summary(result), ensure_ascii=False))
                if result["status"] == "unknown":
                    self.output("结果不确定，未自动重试；保留日志并核实设备状态。")
            except (HarnessError, ValueError) as exc:
                self.output(f"[ERROR] {exc}")
        return True


async def run_console(settings):
    harness = Harness(settings)
    console = BenchConsole(harness, output=lambda line: print(line, flush=True))
    task = None
    try:
        print("连接：" + str(settings.links[0].device or "协议模拟对端"), flush=True)
        await harness.open()
        print("已连接：ArduCopter 4.5 / model-1；" + ("只读模式" if not settings.allow_control else "输入动作命令才控制电机"), flush=True)
        print(f"遥测与任务记录：{settings.state_dir.resolve()}", flush=True)
        print(HELP, flush=True)
        task = asyncio.create_task(console.telemetry())
        async for line in stdin_lines():
            if not await console.execute(line):
                break
    finally:
        print("正在关闭连接，等待已有任务收尾…", flush=True)
        # Keep receiving and displaying telemetry until controller-timed cleanup ends.
        try:
            await harness.close()
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="模型机电机测试与实时 MAVLink 遥测")
    parser.add_argument("--port", help="串口路径；省略时自动选择唯一 USB 串口")
    parser.add_argument("--baud", type=int, default=57600)
    parser.add_argument("--system-id", type=int, default=1)
    parser.add_argument("--component-id", type=int, default=1)
    parser.add_argument("--board-id", type=int, default=1010)
    parser.add_argument("--observe", action="store_true", help="只看遥测，不执行电机动作")
    parser.add_argument("--mock", action="store_true", help="协议模拟，无实机连接")
    parser.add_argument("--state-dir", type=Path, default=Path("state/bench-console"))
    args = parser.parse_args()
    try:
        if args.mock:
            print("[MOCK] 软件协议模拟；所有响应均不代表实机效果。", flush=True)
            with tempfile.TemporaryDirectory(prefix="uav-bench-mock-") as folder:
                asyncio.run(run_console(settings_for(args, folder)))
        else:
            asyncio.run(run_console(settings_for(args, args.state_dir)))
    except KeyboardInterrupt:
        print("已中断终端会话。", flush=True)
    except (HarnessError, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
