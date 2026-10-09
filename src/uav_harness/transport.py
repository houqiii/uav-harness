"""One MAVLink owner per physical link, addressed sessions and optional observers."""
from __future__ import annotations

import asyncio
from collections import deque
import math
import socket
import struct
import time
from pathlib import Path
import serial
from pymavlink.dialects.v20 import ardupilotmega as mav
from .errors import HarnessError, Rejected, Uncertain


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


class MAVLinkTransport:
    def __init__(self, config, settings, targets, event=lambda *a, **k: None):
        self.config, self.settings, self.targets, self.event = config, settings, targets, event
        self.tx = mav.MAVLink(self, srcSystem=settings.source_system, srcComponent=settings.source_component)
        self.rx = mav.MAVLink(None)
        self.rx.robust_parsing = True
        self.sock = self.serial = self.mirror = None
        self.peer = (config.peer_host, config.peer_port) if config.peer_port else None
        self.tasks = []
        self.listeners = []
        self.pending = {}
        self.poisoned = set()
        self.locks = {}
        self.fault = None
        self.record = None
        self.events = deque(maxlen=4000)

    def emit(self, kind, **fields):
        item = json_safe({"link": self.config.name, "kind": kind, "at": time.time(), **fields})
        self.events.append(item)
        self.event(item)

    async def open(self):
        if self.config.kind == "serial":
            self.serial = serial.Serial(port=None, baudrate=self.config.baud, timeout=.05, write_timeout=.5)
            self.serial.port = self.config.device
            self.serial.dtr = self.serial.rts = False
            self.serial.open()
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self.sock.bind((self.config.bind_host, self.config.bind_port))
        self.mirror = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        folder = Path(self.settings.state_dir)
        folder.mkdir(parents=True, exist_ok=True)
        self.record = (folder / f"{self.config.name}-{time.time_ns()}.tlog").open("xb")
        self.tasks = [asyncio.create_task(self._receive()), asyncio.create_task(self._heartbeat())]

    def write(self, data):
        if self.fault:
            raise Uncertain(f"link unavailable: {self.fault}")
        if self.serial:
            if self.serial.write(data) != len(data):
                raise Uncertain("incomplete serial write")
        elif self.peer:
            if self.sock.sendto(data, self.peer) != len(data):
                raise Uncertain("incomplete UDP write")
        else:
            raise HarnessError("UDP peer not established by expected heartbeat")

    async def _receive(self):
        try:
            loop = asyncio.get_running_loop()
            while True:
                if self.serial:
                    data = await asyncio.to_thread(self.serial.read, self.serial.in_waiting or 1)
                else:
                    data, peer = await loop.sock_recvfrom(self.sock, 65535)
                    if self.peer and peer != self.peer:
                        continue
                    if not self.peer and peer[0] != self.config.peer_host:
                        continue
                if not data:
                    continue
                for msg in self.rx.parse_buffer(data) or []:
                    if msg.get_type() == "BAD_DATA":
                        continue
                    source = (msg.get_srcSystem(), msg.get_srcComponent())
                    if not self.serial and self.peer is None:
                        if msg.get_type() != "HEARTBEAT" or self.targets.get(source) != msg.autopilot:
                            continue
                        self.peer = peer
                    stamp = time.monotonic()
                    raw = bytes(msg.get_msgbuf())
                    self.record.write(struct.pack(">Q", int(time.time()*1e6)) + raw)
                    for destination in self.config.observers:
                        host, port = destination.rsplit(":", 1)
                        self.mirror.sendto(raw, (host, int(port)))
                    if source not in self.targets:
                        continue
                    if msg.get_type() == "COMMAND_ACK":
                        key = (*source, msg.command)
                        future = self.pending.get(key)
                        if future and not future.done() and (msg.target_system, msg.target_component) == (self.settings.source_system, self.settings.source_component):
                            self.emit("command_ack", source=source, message=msg.to_dict())
                            if msg.result != mav.MAV_RESULT_IN_PROGRESS:
                                future.set_result(msg.result)
                    for callback in self.listeners:
                        callback(msg, stamp)
                self.record.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fault = f"{type(exc).__name__}: {exc}"
            self.emit("link_fault", error=self.fault)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(Uncertain(self.fault))

    async def _heartbeat(self):
        try:
            while True:
                if self.serial or self.peer:
                    self.tx.heartbeat_send(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fault = f"heartbeat failed: {type(exc).__name__}"

    async def command(self, system, component, command, values=()):
        key = (system, component, command)
        lock = self.locks.setdefault((system, component), asyncio.Lock())
        async with lock:
            if key in self.poisoned:
                raise Uncertain("previous command outcome unresolved; reconcile before reuse")
            future = asyncio.get_running_loop().create_future()
            self.pending[key] = future
            args = list(values) + [0] * (7-len(values))
            try:
                self.emit("command_sent", target=[system, component], command=command, params=args)
                self.tx.command_long_send(system, component, command, 0, *args)
                result = await asyncio.wait_for(future, self.settings.ack_timeout_s)
                if result != mav.MAV_RESULT_ACCEPTED:
                    raise Rejected(command, result)
                return {"command": command, "result": "ACCEPTED"}
            except (TimeoutError, asyncio.CancelledError):
                self.poisoned.add(key)
                raise Uncertain(f"command {command}: no terminal directed ACK; no automatic retry") from None
            except (OSError, serial.SerialException, Uncertain) as exc:
                self.poisoned.add(key)
                raise Uncertain(f"command {command}: transmission/response uncertain ({type(exc).__name__})") from None
            finally:
                self.pending.pop(key, None)

    def setpoint(self, system, component, position, yaw):
        self.tx.set_position_target_local_ned_send(
            int(time.monotonic()*1000) & 0xffffffff, system, component,
            mav.MAV_FRAME_LOCAL_NED, 2552, *position, 0, 0, 0, 0, 0, 0, yaw, 0)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for resource in (self.sock, self.serial, self.mirror, self.record):
            if resource:
                resource.close()
