import { $, element } from "./api.js";
const settingKey = (id) => `uav.video.${id}`;
export function videoSettings(id) {
  try {
    return JSON.parse(localStorage.getItem(settingKey(id))) || {};
  } catch {
    return {};
  }
}
export function saveVideoSettings(id, url, kind) {
  if (url) {
    const parsed = new URL(url);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    )
      throw new Error("请填写不含账号密码的 HTTP(S) 视频地址；RTSP 需先转换。");
  }
  localStorage.setItem(settingKey(id), JSON.stringify({ url, kind }));
}
let currentSource = "",
  currentVehicle = "",
  devices = [],
  selected = "",
  media = null,
  mediaTimer = null;
function emptyVideo(text) {
  $("video-empty").hidden = false;
  $("video-message").textContent = text;
}
function resetMedia() {
  clearTimeout(mediaTimer);
  if (media?.tagName === "VIDEO") {
    media.pause();
    media.removeAttribute("src");
    media.load();
  }
  media?.remove();
  media?.removeAttribute("src");
  media = null;
}
export function refreshVideo() {
  currentSource = "";
  resetMedia();
}
export function renderGimbal(vehicle, online) {
  const changed = currentVehicle !== vehicle?.vehicle_id;
  currentVehicle = vehicle?.vehicle_id || "";
  devices = online ? vehicle?.gimbals || [] : [];
  const select = $("gimbal-select"),
    keys = devices.map((d) => `${d.component_id}:${d.device_id}`);
  if (changed || !keys.includes(selected)) selected = keys[0] || "";
  if (
    Array.from(select.options)
      .map((o) => o.value)
      .join() !== keys.join()
  )
    select.replaceChildren(
      ...devices.map((d) => {
        const option = element(
          "option",
          "",
          `组件 ${d.component_id} / ${d.device_id}`,
        );
        option.value = `${d.component_id}:${d.device_id}`;
        return option;
      }),
    );
  select.value = selected;
  select.hidden = devices.length < 2;
  const device = devices.find(
      (d) => `${d.component_id}:${d.device_id}` === selected,
    ),
    connected = Boolean(device?.connected);
  const state = $("gimbal-state");
  state.className =
    "state" + (connected ? (device.failure_flags ? " error" : "") : " muted");
  state.replaceChildren(
    element("i"),
    document.createTextNode(
      connected
        ? device.failure_flags
          ? "设备故障"
          : "在线"
        : device
          ? "遥测过期"
          : "未接入",
    ),
  );
  for (const key of ["pitch", "roll", "yaw"])
    $(`gimbal-${key}`).textContent = Number.isFinite(
      device?.attitude_deg?.[key],
    )
      ? `${device.attitude_deg[key].toFixed(1)}°`
      : "—";
  $("gimbal-note").textContent = device
    ? `组件 ${device.component_id} · 航向参考：${{ earth: "地理北向", vehicle: "机头方向", invalid: "参考系异常" }[device.yaw_frame]}${device.failure_flags ? ` · 故障标志 ${device.failure_flags}` : ""}${connected && !device.attitude_deg ? " · 姿态无效" : ""}`
    : "";
  select.onchange = () => {
    selected = select.value;
    renderGimbal(vehicle, online);
  };
  const config = videoSettings(currentVehicle),
    discovered = online ? vehicle?.video_streams?.[0] : null;
  const source =
    online && config.url
      ? `${currentVehicle}:${config.kind}:${config.url}`
      : "";
  if (source !== currentSource) {
    currentSource = source;
    resetMedia();
    if (source) {
      const video = config.kind === "video";
      media = element(video ? "video" : "img");
      const ownMedia = media;
      if (video) {
        media.controls = true;
        media.autoplay = true;
        media.muted = true;
        media.playsInline = true;
      } else media.alt = "云台实时画面";
      const ready = () => {
        if (media !== ownMedia) return;
        clearTimeout(mediaTimer);
        $("video-empty").hidden = true;
      };
      media.addEventListener(video ? "loadeddata" : "load", ready);
      media.addEventListener("error", () => {
        if (media !== ownMedia) return;
        clearTimeout(mediaTimer);
        emptyVideo("视频流不可用，请检查地址或格式");
      });
      $("video-surface").replaceChildren(media);
      emptyVideo("正在连接视频流…");
      mediaTimer = setTimeout(() => {
        if (media === ownMedia) emptyVideo("等待视频响应，请检查地址和网络");
      }, 12000);
      media.src = config.url;
    }
  }
  if (!source)
    emptyVideo(
      !online
        ? "服务连接断开，视频已暂停"
        : discovered?.uri?.startsWith("rtsp:")
          ? "已发现 RTSP 视频流，请配置转换后的地址"
          : "等待云台接入",
    );
}
