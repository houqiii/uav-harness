import asyncio
import base64
import io
import time

import pytest
from PIL import Image

from uav_harness.drone4d.capture import CaptureBroker
from uav_harness.drone4d.port import enu_to_ned, heading_from_enu
from uav_harness.errors import HarnessError


def test_world_enu_is_converted_without_mixing_heading_conventions():
    assert enu_to_ned([10, 20, 30]) == (20, 10, -30)
    assert heading_from_enu(0) == pytest.approx(90)
    assert heading_from_enu(3.141592653589793 / 2) == pytest.approx(0)


async def test_missing_renderer_is_explicit_and_does_not_invent_observation():
    broker = CaptureBroker(timeout_s=0.03)
    with pytest.raises(HarnessError, match="renderer|渲染"):
        await broker.capture("uav-1")
    assert not broker.pending


async def test_capture_requires_correlated_vehicle_request_and_camera_pose():
    broker = CaptureBroker(timeout_s=1)
    task = asyncio.create_task(broker.capture("uav-1"))
    await asyncio.sleep(0)
    req = broker.requests()[0]
    raw = io.BytesIO()
    Image.new("RGB", (320, 240), (60, 100, 120)).save(raw, format="JPEG")
    reply = {
        **req,
        "captured_at": time.time(),
        "t_sim_ns": 1234,
        "image_b64": base64.b64encode(raw.getvalue()).decode(),
        "camera": {
            "width": 320,
            "height": 240,
            "fx": 300,
            "fy": 300,
            "cx": 160,
            "cy": 120,
            "yaw_deg": 0,
            "pitch_deg": -35,
            "roll_deg": 0,
            "position_ned": [0, 0, -50],
            "ground_down_m": 0,
            "ground_uncertainty_m": 1,
            "source": "drone4d_native_fpv",
            "timing": "simulated_capture",
        },
    }
    with pytest.raises(HarnessError, match="vehicle"):
        broker.deliver({**reply, "vehicle_id": "other"})
    broker.deliver(reply)
    frame = await task
    assert frame["image"].startswith(b"\xff\xd8")
    assert frame["camera"].width == 320
    assert not broker.pending


def wire_state():
    return {
        "t_sim_ns": "1000000000",
        "state": {
            "pos_enu_m": [10, 20, 30],
            "vel_enu_mps": [0, 0, 0],
            "home_enu_m": [10, 20, 0],
            "q_xyzw": [0, 0, 0, 1],
            "flight_state": "HOVER",
            "in_air": True,
        },
    }


def test_camera_pose_changes_during_inference_require_reobservation():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort

    p = Drone4DPort(None, "uav-1")
    p.latest = wire_state()
    p.latest_at = time.monotonic()
    p.latest_sensors = {"payload": {"gimbal": {"pitch_rad": -0.5, "yaw_rad": 0}}}
    p.sensors_at = time.monotonic()
    p.last_camera = SimpleNamespace(model_dump=lambda: {"pitch_deg": -28.6})
    p.capture_signature = p.pose_signature()
    assert p.camera_pose()["pitch_deg"] == -28.6
    p.latest_sensors["payload"]["gimbal"]["pitch_rad"] = -0.7
    with pytest.raises(HarnessError, match="changed"):
        p.camera_pose()


async def test_cancellation_drains_late_sdk_send_before_final_hover():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort

    gate = asyncio.Event()
    entered = asyncio.Event()
    events = []

    async def call(fn, vid, name, **kwargs):
        assert kwargs["timeout_s"] <= 30
        if name == "goto":
            entered.set()
            await gate.wait()
            events.append("late goto")
        else:
            events.append(name)
        return {"status": "succeeded"}

    p = Drone4DPort(
        SimpleNamespace(
            client=SimpleNamespace(call=call, session=SimpleNamespace(command=object()))
        ),
        "uav-1",
    )

    async def hold():
        events.append("final hover")

    p.hold = hold
    task = asyncio.create_task(p.command("goto", pos=[1, 2, 3]))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert events == ["hover"] and not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["hover", "late goto", "final hover"]


