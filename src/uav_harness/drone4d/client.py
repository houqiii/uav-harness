"""Load the upstream single-file SDK, keeping its queue/renewal/admission semantics."""

import asyncio
import hashlib
import importlib.util
import json
import os
import time
import threading
import httpx
from pathlib import Path
from ..errors import HarnessError


class DroneClient:
    def __init__(self, config, state_dir):
        self.config = config
        self.state_file = Path(state_dir) / "drone4d-session.json"
        self.sdk = None
        self.sandbox = None
        self.session = None
        self.closed = False
        self.recovered = False
        self.admission_wait_until = None
        self.cancel_create = threading.Event()
        self.auth_lock = threading.RLock()

    async def call(self, fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            code = getattr(e, "code", None)
            if code is not None:
                raise HarnessError(
                    f"Drone4D {code} {getattr(e, 'name', '')}: {getattr(e, 'message', '')}"
                ) from None
            raise HarnessError(
                f"Drone4D SDK {type(e).__name__}: {str(e)[:180]}"
            ) from None

    def _connect(self):
        path = self.config.source_dir / "tools/sdk/awr_sandbox.py"
        if not path.is_file():
            raise HarnessError(
                "Drone4D SDK 尚未安装；先运行 scripts/prepare-drone4d.py"
            )
        spec = importlib.util.spec_from_file_location("uav_drone4d_sdk", path)
        self.sdk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sdk)
        self.sandbox = self.sdk.Sandbox(
            self.config.base_url,
            timeout_s=30,
            auto_keepalive=False,
            principal_hint=hashlib.sha256(
                str(self.state_file.resolve()).encode()
            ).hexdigest()[:32],
        )
        # Public admission binds proof-of-work to the source address. The SDK's
        # default environment proxy can rotate egress between challenge and reply.
        previous = self.sandbox.http.client
        self.sandbox.http.client = httpx.Client(
            base_url=self.config.base_url,
            timeout=30,
            trust_env=False,
            headers={
                "accept": "application/json",
                "user-agent": f"awr-sandbox-sdk/{self.sdk.__version__}",
            },
        )
        previous.close()
        upstream_request = self.sandbox.http.request

        def request(method, path, **kwargs):
            # PoW is single use. Retrying a rate-limited creation with the same
            # accepted nonce produces another challenge instead of waiting.
            if method == "POST" and path == "/api/sandbox/v1/sessions":
                kwargs["retries"] = 0
            with self.auth_lock:
                if self.session and path.startswith(self.session.path()):
                    kwargs["token"] = self.session.token
                response = upstream_request(method, path, **kwargs)
                self.adopt_renewal(path, response)
                return response

        self.sandbox.http.request = request
        if self.state_file.is_file():
            saved = json.loads(self.state_file.read_text())
            if saved.get("base_url") != self.config.base_url:
                raise HarnessError(
                    "Persisted Drone4D session belongs to another endpoint; disconnect it before switching"
                )
            try:
                self.session = self.sandbox.attach(saved["sid"], saved["token"])
                self.recovered = True
            except self.sdk.AwrError as exc:
                if exc.status not in (401, 404, 410):
                    raise
                self.state_file.unlink(missing_ok=True)
        return self.sandbox.meta()

    async def connect(self):
        return await self.call(self._connect)

    def adopt_renewal(self, path, response):
        if (
            self.session
            and path == self.session.path("keepalive")
            and response.status_code == 200
        ):
            data = response.json()
            if data.get("token"):
                self.session.token = data["token"]
            if isinstance(data.get("session"), dict):
                self.session.info = data["session"]
            self.persist()

    def native_request(self, method, path, *, headers, params, content):
        # The SDK and renderer share one rotating session credential. Serialize
        # renewal with REST dispatch so no command races a revoked old token.
        with self.auth_lock:
            headers = {**headers, "authorization": "Bearer " + self.session.token}
            response = self.sandbox.http.client.request(
                method, path, headers=headers, params=params, content=content
            )
            self.adopt_renewal(path, response)
            return response

    def renew_if_due(self):
        with self.auth_lock:
            expiry = self.session.expires_unix_ns / 1e9
            if expiry and expiry - time.time() <= 300:
                self.session.keepalive()

    def persist(self):
        with self.auth_lock:
            self._persist()

    def _persist(self):
        if not self.session:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "base_url": self.config.base_url,
                "sid": self.session.sid,
                "token": self.session.token,
                "info": self.session.info,
            }
        )
        pending = self.state_file.with_suffix(".tmp")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
        os.replace(pending, self.state_file)

    def _create(self, snapshot):
        deadline = time.monotonic() + self.config.admission_timeout_s
        force_new = False
        while not self.cancel_create.is_set():
            try:
                return self.sandbox.create(
                    self.config.world_id,
                    "t6_manual",
                    snapshot=snapshot,
                    queue=False,
                    seed=17,
                    wait_ready=False,
                    force_new=force_new,
                )
            except self.sdk.AwrError as exc:
                if (
                    getattr(exc, "code", None) == 409
                    and exc.name == "SANDBOX_EXISTS"
                    and not force_new
                ):
                    # Admission hint is not a credential. A lost/expired token
                    # cannot recover that session; use the documented second slot.
                    force_new = True
                    continue
                if exc.status != 429:
                    raise
                delay = max(1, exc.retry_after_s or 10)
                if time.monotonic() + delay > deadline:
                    raise
                self.admission_wait_until = time.time() + delay
                self.cancel_create.wait(delay)
                self.admission_wait_until = None
        raise HarnessError("Drone4D session creation cancelled")

    async def create(self, snapshot=None):
        if not self.session:
            self.session = await self.call(self._create, snapshot)
            await self.call(self.persist)
        await self.call(self.session.wait_ready)
        await self.call(self.persist)
        return self.session

    async def close(self):
        self.closed = True
        owned_sid = self.session.sid if self.session else None
        if self.session:
            await self.call(self.session.close)
            self.session = None
        if self.sandbox:
            await self.call(self.sandbox.close)
            self.sandbox = None
        if owned_sid and self.state_file.is_file():
            saved = json.loads(self.state_file.read_text())
            if (
                saved.get("sid") == owned_sid
                and saved.get("base_url") == self.config.base_url
            ):
                self.state_file.unlink(missing_ok=True)
