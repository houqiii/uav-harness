from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from .compiler import DeepSeekCompiler
from .contracts import Action, Contract, Plan
from .errors import HarnessError, Uncertain
from .runtime import Harness


class Intent(Contract):
    query: str = Field(min_length=1, max_length=10000)
    execute: bool = False


RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]


def create_app(settings, compiler=None):
    token = os.getenv("HARNESS_API_TOKEN")
    if settings.api_host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise HarnessError("HARNESS_API_TOKEN is required for a non-loopback HTTP bind")
    compiler = compiler or DeepSeekCompiler()

    @asynccontextmanager
    async def lifespan(app):
        harness = Harness(settings)
        app.state.harness = harness
        await harness.open()
        try:
            yield
        finally:
            await harness.close()

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
        return {"service": "ready", "connected_vehicles": sum(s["connected"] for s in states), "qgc_required": False}

    @app.get("/v1/vehicles")
    async def vehicles():
        return {"vehicles": app.state.harness.fleet()}

    @app.get("/v1/dashboard")
    async def dashboard():
        harness = app.state.harness
        kinds = {link.kind for link in settings.links}
        rows = harness.journal.db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 12").fetchall()
        return {"vehicles": harness.fleet(), "control_enabled": settings.allow_control,
                "mode": "simulator" if kinds == {"simulator"} else "mixed" if "simulator" in kinds else "hardware",
                "compiler": {"provider": "DeepSeek", "configured": bool(os.getenv("DEEPSEEK_API_KEY"))},
                "jobs": [harness.journal.get(row[0], events=False) for row in rows]}

    @app.post("/v1/actions", status_code=202)
    async def action(body: Action, request_key: RequestKey):
        return await app.state.harness.submit(Plan(actions=[body]), request_key)

    @app.post("/v1/plans", status_code=202)
    async def plan(body: Plan, request_key: RequestKey):
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
        return await app.state.harness.cancel(job_id)

    @app.post("/v1/jobs/{job_id}/reconcile")
    async def reconcile(job_id: str):
        return await app.state.harness.reconcile(job_id)

    @app.post("/v1/intents")
    async def intent(body: Intent, request_key: RequestKey):
        harness = app.state.harness
        if body.execute:
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

    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    return app

