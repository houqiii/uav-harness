import test from "node:test";
import assert from "node:assert/strict";
import { DemoMission } from "../src/uav_harness/web/demo-state.js";

function finish(mission) {
  for (let i = 0; i < 2000 && mission.running; i++) mission.tick(0.1);
}

test("scan collects twelve unique measured directions before confirming and stops at standoff", () => {
  const m = new DemoMission();
  m.start("found");
  finish(m);
  assert.equal(m.phase, "complete");
  assert.deepEqual(
    m.frames.map((f) => f.heading),
    Array.from({ length: 12 }, (_, i) => i * 30),
  );
  assert.ok(m.frames.every((f) => f.status !== "pending"));
  assert.equal(m.distance, 20);
  assert.equal(m.altitude, 50);
  assert.equal(m.speed, 0);
  assert.equal(m.frames.filter((f) => f.found).length, 1);
});

test("pause freezes motion, captures and analysis; reset removes prior results", () => {
  const m = new DemoMission();
  m.start("found");
  for (let i = 0; i < 40; i++) m.tick(0.2);
  m.pause();
  const before = JSON.stringify(m);
  m.tick(50);
  assert.equal(JSON.stringify(m), before);
  m.resume();
  finish(m);
  assert.equal(m.phase, "complete");
  m.reset();
  assert.equal(m.phase, "idle");
  assert.equal(m.frames.length, 0);
  assert.equal(m.distance, null);
  m.start("missing");
  finish(m);
  assert.equal(m.phase, "not_found");
  assert.equal(m.distance, null);
});

test("target loss or vision timeout holds position without finishing approach", () => {
  for (const scenario of ["lost", "timeout"]) {
    const m = new DemoMission();
    m.start(scenario);
    finish(m);
    assert.equal(m.phase, "holding");
    assert.equal(m.speed, 0);
    assert.ok(m.distance > 20);
    const before = JSON.stringify(m);
    m.tick(100);
    assert.equal(JSON.stringify(m), before);
  }
});

test("invalid scenarios and duplicate starts cannot mutate a running mission", () => {
  const m = new DemoMission();
  assert.throws(() => m.start("unknown"));
  m.start("found");
  m.tick(1);
  const before = JSON.stringify(m);
  assert.throws(() => m.start("found"));
  assert.equal(JSON.stringify(m), before);
});
