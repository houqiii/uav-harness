import { $, request } from "./api.js";
let state = null,
  signature = "",
  busy = false,
  frameSession = null;
const inSandbox = () => !["#hardware", "#console"].includes(location.hash);
function render() {
  if (!state) return;
  const ready = state.state === "ready";
  $("sandbox-panel").hidden = !inSandbox() && !ready;
  // Preserve the same renderer when switching workspaces; never swap its session for the hardware page.
  $("sandbox-panel").classList.toggle(
    "offscreen-renderer",
    !inSandbox() && ready,
  );
  $("sandbox-status").textContent =
    `${state.endpoint} · ${state.remote_build?.version || "v0.2.0"} · ${{ disconnected: "未连接", connecting: "正在创建独立会话", ready: "会话就绪", failed: "连接失败" }[state.state] || state.state}`;
  $("sandbox-connect").textContent =
    state.sid && state.state !== "ready" ? "重新接入已有会话" : "创建独立沙盒";
  $("sandbox-connect").disabled =
    busy || !state.enabled || ["ready", "connecting"].includes(state.state);
  $("sandbox-disconnect").disabled =
    busy || !(state.sid || state.state === "connecting");
  $("sandbox-error").hidden = !state.error && state.enabled && state.web_ready;
  $("sandbox-error").textContent =
    state.error ||
    (!state.enabled
      ? "使用 --sandbox 启动服务以启用在线沙盒。"
      : !state.web_ready
        ? "先运行 .venv/bin/python scripts/prepare-drone4d.py 构建原生视图。"
        : "");
  $("sandbox-session").textContent = state.sid
    ? `${state.sid} · ${state.world_id} · ${state.t_sim_ns ? (Number(state.t_sim_ns) / 1e9).toFixed(1) + " s" : "等待仿真时钟"}`
    : "尚未分配会话";
  $("sandbox-renderer").textContent = state.admission_wait_until
    ? `公开服务准入限流，遵照 Retry-After 等待 ${Math.max(0, Math.ceil(state.admission_wait_until - Date.now() / 1000))} s`
    : state.renderer_ready
      ? "原生云台渲染器在线"
      : state.renderer?.error || "等待原生世界与云台载入";
  $("sandbox-world").hidden = !ready;
  $("sandbox-viewport").hidden = !ready;
  $("sandbox-view-empty").hidden = ready;
  if (ready && frameSession !== state.sid) {
    frameSession = state.sid;
    $("sandbox-world").src =
      `/world/${encodeURIComponent(state.world_id)}?harness_bridge=1&chrome=0`;
  } else if (!ready && frameSession) {
    $("sandbox-world").src = "about:blank";
    frameSession = null;
  }
  $("sandbox-native-link").href =
    `/world/${encodeURIComponent(state.world_id)}?harness_bridge=1&chrome=0`;
  window.dispatchEvent(new CustomEvent("drone4d-state", { detail: state }));
  const next = JSON.stringify([
    state.state,
    state.renderer_ready,
    state.vehicles.map((v) => v.vehicle_id),
  ]);
  if (next !== signature) {
    signature = next;
    window.dispatchEvent(new Event("drone4d-changed"));
  }
}
async function poll() {
  try {
    state = await request("/v1/sandbox");
    render();
  } catch (e) {
    $("sandbox-error").hidden = false;
    $("sandbox-error").textContent = e.message;
  } finally {
    setTimeout(poll, 1500);
  }
}
export function initSandbox() {
  new ResizeObserver(([entry]) => {
    $("sandbox-viewport").style.setProperty(
      "--sandbox-render-scale",
      String(entry.contentRect.width / 1280),
    );
  }).observe($("sandbox-viewport"));
  $("sandbox-connect").onclick = async () => {
    busy = true;
    render();
    try {
      state = await request("/v1/sandbox/connect", { body: {} });
    } catch (e) {
      $("sandbox-error").hidden = false;
      $("sandbox-error").textContent = e.message;
    } finally {
      busy = false;
      render();
    }
  };
  $("sandbox-disconnect").onclick = async () => {
    busy = true;
    render();
    try {
      state = await request("/v1/sandbox/disconnect", {
        body: {},
        timeout: 150000,
      });
    } catch (e) {
      $("sandbox-error").hidden = false;
      $("sandbox-error").textContent = e.message;
    } finally {
      busy = false;
      render();
    }
  };
  window.addEventListener("hashchange", render);
  poll();
}
