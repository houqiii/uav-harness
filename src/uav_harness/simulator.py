"""MAVLink protocol test double. This is neither ArduPilot nor PX4 firmware SITL."""
from __future__ import annotations

import asyncio
import math
import socket
import time
from .transport import mav


class ProtocolSimulator:
    def __init__(self, config, destination):
        self.config, self.destination = config, destination
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("127.0.0.1", 0))
        self.tx = mav.MAVLink(self, srcSystem=config.system_id, srcComponent=config.component_id)
        self.rx = mav.MAVLink(None)
        self.tasks = []
        self.commands, self.setpoints = [], []
        self.drop_ack = set()
        self.reject = {}
        self.ack_target = None
        self.ignore_effect = set()
        self.stop_telemetry = False
        self.stop_position = False
        self.armed = False
        self.main, self.sub = (0, 0) if config.backend == "ardupilot" else (1, 0)
        self.position = [0., 0., 0.]
        self.target = list(self.position)
        self.velocity = [0., 0., 0.]
        self.yaw = 0.
        self.landing = False
        self.motor_until = 0
        self.motor_pwm = 1231
        self.motor_seq = 1
        self.probe = False
        self.started = time.monotonic()
        self.setpoint_hz_start = None
        self.flags = 167 if config.profile == "model_bench" else 47

    def write(self, data):
        self.sock.sendto(data, self.destination)

    async def open(self):
        self.tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.telemetry())]

    def ack(self, msg, result):
        if msg.command not in self.drop_ack:
            dest = self.ack_target or (msg.get_srcSystem(), msg.get_srcComponent())
            self.tx.command_ack_send(msg.command, result, target_system=dest[0], target_component=dest[1])

    async def receive(self):
        loop = asyncio.get_running_loop()
        while True:
            data, _ = await loop.sock_recvfrom(self.sock, 65535)
            for msg in self.rx.parse_buffer(data) or []:
                if msg.get_type() not in ("HEARTBEAT", "BAD_DATA") and (msg.target_system, msg.target_component) != (self.config.system_id, self.config.component_id):
                    continue
                if msg.get_type() == "PARAM_REQUEST_READ":
                    values = {"MOT_PWM_TYPE": 0, "MOT_PWM_MIN": 1051, "MOT_PWM_MAX": 1951}
                    if msg.param_id in values:
                        self.tx.param_value_send(msg.param_id.encode(), values[msg.param_id], mav.MAV_PARAM_TYPE_REAL32, len(values), 0)
                elif msg.get_type() == "COMMAND_LONG":
                    self.commands.append(msg.to_dict())
                    cmd, result = msg.command, self.reject.get(msg.command, 0)
                    if result or cmd in self.ignore_effect:
                        self.ack(msg, result)
                        continue
                    if cmd == 512 and msg.param1 == 148:
                        major, minor = self.config.firmware or ((4,5) if self.config.backend == "ardupilot" else (1,16))
                        self.tx.autopilot_version_send(128, (major<<24)|(minor<<16), 0, 0,
                                                      (self.config.expected_board_id or 1010)<<16,
                                                      [0]*8, [0]*8, [0]*8, 0, 0, 0)
                    elif cmd == 512 and msg.param1 == 242:
                        self.home()
                    elif cmd == 176:
                        if self.config.backend == "px4" and msg.param2 == 6 and (self.setpoint_hz_start is None or time.monotonic()-self.setpoint_hz_start < 1):
                            result = mav.MAV_RESULT_DENIED
                        else:
                            self.main, self.sub = int(msg.param2), int(msg.param3)
                            self.landing = self.main == 9 if self.config.backend == "ardupilot" else (self.main,self.sub)==(4,6)
                    elif cmd == 400:
                        if not msg.param1 and self.position[2] < -.05:
                            result = 4
                        else:
                            self.armed = bool(msg.param1)
                    elif cmd == 22:
                        if not self.armed:
                            result = 4
                        elif self.config.profile == "model_bench":
                            if self.main != 2 or not msg.param3:
                                result = 4
                            else:
                                self.probe = True
                        else:
                            altitude = msg.param7-(100 if self.config.backend == "px4" else 0)
                            self.target[2] = -altitude
                            if self.config.backend == "px4":
                                self.main, self.sub = 4, 2
                    elif cmd == 21:
                        self.main, self.sub, self.landing = 4, 6, True
                    elif cmd == 209:
                        self.armed = True
                        self.motor_until = time.monotonic()+msg.param4
                        self.motor_pwm, self.motor_seq = int(msg.param3), int(msg.param1)
                    self.ack(msg, result)
                elif msg.get_type() == "SET_POSITION_TARGET_LOCAL_NED":
                    self.setpoints.append(msg.to_dict())
                    self.setpoint_hz_start = self.setpoint_hz_start or time.monotonic()
                    if self.config.backend == "ardupilot" and self.main == 4 or self.config.backend == "px4" and self.main == 6:
                        self.target = [msg.x, msg.y, msg.z]

    def home(self):
        self.tx.home_position_send(300000000, 1200000000, 100000, 0, 0, 0, [1,0,0,0], 0, 0, 0)

    async def telemetry(self):
        while True:
            now = time.monotonic()
            if self.motor_until and now >= self.motor_until:
                self.motor_until, self.armed = 0, False
                self.tx.statustext_send(6, b"finished motor test")
            if self.landing:
                self.target[2] = 0
            for i in range(3):
                delta = self.target[i]-self.position[i]
                step = max(-.1, min(.1, delta))
                self.position[i] += step
                self.velocity[i] = step/.05
            if self.landing and abs(self.position[2]) < .01:
                self.armed, self.landing, self.probe = False, False, False
            if not self.stop_telemetry:
                custom = self.main if self.config.backend == "ardupilot" else (self.main<<16)|(self.sub<<24)
                self.tx.heartbeat_send(2, 3 if self.config.backend == "ardupilot" else 12, 81|(128 if self.armed else 0), custom, 4 if self.armed else 3)
                self.tx.extended_sys_state_send(0, 4 if self.landing else 2 if self.position[2]<-.05 else 1)
                self.tx.attitude_send(int((now-self.started)*1000), 0, 0, self.yaw, 0, 0, 0)
                self.tx.sys_status_send(0, 0, 0, 10, 12000, 0, 90, 0, 0, 0, 0, 0, 0)
                self.tx.system_time_send(0, int((now-self.started)*1000))
                if self.config.backend == "ardupilot":
                    self.tx.ekf_status_report_send(self.flags, 0, 0, 0, 0, 0)
                else:
                    self.tx.estimator_status_send(int(now*1e6), self.flags, 0, 0, 0, 0, 0, 0, 0, 0)
                if not self.stop_position:
                    self.tx.local_position_ned_send(int(now*1000)&0xffffffff, *self.position, *self.velocity)
                pwm = [1600 if self.probe else 1150 if self.armed else 1051]*8
                if self.motor_until:
                    pwm = [1051]*8
                    pwm[self.motor_seq-1] = self.motor_pwm
                self.tx.servo_output_raw_send(int(now*1e6)&0xffffffff, 0, *pwm)
                self.home()
            await asyncio.sleep(.05)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.sock.close()