async def test_gimbal_waits_for_observed_angles_not_command_ack():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort, telemetry_from_state

    p = Drone4DPort(SimpleNamespace(config=SimpleNamespace(speed_mps=3)), "uav-1")
    updates = 0

    async def refresh():
        nonlocal updates
        updates += 1
        p.latest_sensors = {
            "payload": {
                "gimbal": {
                    "pitch_rad": -0.5235987756 if updates >= 4 else 0,
                    "yaw_rad": 0,
                }
            }
        }
        return telemetry_from_state(wire_state())

    async def command(*args, **kwargs):
        return {"status": "succeeded"}

    p.refresh = refresh
    p.command = command
    result = await p.execute({"action": "gimbal", "pitch_deg": -30})
    assert result["status"] == "succeeded" and updates >= 5


def test_sandbox_native_auth_and_hardware_routes_are_isolated(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from uav_harness.api import create_app
    from uav_harness.dashboard import offline_settings
    import uav_harness.drone4d.routes as routes

    monkeypatch.setenv("HARNESS_API_TOKEN", "management-secret")
    settings = offline_settings(tmp_path)
    settings.drone4d.enabled = True
    settings.drone4d.web_dir = tmp_path / "native"
    settings.drone4d.web_dir.mkdir()
    (settings.drone4d.web_dir / "index.html").write_text("<p>native</p>")
    original = httpx.AsyncClient
    seen = []

    def remote(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        return httpx.Response(
            200,
            json={"token": "renewed-secret"}
            if req.url.path.endswith("/keepalive")
            else {"ok": True},
        )

    monkeypatch.setattr(
        routes.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(remote)),
    )
    with TestClient(create_app(settings, offline=True)) as client:
        svc = client.app.state.sandbox
        svc.client.session = SimpleNamespace(
            sid="owned",
            token="sandbox-secret",
            expires_unix_ns=99,
            world="synthcity",
            info={},
            path=lambda *parts: (
                "/api/sandbox/v1/sessions/owned"
                + ("/" + "/".join(parts) if parts else "")
            ),
        )
        svc.client.sandbox = SimpleNamespace(
            http=SimpleNamespace(
                client=httpx.Client(
                    base_url="https://drone4d.agentnetwork.org.cn",
                    transport=httpx.MockTransport(remote),
                )
            )
        )
        svc.remember_token("sandbox-secret")
        svc.state = "ready"

        async def close():
            svc.client.session = None

        svc.client.close = close
        assert client.get("/v1/sandbox/bridge").status_code == 401
        assert client.get("/sandbox/owned").status_code == 200
        assert client.get("/api/sandbox/v1/sessions/other").status_code == 403
        assert client.get("/api/sandbox/v1/sessions/owned").status_code == 401
        auth = {"Authorization": "Bearer sandbox-secret"}
        assert (
            client.get("/api/sandbox/v1/sessions/owned", headers=auth).status_code
            == 200
        )
        assert seen[-1][1] == "Bearer sandbox-secret"
        assert (
            client.post("/api/sandbox/v1/sessions", headers=auth, json={}).status_code
            == 403
        )
        assert (
            client.post(
                "/api/sandbox/v1/sessions/owned/keepalive", headers=auth, json={}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/sandbox/v1/sessions/owned",
                headers={"Authorization": "Bearer renewed-secret"},
            ).status_code
            == 200
        )
        assert svc.client.session.token == "renewed-secret"
        assert "renewed-secret" in svc.client.state_file.read_text()
        # The browser may still send its previous credential during renewal.
        assert (
            client.get("/api/sandbox/v1/sessions/owned", headers=auth).status_code
            == 200
        )
        assert seen[-1][1] == "Bearer renewed-secret"
        assert (
            client.post("/v1/sandbox/render/frame", headers=auth, json={}).status_code
            == 401
        )
        admin = {"Authorization": "Bearer management-secret"}
        assert (
            client.get("/v1/sandbox/render/requests?sid=old", headers=admin).status_code
            == 409
        )
        assert (
            client.get(
                "/v1/sandbox/render/requests?sid=owned", headers=admin
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/sandbox/render/status",
                headers=admin,
                json={"sid": "old", "ready": True},
            ).status_code
            == 409
        )
        hardware = client.get("/v1/brain", headers=admin).json()
        assert hardware["mode"] == "hardware_disconnected"
        assert not hardware["hardware_control_enabled"]
        assert all(
            not v["camera_configured"] and not v["execution_enabled"]
            for v in hardware["vehicles"]
        )
        assert (
            client.post(
                "/v1/brain/preview", headers=admin, json={"vehicle_id": "px4-1"}
            ).status_code
            == 409
        )
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/sandbox/v1/sessions/owned/rt", subprotocols=["awr.rt.v1"]
            ):
                pass
        import websockets

        protocols = []

        class Remote:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def send(self, message):
                pass

            async def __aiter__(self):
                yield b"native frame"

        def connect(url, **kwargs):
            protocols.extend(kwargs["subprotocols"])
            return Remote()

        monkeypatch.setattr(websockets, "connect", connect)
        with client.websocket_connect(
            "/api/sandbox/v1/sessions/owned/rt",
            subprotocols=["awr.rt.v1", "bearer.sandbox-secret"],
        ) as ws:
            assert ws.receive_bytes() == b"native frame"
        assert protocols == ["awr.rt.v1", "bearer.renewed-secret"]
        assert "sandbox-secret" not in client.get("/v1/sandbox", headers=admin).text


