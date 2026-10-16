"""Dashboard-owned sandbox endpoints plus a scoped native-client reverse proxy."""

import asyncio
import json
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from ..brain.contracts import MissionRequest
from ..contracts import Contract, Id


class PreviewRequest(Contract):
    vehicle_id: Id


class ResetRequest(Contract):
    seed: int = 17


def register_routes(app, settings):
    def service():
        return app.state.sandbox

    def renderer_scope(sid):
        s = service().client.session
        if not s or sid != s.sid:
            raise HTTPException(
                409, "renderer belongs to a different sandbox session; reload the view"
            )

    @app.get("/v1/sandbox")
    async def sandbox_metadata():
        return service().session_metadata()

    @app.post("/v1/sandbox/connect", status_code=202)
    async def connect_sandbox():
        return await service().connect()

    @app.post("/v1/sandbox/disconnect")
    async def disconnect_sandbox():
        return await service().disconnect()

    @app.post("/v1/sandbox/reset", status_code=202)
    async def reset_sandbox(body: ResetRequest):
        if not 0 <= body.seed <= 2**32 - 1:
            raise HTTPException(422, "seed outside range")
        return await service().reset(body.seed)

    @app.get("/v1/sandbox/bridge")
    async def bridge_config():
        return service().bridge_config()

    @app.get("/v1/sandbox/render/requests")
    async def render_requests(sid: str = ""):
        renderer_scope(sid)
        return {"requests": service().capture.requests()}

    @app.post("/v1/sandbox/render/frame")
    async def render_frame(request: Request):
        raw = await request.body()
        if len(raw) > 9 * 1024 * 1024:
            raise HTTPException(413, "native capture too large")
        body = json.loads(raw)
        renderer_scope(body.get("sid"))
        service().capture.deliver(body)
        return {"accepted": True}

    @app.post("/v1/sandbox/render/status")
    async def render_status(request: Request):
        data = await request.json()
        renderer_scope(data.get("sid"))
        service().capture.renderer_status = {
            k: data[k]
            for k in ("ready", "error", "world_id", "camera_mode")
            if k in data
        }
        return {"accepted": True}

    @app.get("/v1/sandbox/brain")
    async def brain_metadata():
        return service().metadata()

    @app.post("/v1/sandbox/brain/missions", status_code=202)
    async def start_mission(body: MissionRequest):
        return await service().start(body)

    @app.get("/v1/sandbox/brain/missions/{mid}")
    async def get_mission(mid: str):
        return service().get(mid).snapshot()

    @app.post("/v1/sandbox/brain/missions/{mid}/{action}")
    async def control_mission(mid: str, action: str):
        return await service().control(mid, action)

    @app.get("/v1/sandbox/brain/missions/{mid}/frames/{fid}")
    async def mission_image(mid: str, fid: str):
        obs = service().get(mid).frames.get(fid)
        if not obs:
            raise HTTPException(404, "frame expired")
        return Response(
            obs.image, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    @app.post("/v1/sandbox/brain/preview")
    async def preview(body: PreviewRequest):
        obs = await service().preview(body.vehicle_id)
        data = obs.model_dump()
        active = service().missions.get(service().owners.get(body.vehicle_id))
        data["image_url"] = (
            f"/v1/sandbox/brain/missions/{active.mission_id}/frames/{obs.observation_id}"
            if active
            else f"/v1/sandbox/brain/preview/{body.vehicle_id}/image?observation_id={obs.observation_id}"
        )
        return data

    @app.get("/v1/sandbox/brain/preview/{vid}/image")
    async def preview_image(vid: str):
        obs = service().preview_frames.get(vid)
        if not obs:
            raise HTTPException(404, "preview unavailable")
        return Response(
            obs.image, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    @app.get("/world/{world_id}", include_in_schema=False)
    async def native_view(world_id: str):
        if world_id != settings.drone4d.world_id:
            raise HTTPException(404, "world not configured")
        path = settings.drone4d.web_dir / "index.html"
        if not path.is_file():
            raise HTTPException(
                503, "Native Drone4D frontend not built. Run scripts/prepare-drone4d.py"
            )
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/sandbox/{sid}", include_in_schema=False)
    async def native_session_view(sid: str):
        s = service().client.session
        if not s or s.sid != sid:
            raise HTTPException(404, "owned sandbox session unavailable")
        return FileResponse(
            settings.drone4d.web_dir / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    # The native build has its own base; it never replaces the Harness /assets.
    if settings.drone4d.enabled:
        settings.drone4d.web_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/drone4d-assets",
            StaticFiles(directory=settings.drone4d.web_dir),
            name="drone4d-assets",
        )
        for part in ("brand", "models", "fonts"):
            folder = settings.drone4d.web_dir / part
            if folder.is_dir():
                app.mount(
                    "/" + part, StaticFiles(directory=folder), name="drone4d-" + part
                )

    def check_scope(path, method, authorization):
        s = service().client.session
        if not s:
            raise HTTPException(409, "sandbox session not connected")
        prefix = "api/sandbox/v1/sessions/"
        if path.startswith(prefix) and path[len(prefix) :].split("/")[0] != s.sid:
            raise HTTPException(403, "proxy is scoped to this owned sandbox only")
        if path.startswith(prefix) and not service().accepts_token(
            authorization.removeprefix("Bearer ")
        ):
            raise HTTPException(401, "owned sandbox token required")
        if method not in ("GET", "HEAD"):
            if not path.startswith(prefix + s.sid + "/"):
                raise HTTPException(
                    403, "global sandbox writes are owned by the dashboard service"
                )
            if not service().accepts_token(authorization.removeprefix("Bearer ")):
                raise HTTPException(401, "sandbox token required")
        return s

    async def proxy(request, path):
        s = check_scope(path, request.method, request.headers.get("authorization", ""))
        headers = {
            k: request.headers[k]
            for k in (
                "authorization",
                "range",
                "if-none-match",
                "content-type",
                "idempotency-key",
            )
            if k in request.headers
        }
        # Credentials supplied by the native sandbox client are retained, never sent to another origin.
        if "authorization" not in headers and path.startswith(
            ("api/worlds/", "worlds/")
        ):
            headers["authorization"] = "Bearer " + s.token
        url = settings.drone4d.base_url + "/" + path
        try:
            if path.startswith(f"api/sandbox/v1/sessions/{s.sid}"):
                r = await service().client.call(
                    service().client.native_request,
                    request.method,
                    "/" + path,
                    params=dict(request.query_params),
                    headers=headers,
                    content=await request.body(),
                )
            else:
                async with httpx.AsyncClient(
                    timeout=35, trust_env=False, follow_redirects=False
                ) as client:
                    r = await client.request(
                        request.method,
                        url,
                        params=request.query_params,
                        headers=headers,
                        content=await request.body(),
                    )
        except httpx.HTTPError as e:
            raise HTTPException(502, "Drone4D proxy " + type(e).__name__) from None
        if (
            path == f"api/sandbox/v1/sessions/{s.sid}/keepalive"
            and r.status_code == 200
        ):
            try:
                service().remember_token(r.json().get("token"))
            except ValueError:
                pass
        out = {
            k: v
            for k, v in r.headers.items()
            if k
            in (
                "content-type",
                "content-range",
                "accept-ranges",
                "etag",
                "cache-control",
                "last-modified",
            )
        }
        return Response(r.content, status_code=r.status_code, headers=out)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "HEAD", "POST", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def native_api(request: Request, path: str):
        return await proxy(request, "api/" + path)

    @app.api_route(
        "/worlds/{path:path}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def native_worlds(request: Request, path: str):
        return await proxy(request, "worlds/" + path)

    @app.websocket("/api/sandbox/v1/sessions/{sid}/rt")
    async def native_rt(ws: WebSocket, sid: str):
        import websockets

        s = service().client.session
        if not s or s.sid != sid:
            await ws.close(4403)
            return
        origin = ws.headers.get("origin")
        if origin and urlsplit(origin).netloc != ws.headers.get("host"):
            await ws.close(4403)
            return
        url = (
            settings.drone4d.base_url.replace("https://", "wss://").replace(
                "http://", "ws://"
            )
            + f"/api/sandbox/v1/sessions/{sid}/rt"
        )
        if ws.url.query:
            url += "?" + ws.url.query
        protocols = ws.scope.get("subprotocols", [])
        bearer = next((p[7:] for p in protocols if p.startswith("bearer.")), "")
        if "awr.rt.v1" not in protocols or not service().accepts_token(bearer):
            await ws.close(4401)
            return
        await ws.accept(subprotocol="awr.rt.v1")
        try:
            async with websockets.connect(
                url,
                subprotocols=["awr.rt.v1", "bearer." + s.token],
                origin=settings.drone4d.base_url,
                max_size=16 * 1024 * 1024,
                proxy=None,
            ) as remote:

                async def outbound():
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        await remote.send(
                            msg.get("bytes")
                            if msg.get("bytes") is not None
                            else msg.get("text", "")
                        )

                async def inbound():
                    async for msg in remote:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)

                tasks = [
                    asyncio.create_task(outbound()),
                    asyncio.create_task(inbound()),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            pass
        except Exception:
            try:
                await ws.close(1011, "Drone4D realtime connection unavailable")
            except RuntimeError:
                pass
