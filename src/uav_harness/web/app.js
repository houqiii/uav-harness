import { $, element, request, notice } from "./api.js";
import { initTelemetry, renderTelemetry } from "./telemetry.js";
import {
  renderGimbal,
  videoSettings,
  saveVideoSettings,
  refreshVideo,
} from "./gimbal.js";
import { initWorkspace, updateWorkspace, pollJob } from "./workspace.js";
let snapshot = null,
  connected = false,
  selected = "",
  polling = false,
  lastError = "";
initTelemetry();
initWorkspace();
function selectedVehicle() {
  return snapshot?.vehicles.find((v) => v.vehicle_id === selected);
}
function render() {
  const v = selectedVehicle();
  const offline = snapshot?.mode === "offline";
  renderTelemetry(v, connected, offline);
  renderGimbal(v, connected && !offline);
  if (offline) $("video-message").textContent = "离线推演 · 云台未接入";
  updateWorkspace(snapshot, connected);
  const state = $("link-state");
  state.className = "state" + (connected && !offline ? "" : " muted");
  const labels = {
    simulator: "模拟链路",
    hardware: "实机链路",
    mixed: "混合链路",
    offline: "离线推演",
  };
  state.replaceChildren(
    element("i"),
    document.createTextNode(connected ? labels[snapshot.mode] : "连接断开"),
  );
  $("control-mode").textContent =
    connected && offline
      ? "仅生成计划 · 未连接飞控 · 不发送指令"
      : connected
        ? `${snapshot.mode === "simulator" ? "协议模拟 · " : ""}${snapshot.control_enabled ? "控制已启用" : "只读观察"}`
        : "服务不可用";
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    snapshot = await request("/v1/dashboard");
    connected = true;
    const values = snapshot.vehicles.map((v) => v.vehicle_id);
    if (!values.includes(selected))
      selected =
        (snapshot.mode === "offline"
          ? values.find(
              (id) =>
                snapshot.vehicles.find((v) => v.vehicle_id === id).profile ===
                "flight",
            )
          : null) ||
        values.find(
          (id) =>
            snapshot.vehicles.find((v) => v.vehicle_id === id).profile ===
            "model_bench",
        ) ||
        values[0];
    const selector = $("vehicle-select");
    if (
      Array.from(selector.options)
        .map((o) => o.value)
        .join() !== values.join()
    )
      selector.replaceChildren(
        ...values.map((id) => {
          const option = element("option", "", id);
          option.value = id;
          return option;
        }),
      );
    selector.value = selected;
    render();
    if (lastError) {
      notice("");
      lastError = "";
    }
    try {
      await pollJob();
    } catch (error) {
      notice("任务状态查询失败：" + error.message);
    }
  } catch (error) {
    connected = false;
    render();
    if (error.message !== lastError) {
      notice(error.message);
      lastError = error.message;
    }
  } finally {
    polling = false;
    setTimeout(poll, 1000);
  }
}
$("vehicle-select").onchange = () => {
  selected = $("vehicle-select").value;
  render();
};
function openSettings() {
  $("api-token").value = sessionStorage.getItem("uav.apiToken") || "";
  const config = videoSettings(selected);
  $("video-url").value = config.url || "";
  $("video-kind").value = config.kind || "mjpeg";
  $("settings-error").textContent = "";
  const stream = selectedVehicle()?.video_streams?.[0];
  $("video-discovered").textContent = stream?.uri
    ? `设备宣告地址：${stream.uri}`
    : "";
  $("settings-dialog").showModal();
}
$("settings-button").onclick = openSettings;
$("video-settings-button").onclick = openSettings;
$("close-settings").onclick = () => $("settings-dialog").close();
$("settings-form").onsubmit = (event) => {
  event.preventDefault();
  try {
    if ($("video-url").value.trim() && !selected)
      throw new Error("请先连接服务并选择飞机，再设置视频。");
    if (selected)
      saveVideoSettings(
        selected,
        $("video-url").value.trim(),
        $("video-kind").value,
      );
    sessionStorage.setItem("uav.apiToken", $("api-token").value.trim());
    $("settings-dialog").close();
    refreshVideo();
    notice("设置已保存。", true);
    render();
  } catch (error) {
    $("settings-error").textContent = error.message;
  }
};
window.addEventListener("unhandledrejection", (event) => {
  notice("界面请求失败：" + (event.reason?.message || "未知错误"));
  event.preventDefault();
});
poll();
