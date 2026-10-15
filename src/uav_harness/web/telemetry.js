import { $, element } from "./api.js";
const deg = (v) =>
  Number.isFinite(v) ? `${((v * 180) / Math.PI).toFixed(1)}°` : "—";
const landedLabels = {
  0: "未知",
  1: "地面",
  2: "空中",
  3: "起飞中",
  4: "降落中",
};
const apModes = {
  0: "STABILIZE",
  1: "ACRO",
  2: "ALT_HOLD",
  3: "AUTO",
  4: "GUIDED",
  5: "LOITER",
  6: "RTL",
  7: "CIRCLE",
  9: "LAND",
  11: "DRIFT",
  13: "SPORT",
  14: "FLIP",
  15: "AUTOTUNE",
  16: "POSHOLD",
  17: "BRAKE",
  20: "GUIDED_NOGPS",
};
export function modeName(vehicle) {
  const hb = vehicle?.heartbeat;
  if (!hb) return "—";
  if (vehicle.backend === "ardupilot")
    return apModes[hb.custom_mode] || `MODE ${hb.custom_mode}`;
  const mode = (hb.custom_mode >> 16) & 255,
    sub = (hb.custom_mode >> 24) & 255;
  return (
    {
      1: "MANUAL",
      2: "ALTCTL",
      3: "POSCTL",
      4: `AUTO/${{ 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL", 6: "LAND" }[sub] || sub}`,
      5: "ACRO",
      6: "OFFBOARD",
      7: "STABILIZED",
    }[mode] || `MODE ${mode}/${sub}`
  );
}
export function initTelemetry() {
  for (let i = 1; i <= 8; i++) {
    const channel = element("div", "pwm-channel");
    channel.append(element("label", "", `CH${i}`));
    const row = element("div", "pwm-value"),
      track = element("div", "pwm-track"),
      bar = element("span");
    bar.id = `pwm-bar-${i}`;
    track.append(bar);
    const value = element("output", "", "—");
    value.id = `pwm-value-${i}`;
    row.append(track, value);
    channel.append(row);
    $("pwm-grid").append(channel);
  }
}
export function renderTelemetry(vehicle, online, offline = false) {
  const v = online ? vehicle : null;
  const connected = Boolean(v?.connected);
  $("vehicle-state").className = "state" + (connected ? "" : " muted");
  $("vehicle-state").replaceChildren(
    element("i"),
    document.createTextNode(
      offline ? "未接入" : connected ? "在线" : online ? "离线" : "断开",
    ),
  );
  $("mode").textContent = modeName(v);
  $("armed").textContent = v?.heartbeat
    ? v.heartbeat.base_mode & 128
      ? "已解锁"
      : "未解锁"
    : "—";
  $("armed").style.color = v?.heartbeat?.base_mode & 128 ? "#ffce81" : "";
  $("landed").textContent = v?.landed
    ? landedLabels[v.landed.landed_state] || "未知"
    : "—";
  const a = v?.attitude,
    valid = a && [a.roll, a.pitch, a.yaw].every(Number.isFinite);
  ["roll", "pitch", "yaw"].forEach(
    (k) => ($(k).textContent = deg(valid ? a[k] : null)),
  );
  $("horizon").classList.toggle("unavailable", !valid);
  $("horizon").setAttribute(
    "aria-label",
    valid ? `横滚 ${deg(a.roll)}，俯仰 ${deg(a.pitch)}` : "姿态遥测缺失或过期",
  );
  $("horizon-world").style.transform = valid
    ? `rotate(${(-a.roll * 180) / Math.PI}deg) translateY(${Math.max(-60, Math.min(60, ((a.pitch * 180) / Math.PI) * 1.4))}px)`
    : "none";
  const b = v?.sys_status,
    voltage =
      b &&
      Number.isFinite(b.voltage_battery) &&
      ![0, 65535].includes(b.voltage_battery)
        ? `${(b.voltage_battery / 1000).toFixed(2)} V`
        : "未知";
  const remaining =
    b && b.battery_remaining >= 0 && b.battery_remaining <= 100
      ? `${b.battery_remaining}%`
      : "未知";
  $("battery").textContent = b ? `${voltage} / ${remaining}` : "—";
  const pos = v?.local_position_ned;
  $("position").textContent =
    pos && [pos.x, pos.y, pos.z].every(Number.isFinite)
      ? `${pos.x.toFixed(2)}, ${pos.y.toFixed(2)}, ${pos.z.toFixed(2)} m`
      : offline
        ? "—"
        : "缺失 / 过期";
  $("telemetry-note").textContent = v?.servo_output
    ? "实时更新"
    : offline
      ? "离线推演 · 无遥测"
      : "输出缺失 / 过期";
  for (let i = 1; i <= 8; i++) {
    const value = v?.servo_output?.[`servo${i}_raw`],
      validValue = Number.isFinite(value) && value > 0;
    $(`pwm-value-${i}`).textContent = validValue ? `${value} μs` : "—";
    $(`pwm-bar-${i}`).style.width = validValue
      ? `${Math.max(0, Math.min(100, ((value - 1000) / 1000) * 100))}%`
      : "0%";
  }
}
