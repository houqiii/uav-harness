from __future__ import annotations

import asyncio
from pathlib import Path
from .adapters import make_adapter
from .contracts import Plan
from .errors import HarnessError, Uncertain
from .journal import Journal
from .simulator import ProtocolSimulator
from .transport import MAVLinkTransport
from .vehicle import VehicleSession


class Harness:
    def __init__(self, settings):
        self.settings = settings
        Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
        self.journal = Journal(Path(settings.state_dir)/"jobs.sqlite3")
        self.transports, self.sessions, self.adapters, self.simulators = {}, {}, {}, {}
        self.jobs, self.cancel_flags, self.leases, self.current = {}, {}, {}, {}
        self.lock = asyncio.Lock()
        self.closed = False
        for link in settings.links:
            targets = {(v.system_id, v.component_id): 3 if v.backend == "ardupilot" else 12
                       for v in settings.vehicles if v.link == link.name}
            self.transports[link.name] = MAVLinkTransport(link, settings, targets, self.protocol_event)
        for config in settings.vehicles:
            session = VehicleSession(config, self.transports[config.link], settings)
            self.sessions[config.vehicle_id] = session
            self.adapters[config.vehicle_id] = make_adapter(session)

    def protocol_event(self, event):
        identity = event.get("target") or event.get("source")
        job_id = None
        if identity:
            for vid, s in self.sessions.items():
                if s.config.link == event["link"] and list(identity) == [s.config.system_id, s.config.component_id]:
                    job_id = self.current.get(vid)
        self.journal.event(job_id, event)

    async def open(self):
        try:
            for link in self.transports.values():
                await link.open()
                if link.config.kind == "simulator":
                    config = next(v for v in self.settings.vehicles if v.link == link.config.name)
                    simulator = ProtocolSimulator(config, link.sock.getsockname())
                    self.simulators[config.vehicle_id] = simulator
                    link.peer = simulator.sock.getsockname()
                    await simulator.open()
            await asyncio.gather(*(s.initialize() for s in self.sessions.values()))
        except BaseException:
            await self.close()
            raise

    def fleet(self):
        return [s.snapshot() for s in self.sessions.values()]

    def validate(self, plan):
        for action in plan.actions:
            if action.vehicle_id not in self.adapters:
                raise HarnessError("unknown vehicle")
            self.adapters[action.vehicle_id].validate(action)

    async def submit(self, plan, request_key):
        self.validate(plan)
        async with self.lock:
            # Replay lookup precedes admission, so repeat HTTP requests don't compete with their own lease.
            existing = self.journal.db.execute("SELECT id FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                job_id, _ = self.journal.create(request_key, plan.model_dump())
                return self.journal.get(job_id) | {"replayed": True}
            if not self.settings.allow_control:
                raise HarnessError("service is configured for observation only")
            vehicles = {a.vehicle_id for a in plan.actions}
            if vehicles & self.leases.keys():
                raise HarnessError("vehicle owned by an active job")
            if vehicles & self.journal.unknown_vehicles():
                raise Uncertain("unresolved prior outcome; reconcile before new actions")
            for vid in vehicles:
                self.sessions[vid].require(control=True)
            job_id, _ = self.journal.create(request_key, plan.model_dump())
            for vid in vehicles:
                self.leases[vid] = job_id
            flag = asyncio.Event()
            self.cancel_flags[job_id] = flag
            self.jobs[job_id] = asyncio.create_task(self.run(job_id, plan, flag))
            return self.journal.get(job_id) | {"replayed": False}

    async def run_action(self, job_id, action, flag):
        self.current[action.vehicle_id] = job_id
        self.journal.update(job_id, action_id=action.action_id, result={"status": "running"})
        task = asyncio.create_task(self.adapters[action.vehicle_id].execute(action))
        cancel = asyncio.create_task(flag.wait())
        try:
            done, _ = await asyncio.wait([task, cancel], timeout=action.timeout_s, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                result = await task
                self.journal.update(job_id, action_id=action.action_id, result={"status": "succeeded", **result})
                return "succeeded"
            task.cancel()
            # Bench actions finish their controller-timed stop / land cleanup before release.
            await asyncio.gather(task, return_exceptions=True)
            raise Uncertain("action cancelled" if flag.is_set() else "action completion timeout")
        except HarnessError as exc:
            status = "unknown" if isinstance(exc, Uncertain) else "failed"
            self.journal.update(job_id, action_id=action.action_id, result={"status": status, "error": str(exc)})
            return status
        except Exception as exc:
            self.journal.update(job_id, action_id=action.action_id, result={"status": "unknown", "error": f"executor exception: {type(exc).__name__}"})
            return "unknown"
        finally:
            cancel.cancel()
            await asyncio.gather(cancel, return_exceptions=True)
            self.current.pop(action.vehicle_id, None)

    async def run(self, job_id, plan, flag):
        self.journal.update(job_id, "running")
        pending = {a.action_id: a for a in plan.actions}
        done = set()
        outcome = "succeeded"
        try:
            while pending and not flag.is_set():
                ready = [a for a in pending.values() if set(a.depends_on) <= done]
                results = await asyncio.gather(*(self.run_action(job_id, a, flag) for a in ready))
                for a, status in zip(ready, results):
                    pending.pop(a.action_id)
                    if status == "succeeded":
                        done.add(a.action_id)
                    elif status == "unknown":
                        outcome = "unknown"
                    elif outcome != "unknown":
                        outcome = "failed"
                if outcome != "succeeded":
                    break
            if flag.is_set() and outcome == "succeeded":
                outcome = "cancelled"
            if outcome == "succeeded":
                # Hand completed streamed movement to native hold, so no orphan stream
                # survives release of its job lease or a subsequent CLI exit.
                for vid in {a.vehicle_id for a in plan.actions}:
                    if self.sessions[vid].target:
                        try:
                            await self.adapters[vid].pause()
                        except HarnessError as exc:
                            self.journal.event(job_id, {"kind": "handoff_unconfirmed", "vehicle_id": vid, "error": str(exc)})
                            outcome = "unknown"
            if outcome != "succeeded":
                for vid in {a.vehicle_id for a in plan.actions}:
                    try:
                        await self.adapters[vid].pause()
                    except HarnessError as exc:
                        self.journal.event(job_id, {"kind": "pause_unconfirmed", "vehicle_id": vid, "error": str(exc)})
                        outcome = "unknown"
            for action_id in pending:
                self.journal.update(job_id, action_id=action_id, result={"status": "not_executed"})
            self.journal.update(job_id, outcome)
        finally:
            for vid in {a.vehicle_id for a in plan.actions}:
                self.leases.pop(vid, None)

    async def cancel(self, job_id):
        row = self.journal.get(job_id)
        if row["status"] in ("queued", "running", "cancelling") and job_id in self.cancel_flags:
            self.journal.update(job_id, "cancelling")
            self.cancel_flags[job_id].set()
        return self.journal.get(job_id)

    async def reconcile(self, job_id):
        async with self.lock:
            row = self.journal.get(job_id)
            if row["status"] != "unknown":
                raise HarnessError("only unknown jobs need reconciliation")
            vehicles = {a["vehicle_id"] for a in row["plan"]["actions"]}
            for vid in vehicles:
                if vid in self.leases:
                    raise HarnessError("vehicle still leased")
                self.sessions[vid].require()
                if self.sessions[vid].armed() or not self.sessions[vid].grounded():
                    raise HarnessError("fresh ground/disarmed state required for reconciliation")
            self.journal.event(job_id, {"kind": "reconciled_on_ground", "states": [self.sessions[v].snapshot() for v in vehicles]})
            self.journal.update(job_id, "reconciled")
            # Poisoned ACK IDs remain blocked on this connection. Restart before reusing one.
            return self.journal.get(job_id)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for flag in self.cancel_flags.values():
            flag.set()
        if self.jobs:
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        for s in self.sessions.values():
            await s.stop_stream()
        for simulator in self.simulators.values():
            await simulator.close()
        for transport in self.transports.values():
            await transport.close()
        self.journal.close()
