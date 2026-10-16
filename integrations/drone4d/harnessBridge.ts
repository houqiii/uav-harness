/** UAV Harness extension for Drone4D v0.2.0. Uses native rendering; never exports target truth to the planner. */
import {
  BoxGeometry,
  CylinderGeometry,
  Group,
  Mesh,
  MeshBasicMaterial,
  SphereGeometry,
  Vector3,
} from "three";
import { register, type FrameCtx } from "@/engine/loop";
import { camera, sensorView, viewport, layers } from "@/viewport/facade";
import { vp } from "@/viewport/session";
import { selection } from "@/stores/selection";
import { resumeSandbox, STORAGE_KEY } from "@/stores/sandboxSession";
import { targetsModel } from "@/engine/targets";
import { targetsLayer } from "@/viewport/layers/targets";
import { boot } from "@/app/boot/BootController";

interface CaptureRequest {
  request_id: string;
  vehicle_id: string;
  requested_at: number;
}
interface Config {
  sid: string;
  token: string;
  expires_unix_ns: string;
  world: string;
  template: string | null;
  styles: Record<string, { hat: string }>;
  vehicle_ids: string[];
}
const requests = new Map<string, CaptureRequest>();
const figures = new Map<string, Group>();
let config: Config;
const observerOnly =
  new URLSearchParams(location.search).get("harness_view") === "third";
