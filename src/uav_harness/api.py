from contextlib import asynccontextmanager, nullcontext
import asyncio
import hmac
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, field_validator
from .compiler import COMPILATION_DEADLINE_S, DEFAULT_DEEPSEEK_MODEL, DEEPSEEK_ENDPOINT, DeepSeekCompiler, planning_context
from .contracts import Action, Contract, Plan
from .errors import HarnessError, Uncertain
from .runtime import Harness
from .brain.service import BrainService
from .brain.contracts import MissionRequest


class Intent(Contract):
    query: str = Field(min_length=1, max_length=10000)
    execute: bool = False


class DemoRequest(Intent):
    @field_validator('query')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('请输入搜索任务')
        return value.strip()


RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]


def create_app(settings, compiler=None, *, offline=False, planner=None):
    if offline:
        settings = settings.model_copy(deep=True, update={"allow_control": False})
    token = os.getenv("HARNESS_API_TOKEN")
    if settings.api_host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise HarnessError("HARNESS_API_TOKEN is required for a non-loopback HTTP bind")
    compiler = compiler or DeepSeekCompiler()

    @asynccontextmanager
    async def lifespan(app):
        # Preview must not open links or mutate the real controller's job journal.
        with TemporaryDirectory(prefix="uav-offline-") if offline else nullcontext() as folder:
            active = settings.model_copy(update={"state_dir": Path(folder)}) if offline else settings
            harness = Harness(active)
            app.state.harness = harness
            if not offline:
                await harness.open()
            app.state.brain=BrainService(harness,offline,planner)
            try:
                yield
            finally:
                await app.state.brain.close()
                await harness.close()

    def require_execution():
        if offline:
            raise HarnessError("offline preview only; execution disabled")

    app = FastAPI(title="UAV Harness", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request, call_next):
        public = request.url.path == "/" or request.url.path.startswith("/assets/")
        if token and not public and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer "+token):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(HarnessError)
    async def harness_error(request, exc):
        return JSONResponse({"error": str(exc), "outcome": "unknown" if isinstance(exc, Uncertain) else "rejected"}, status_code=409)

    @app.get("/health")
    async def health():
        states = app.state.harness.fleet()
        return {"service": "ready", "connected_vehicles": sum(s["connected"] for s in states), "qgc_required": False, "offline": offline}

    @app.get("/v1/vehicles")
    async def vehicles():
        return {"vehicles": app.state.harness.fleet()}

    @app.get("/v1/dashboard")
    async def dashboard():
        harness = app.state.harness
        kinds = {link.kind for link in settings.links}
        rows = harness.journal.db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 12").fetchall()
        return {"vehicles": harness.fleet(), "control_enabled": settings.allow_control,
                "planning_context": planning_context(harness),
                "mode": "offline" if offline else "simulator" if kinds == {"simulator"} else "mixed" if "simulator" in kinds else "hardware",
                "compiler": {"provider": "DeepSeek 官方", "endpoint": DEEPSEEK_ENDPOINT, "model": os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL), "reasoning_effort": os.getenv("DEEPSEEK_REASONING_EFFORT", "low"), "deadline_s": COMPILATION_DEADLINE_S, "configured": bool(os.getenv("DEEPSEEK_API_KEY"))},
                "jobs": [harness.journal.get(row[0], events=False) for row in rows]}

    @app.post("/v1/actions", status_code=202)
    async def action(body: Action, request_key: RequestKey):
        require_execution()
        return await app.state.harness.submit(Plan(actions=[body]), request_key)

    @app.post("/v1/plans", status_code=202)
    async def plan(body: Plan, request_key: RequestKey):
        require_execution()
        return await app.state.harness.submit(body, request_key)

    @app.post("/v1/plans/validate")
    async def validate_plan(body: Plan):
        app.state.harness.validate(body)
        return {"plan": body.model_dump(), "validated": True}

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str):
        return app.state.harness.journal.get(job_id)

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        require_execution()
        return await app.state.harness.cancel(job_id)

    @app.post("/v1/jobs/{job_id}/reconcile")
    async def reconcile(job_id: str):
        require_execution()
        return await app.state.harness.reconcile(job_id)

    @app.post("/v1/intents")
    async def intent(body: Intent, request_key: RequestKey):
        harness = app.state.harness
        if body.execute:
            require_execution()
            existing = harness.journal.db.execute("SELECT id FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                row = harness.journal.get(existing[0])
                if row["plan"]["query"] != body.query:
                    raise HarnessError("idempotency key reused with different intent")
                return {"executed": True, "job": row, "replayed": True}
        proposal = await compiler.compile(body.query, harness)
        if not body.execute:
            return {"executed": False, "plan": proposal.model_dump()}
        row = await harness.submit(proposal, request_key)
        return {"executed": True, "job": row}

    @app.post("/v1/intents/preview-stream")
    async def preview_stream(body: Intent, request_key: RequestKey):
        if body.execute:
            raise HarnessError("streaming endpoint previews only; execution disabled")
        async def produce(progress):
            plan = await compiler.compile(body.query, app.state.harness, on_progress=progress)
            return {'plan': plan.model_dump()}
        return proposal_stream(produce)

    @app.post('/v1/demo/preview-stream')
    async def preview_demo(body: DemoRequest):
        return JSONResponse({'error':'Scripted demo retired. Use /v1/brain/missions for VLM online control.'},status_code=410)

    @app.get('/v1/brain')
    async def brain_metadata():
        return app.state.brain.metadata()

    @app.post('/v1/brain/missions',status_code=202)
    async def start_mission(body: MissionRequest):
        return await app.state.brain.start(body)

    @app.get('/v1/brain/missions/{mid}')
    async def get_mission(mid: str):
        return app.state.brain.get(mid).snapshot()

    @app.post('/v1/brain/missions/{mid}/{action}')
    async def control_mission(mid: str,action: str):
        return await app.state.brain.control(mid,action)

    @app.get('/v1/brain/missions/{mid}/frames/{fid}')
    async def mission_image(mid: str,fid: str):
        obs=app.state.brain.get(mid).frames.get(fid)
        if not obs: raise HTTPException(404,'frame expired or unavailable')
        return Response(obs.image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    class PreviewRequest(Contract):
        vehicle_id: str = Field(min_length=1,max_length=80)

    @app.post('/v1/brain/preview')
    async def preview_observation(body: PreviewRequest):
        obs=await app.state.brain.preview(body.vehicle_id)
        data=obs.model_dump()
        active=app.state.brain.missions.get(app.state.brain.owners.get(body.vehicle_id))
        data['image_url']=(f'/v1/brain/missions/{active.mission_id}/frames/{obs.observation_id}' if active and obs.observation_id in active.frames
            else f'/v1/brain/preview/{body.vehicle_id}/image?observation_id={obs.observation_id}')
        return data

    @app.get('/v1/brain/preview/{vid}/image')
    async def preview_image(vid: str):
        obs=app.state.brain.preview_frames.get(vid)
        if not obs: raise HTTPException(404,'preview unavailable')
        return Response(obs.image,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    def proposal_stream(produce):

        async def events():
            updates = asyncio.Queue(maxsize=8)
            latest = {"phase": "connecting"}
            started = time.monotonic()

            def progress(event):
                latest.update(event)
                if updates.full():
                    updates.get_nowait()
                updates.put_nowait({"type": "status", **event})

            async def compile_plan():
                try:
                    return {"type": "done", "executed": False, **await produce(progress)}
                except HarnessError as exc:
                    return {"type": "error", "error": str(exc)}

            task = asyncio.create_task(compile_plan())
            try:
                yield 'data: '+json.dumps({"type": "status", **latest})+'\n\n'
                while not task.done():
                    try:
                        event = await asyncio.wait_for(updates.get(), 1)
                    except TimeoutError:
                        event = {"type": "status", **latest, "elapsed_s": round(time.monotonic()-started, 1)}
                    yield 'data: '+json.dumps(event, ensure_ascii=False)+'\n\n'
                yield 'data: '+json.dumps(await task, ensure_ascii=False)+'\n\n'
            finally:
                # A closed browser must close the provider request rather than leave an orphan generation.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    return app

