"""Passive payload telemetry, independent from flight-controller command routing."""
import math
import time
from .transport import json_safe


def euler_degrees(q):
    if len(q) != 4 or not all(math.isfinite(v) for v in q):
        return None
    norm = math.sqrt(sum(v*v for v in q))
    if norm < 1e-6:
        return None
    w, x, y, z = (v/norm for v in q)
    return dict(zip(('roll', 'pitch', 'yaw'), map(math.degrees, (
        math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y)),
        math.asin(max(-1, min(1, 2*(w*y-z*x)))),
        math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z)),
    ))))


class PayloadTelemetry:
    def __init__(self, system_id, settings):
        self.system_id, self.settings = system_id, settings
        self.gimbals, self.streams = {}, {}

    def receive(self, msg, stamp):
        if msg.get_srcSystem() != self.system_id:
            return
        kind, comp = msg.get_type(), msg.get_srcComponent()
        if kind == 'GIMBAL_DEVICE_ATTITUDE_STATUS':
            key = (comp, getattr(msg, 'gimbal_device_id', 0))
            if key in self.gimbals or len(self.gimbals) < 32:
                self.gimbals[key] = (msg, stamp)
        elif kind == 'VIDEO_STREAM_INFORMATION':
            key = (comp, msg.stream_id)
            if key in self.streams or len(self.streams) < 32:
                self.streams[key] = (msg, stamp)

    def snapshot(self):
        now = time.monotonic()
        devices = []
        for (comp, device), (msg, stamp) in self.gimbals.items():
            fresh = now-stamp <= self.settings.telemetry_timeout_s
            flags = msg.flags
            frame = 'invalid' if flags & 96 == 96 else 'earth' if flags & 64 else 'vehicle' if flags & 32 else 'earth' if flags & 16 else 'vehicle'
            devices.append({'component_id': comp, 'device_id': device, 'connected': fresh,
                            'age_s': round(max(0, now-stamp), 2), 'yaw_frame': frame,
                            'attitude_deg': euler_degrees(msg.q) if fresh else None,
                            'failure_flags': msg.failure_flags if fresh else None})
        # Stream information is static metadata; receiving it is not proof that video is live.
        streams = [{'component_id': comp, 'stream_id': stream, 'age_s': round(max(0, now-stamp), 2),
                    'uri': msg.uri, 'name': msg.name, 'type': msg.type,
                    'width': msg.resolution_h, 'height': msg.resolution_v, 'framerate': msg.framerate}
                   for (comp, stream), (msg, stamp) in self.streams.items()]
        return json_safe({'gimbals': devices, 'video_streams': streams})