let busy = false;
let exporting = false;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
const token = () => sessionStorage.getItem("uav.apiToken") || "";
async function api(path: string, body?: unknown) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    cache: "no-store",
    headers: {
      ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Harness bridge HTTP ${response.status}`);
  return response.json();
}
function person(hat: string): Group {
  const group = new Group();
  const skin = new MeshBasicMaterial({ color: "#ca9c7c" });
  const shirt = new MeshBasicMaterial({ color: "#e3e6db" });
  const dark = new MeshBasicMaterial({ color: "#253246" });
  const cap = new MeshBasicMaterial({
    color: hat === "red" ? "#ef2020" : "#205ee8",
  });
  function box(
    sx: number,
    sy: number,
    sz: number,
    x: number,
    z: number,
    material: MeshBasicMaterial,
  ) {
    const m = new Mesh(new BoxGeometry(sx, sy, sz), material);
    m.position.set(x, 0, z);
    group.add(m);
  }
  box(0.12, 0.16, 0.78, -0.1, 0.42, dark);
  box(0.12, 0.16, 0.78, 0.1, 0.42, dark);
  box(0.39, 0.23, 0.57, 0, 1.06, shirt);
  box(0.1, 0.13, 0.55, -0.25, 1.07, shirt);
  box(0.1, 0.13, 0.55, 0.25, 1.07, shirt);
  const head = new Mesh(new SphereGeometry(0.13, 16, 12), skin);
  head.position.z = 1.53;
  group.add(head);
  for (const [r, h, z] of [
    [0.145, 0.1, 1.66],
    [0.19, 0.025, 1.61],
  ]) {
    const m = new Mesh(new CylinderGeometry(r, r, h, 20), cap);
    m.rotation.x = Math.PI / 2;
    m.position.z = z;
    group.add(m);
  }
  group.name = "harness-scene-person";
  return group;
}
function updateFigures() {
  // These transforms are scene construction inputs only. Capture responses contain RGB and camera pose, never IDs/positions.
  for (const [id, style] of Object.entries(config.styles)) {
    let g = figures.get(id);
    if (!g) {
      g = person(style.hat);
      figures.set(id, g);
      vp.worldRoot.add(g);
    }
    const row = targetsModel.rowOf(id);
    g.visible = row >= 0;
    if (row >= 0) {
      const p = targetsModel.rows.pos,
        k = row * 3;
      g.position.set(p[k], p[k + 1], p[k + 2]);
      g.rotation.z = targetsModel.rows.heading[row];
    }
  }
  // Avoid simulator identification boxes and sensitivity volumes in the VLM image.
  targetsLayer.overlay?.setVisible(false);
}
function nedFromThree(v: Vector3): number[] {
  return [-v.z, v.x, -v.y];
}
function ready() {
  return (
    boot.state === "REVEALED" &&
    vp.world &&
    vp.camera &&
    vp.be?.state === "READY"
  );
}
async function poll() {
  if (busy) return;
  busy = true;
  try {
    if (observerOnly) {
      if (ready()) {
        selection.select([config.vehicle_ids[0]]);
        if (camera.mode !== "third") camera.setMode("third");
        if (!camera.followLock) camera.setFollowLock(true);
        layers.setVisible("frustums", false);
        viewport.requestFrame();
      }
      return;
    }
    const result = await api(
      `/v1/sandbox/render/requests?sid=${encodeURIComponent(config.sid)}`,
    );
    for (const request of result.requests as CaptureRequest[]) {
      if (!requests.has(request.request_id))
        requests.set(request.request_id, request);
    }
    await api("/v1/sandbox/render/status", {
      sid: config.sid,
      ready: Boolean(ready()),
      world_id: vp.worldId,
      camera_mode: camera.mode,
    });
    if (ready()) {
      const request = requests.values().next().value;
      selection.select([request?.vehicle_id || config.vehicle_ids[0]]);
      if (camera.mode !== "fpv") sensorView.set("eo", { enterFpv: true });
      viewport.requestFrame();
    }
  } catch (e) {
    console.warn(
      "Drone4D capture bridge waiting",
      e instanceof Error ? e.message : String(e),
    );
  } finally {
    busy = false;
    pollTimer = setTimeout(poll, 350);
  }
}
async function exportFrame(ctx: FrameCtx) {
  if (
    observerOnly ||
    exporting ||
    !requests.size ||
    !ready() ||
    camera.mode !== "fpv"
  )
    return;
  const request = requests.values().next().value!;
  if (!config.vehicle_ids.includes(request.vehicle_id)) return;
  exporting = true;
  requests.delete(request.request_id);
  try {
    const cam = vp.camera!;
    cam.updateMatrixWorld();
    const be = vp.be!;
    const canvas = be.renderer.domElement;
    const output = document.createElement("canvas");
    // Preserve native 1080p detail; never upscale a lower-resolution framebuffer.
    const scale = Math.min(1, 1920 / canvas.width);
    output.width = Math.round(canvas.width * scale);
    output.height = Math.round(canvas.height * scale);
    const gc = output.getContext("2d")!;
    gc.drawImage(canvas, 0, 0, output.width, output.height);
    const w = output.width,
      h = output.height;
    const m = cam.matrixWorld.elements,
      p = cam.projectionMatrix.elements.slice();
    const capturedAt = Date.now() / 1000;
    const simulationNs = Math.round(ctx.tFocusS * 1e9);
    const forward = nedFromThree(new Vector3(-m[8], -m[9], -m[10]));
    const right = nedFromThree(new Vector3(m[0], m[1], m[2]));
    const down = nedFromThree(new Vector3(-m[4], -m[5], -m[6]));
    const yaw = Math.atan2(forward[1], forward[0]),
      pitch = Math.asin(Math.max(-1, Math.min(1, -forward[2])));
    const roll = Math.atan2(right[2], down[2]);
    const position = nedFromThree(new Vector3(m[12], m[13], m[14]));
    const ground = viewport.groundZ(m[12], -m[14]);
    const blob = await new Promise<Blob>((resolve, reject) =>
      output.toBlob(
        (b) => (b ? resolve(b) : reject(new Error("empty framebuffer"))),
        "image/jpeg",
        0.92,
      ),
    );
    const image = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    await api("/v1/sandbox/render/frame", {
      ...request,
      sid: config.sid,
      captured_at: capturedAt,
      t_sim_ns: simulationNs,
      image_b64: image,
      camera: {
        width: w,
        height: h,
        fx: (Math.abs(p[0]) * w) / 2,
        fy: (Math.abs(p[5]) * h) / 2,
        cx: ((1 - p[8]) * w) / 2,
        cy: ((1 + p[9]) * h) / 2,
        yaw_deg: ((yaw * 180) / Math.PI + 360) % 360,
        pitch_deg: (pitch * 180) / Math.PI,
        roll_deg: (roll * 180) / Math.PI,
        position_ned: position,
        ground_down_m: Number.isFinite(ground) ? -ground : null,
        ground_uncertainty_m: 2,
        source: "drone4d_native_fpv",
        timing: "simulated_capture",
      },
    });
  } catch (e) {
    await api("/v1/sandbox/render/frame", {
      ...request,
      sid: config.sid,
      error: e instanceof Error ? e.message : String(e),
    }).catch(() => {});
  } finally {
    exporting = false;
  }
}
export async function installHarnessBridge() {
  if (new URLSearchParams(location.search).get("harness_bridge") !== "1")
    return;
  config = await api("/v1/sandbox/bridge");
  history.replaceState(
    null,
    "",
    `/sandbox/${config.sid}?harness_bridge=1&chrome=0${observerOnly ? "&harness_view=third" : ""}`,
  );
  sessionStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      sid: config.sid,
      token: config.token,
      expires_unix_ns: config.expires_unix_ns,
      world: config.world,
      template: config.template,
    }),
  );
  if (!(await resumeSandbox()))
    throw new Error("Cannot join dashboard-owned sandbox session");
  register("world", "harness.scene", updateFigures, { order: 1000 });
  register(
    "overlay",
    "harness.capture",
    (ctx) => {
      void exportFrame({ ...ctx });
    },
    { order: 1000 },
  );
  void poll();
  addEventListener(
    "pagehide",
    () => {
      if (pollTimer) clearTimeout(pollTimer);
    },
    { once: true },
  );
}
