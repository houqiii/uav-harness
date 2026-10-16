"""Correlated native FPV readbacks. No renderer means no observation."""

import asyncio
import base64
import io
import time
import uuid

from PIL import Image
from ..brain.contracts import Camera
from ..errors import HarnessError


class CaptureBroker:
    def __init__(self, timeout_s=30):
        self.timeout_s = timeout_s
        self.pending = {}
        self.last_renderer_at = 0
        self.renderer_status = {}

    async def capture(self, vehicle_id):
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = {
            "request_id": rid,
            "vehicle_id": vehicle_id,
            "requested_at": time.time(),
            "future": future,
        }
        try:
            return await asyncio.wait_for(future, self.timeout_s)
        except TimeoutError:
            raise HarnessError(
                "Drone4D renderer 未返回云台图像；保持沙盒页面打开并等待世界载入，无备用假图像"
            ) from None
        finally:
            self.pending.pop(rid, None)

    def requests(self):
        self.last_renderer_at = time.time()
        return [
            {k: v for k, v in x.items() if k != "future"}
            for x in self.pending.values()
            if not x["future"].done()
        ]

    def deliver(self, body):
        pending = self.pending.get(body.get("request_id"))
        if not pending or pending["future"].done():
            raise HarnessError("capture request expired or unknown")
        if body.get("vehicle_id") != pending["vehicle_id"]:
            raise HarnessError("capture vehicle mismatch")
        if body.get("error"):
            pending["future"].set_exception(
                HarnessError("Drone4D renderer: " + str(body["error"])[:300])
            )
            return
        stamp = float(body["captured_at"])
        if not pending["requested_at"] <= stamp <= time.time() + 1:
            raise HarnessError("capture timestamp stale or outside request")
        raw = body.get("image_b64", "")
        if len(raw) > 8 * 1024 * 1024:
            raise HarnessError("capture image exceeds size bound")
        try:
            data = base64.b64decode(raw, validate=True)
            camera = Camera.model_validate(body["camera"])
            image = Image.open(io.BytesIO(data))
            if image.size != (camera.width, camera.height):
                raise ValueError("dimensions")
            image.verify()
            sim_ns = int(body["t_sim_ns"])
            if sim_ns < 0:
                raise ValueError("negative simulation time")
        except (ValueError, KeyError, OSError):
            raise HarnessError("invalid rendered image/calibrated camera") from None
        pending["future"].set_result(
            {"image": data, "camera": camera, "captured_at": stamp, "t_sim_ns": sim_ns}
        )