async def test_cancel_during_spoolup_sends_hover_and_waits_for_hold():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort, telemetry_from_state

    p = Drone4DPort(SimpleNamespace(), "uav-1")
    updates = 0
    commands = []

    async def refresh():
        nonlocal updates
        updates += 1
        p.latest = wire_state()
        p.latest["state"].update(
            in_air=False,
            flight_state="TAKING_OFF" if updates < 3 else "FLYING",
            ctrl_mode="TAKEOFF" if updates < 3 else "HOLD",
        )
        return telemetry_from_state(p.latest)

    async def command(name, **kwargs):
        commands.append(name)
        return {"status": "accepted"}

    p.refresh = refresh
    p.command = command
    result = await p.hold()
    assert commands == ["hover"] and updates >= 4
    assert result["completion_scope"] == "drone4d_observed_hover"


async def test_yaw_uses_direct_position_hold_while_translation_keeps_path_admission():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort, telemetry_from_state

    p = Drone4DPort(SimpleNamespace(config=SimpleNamespace(speed_mps=3)), "uav-1")
    commands = []

    async def refresh():
        return telemetry_from_state(wire_state())

    async def command(name, **kwargs):
        commands.append((name, kwargs))
        return {"status": "accepted"}

    async def wait(*a, **kw):
        pass

    async def hold():
        pass

    p.refresh = refresh
    p.command = command
    p.wait = wait
    p.hold = hold
    await p.execute({"action": "yaw", "heading_deg": 120})
    assert commands[-1][1]["route"] == "direct"
    assert commands[-1][1]["yaw_rad"] == pytest.approx(-0.5235987756)
    await p.execute({"action": "move", "offset_ned": [3, 4, 0]})
    assert commands[-1][1]["route"] == "safe_transit"


def test_optional_battery_extension_may_be_null_during_startup():
    from uav_harness.drone4d.port import telemetry_from_state

    wire = wire_state()
    wire["state_ext"] = None
    assert telemetry_from_state(wire).battery_percent is None
    wire["state_ext"] = {"battery": None}
    assert telemetry_from_state(wire).battery_percent is None


def test_public_creation_rate_wait_respects_retry_after_without_reusing_pow(tmp_path):
    from types import SimpleNamespace
    from uav_harness.drone4d.client import DroneClient
    from uav_harness.drone4d.settings import Drone4DConfig

    c = DroneClient(Drone4DConfig(), tmp_path)
    calls = []
    waits = []

    class Quota(Exception):
        status = 429
        retry_after_s = 123

    def create(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Quota()
        return "new session"

    c.sdk = SimpleNamespace(AwrError=Quota)
    c.sandbox = SimpleNamespace(create=create)
    c.cancel_create = SimpleNamespace(
        is_set=lambda: False, wait=lambda seconds: waits.append(seconds)
    )
    assert c._create({}) == "new session"
    assert waits == [123] and len(calls) == 2
    assert not calls[0]["wait_ready"] and "pow" not in calls[1]


def test_lost_credential_uses_second_slot_once_not_an_unbounded_creation_loop(tmp_path):
    from types import SimpleNamespace
    from uav_harness.drone4d.client import DroneClient
    from uav_harness.drone4d.settings import Drone4DConfig

    class Exists(Exception):
        code = 409
        status = 0  # SDK-generated admission errors have no HTTP response.
        name = "SANDBOX_EXISTS"

    c = DroneClient(Drone4DConfig(), tmp_path)
    attempts = []

    def create(*args, **kwargs):
        attempts.append(kwargs["force_new"])
        raise Exists()

    c.sdk = SimpleNamespace(AwrError=Exists)
    c.sandbox = SimpleNamespace(create=create)
    with pytest.raises(Exists):
        c._create({})
    assert attempts == [False, True]


async def test_camera_setup_waits_for_vehicle_ready_not_only_session_ready(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from uav_harness.drone4d.service import Drone4DService
    from uav_harness.dashboard import offline_settings

    monkeypatch.setattr("uav_harness.drone4d.service.search_scene", lambda _: {})
    s = Drone4DService(SimpleNamespace(settings=offline_settings(tmp_path)))
    updates = 0
    commands = []

    def vehicle_state(vid):
        nonlocal updates
        updates += 1
        w = wire_state()
        w["state"]["lifecycle"] = "READY" if updates >= 2 else "WARMING"
        return w

    def command(vid, name, **kwargs):
        assert updates >= 2
        commands.append(name)
        return {"status": "accepted"}

    session = SimpleNamespace(
        state=lambda: {"state": "READY"},
        command=command,
        vehicles=SimpleNamespace(
            list=lambda: {"items": [{"id": "harness-01"}]},
            state=vehicle_state,
            sensors=lambda vid: {
                "payload": {"gimbal": {"pitch_rad": -0.6, "yaw_rad": 0}}
            },
        ),
    )

    async def call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def connect():
        return {"build": {"version": "0.2.0"}}

    async def create(snapshot):
        pass

    async def close():
        pass

    s.client = SimpleNamespace(
        call=call,
        connect=connect,
        create=create,
        close=close,
        session=session,
        recovered=False,
    )
    s._closing = True
    await s._connect()
    assert s.state == "ready" and commands == ["gimbal", "zoom"]
    await s.poll_task


async def test_failed_endpoint_switch_does_not_discard_previous_session_credentials(
    tmp_path,
):
    import json
    from uav_harness.drone4d.client import DroneClient
    from uav_harness.drone4d.settings import Drone4DConfig

    c = DroneClient(Drone4DConfig(), tmp_path)
    saved = {
        "base_url": "https://previous.example",
        "sid": "old",
        "token": "test-only-token",
    }
    c.state_file.write_text(json.dumps(saved))
    await c.close()
    assert json.loads(c.state_file.read_text()) == saved


async def test_payload_keepalive_renews_latest_planned_angle_in_simulation_time():
    from uav_harness.drone4d.port import Drone4DPort

    p = Drone4DPort(None, "uav-1")
    p.latest = wire_state()
    calls = []

    async def send(name, **kwargs):
        calls.append(kwargs)
        return {"status": "accepted"}

    p._send_command = send
    await p.command("gimbal", mode="angles", pitch_rad=-0.6, yaw_rad=0)
    p.latest["t_sim_ns"] = "25000000000"
    await p.renew_payload()
    await p.command("gimbal", mode="angles", pitch_rad=-0.9, yaw_rad=0.2)
    p.latest["t_sim_ns"] = "50000000000"
    await p.renew_payload()
    assert [a["pitch_rad"] for a in calls] == [-0.6, -0.6, -0.9, -0.9]
    assert calls[-1]["yaw_rad"] == 0.2
    p.latest["t_sim_ns"] = "1000000000"
    await p.renew_payload()
    assert len(calls) == 5 and calls[-1]["pitch_rad"] == -0.9


async def test_optical_zoom_waits_for_actual_focal_length_and_known_spec():
    from types import SimpleNamespace
    from uav_harness.drone4d.port import Drone4DPort, telemetry_from_state

    p = Drone4DPort(None, "uav-1")
    updates = 0
    commands = []

    async def refresh():
        nonlocal updates
        updates += 1
        p.latest_sensors = {
            "sensors": [{"type": "eo_zoom", "spec_ref": "eo_gx40@1.0.0"}],
            "payload": {
                "sensors": [{"sensor": "eo", "f_mm": 14.4 if updates >= 4 else 4.8}]
            },
        }
        return telemetry_from_state(wire_state())

    async def command(name, **kwargs):
        commands.append((name, kwargs))
        return {"status": "accepted"}

    p.refresh = refresh
    p.command = command
    result = await p.execute({"action": "zoom", "zoom_x": 3})
    assert result["status"] == "succeeded" and updates >= 5
    assert commands == [("zoom", {"sensor": "eo", "zoom_x": 3})]
    p.latest_sensors["sensors"][0]["spec_ref"] = "unknown"
    assert not p.zoom_available()
