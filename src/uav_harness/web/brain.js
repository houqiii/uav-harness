import { $, element, request } from "./api.js";

const states = {
  created: "任务排队",
  queued: "准备继续",
  observing: "采集新观测",
  thinking: "VLM 在线规划",
  executing: "执行本轮动作",
  pausing: "中止规划并保持位置",
  paused: "已暂停，保持位置",
  cancelled: "任务已结束",
  succeeded: "任务完成，保持位置",
  not_found: "搜索结束，未发现目标",
  needs_operator: "等待操作者处理",
  failed: "任务中断",
  unknown: "执行结果不确定",
  preview_ready: "本轮规划完成 · 未执行",
};
const terminal = new Set([
  "cancelled",
  "succeeded",
  "not_found",
  "needs_operator",
  "failed",
  "unknown",
  "preview_ready",
]);
const actions = {
  observe: "重新观察",
  takeoff: "起飞",
  yaw: "调整航向",
  gimbal: "调整云台",
  zoom: "光学变焦",
  approach: "接近目标",
  altitude: "调整高度",
  hold: "保持位置",
  finish: "结束任务",
  move: "执行几何计算后的移动",
};
let backend =
  location.hash === "#hardware" || location.hash === "#console"
    ? "hardware"
    : "sandbox";
let backendEpoch = 0;
const storageKey = () => `brain.currentMission.${backend}`;
const prefix = () =>
  backend === "sandbox" ? "/v1/sandbox/brain" : "/v1/brain";
async function brainRequest(path = "", options) {
  const epoch = backendEpoch;
  const result = await request(prefix() + path, options);
  if (epoch !== backendEpoch) {
    const error = new Error("workspace switched");
    error.obsolete = true;
    throw error;
  }
  return result;
}
let sandboxTelemetry = null;
let metadata = null,
  current = null,
  preview = null,
  currentId = sessionStorage.getItem(storageKey()),
  online = false,
  busy = false,
  selectedFrame = null,
  frameSignature = "",
  eventSequence = 0,
  viewVersion = 0;
const urls = new Map(),
  pending = new Map();
const svgNS = "http://www.w3.org/2000/svg";
const fmt = (n, d = 1) => (Number.isFinite(n) ? n.toFixed(d) : "—");
const stamp = (t) =>
  new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false });
function svg(tag, attrs = {}) {
  const e = document.createElementNS(svgNS, tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  return e;
}
function polar(a, r) {
  const v = (a * Math.PI) / 180;
  return [160 + Math.sin(v) * r, 160 - Math.cos(v) * r];
}
function setupCompass() {
  const root = $("demo-compass");
  for (const r of [52, 94, 126])
    root.append(svg("circle", { cx: 160, cy: 160, r, class: "compass-ring" }));
  for (let i = 0; i < 12; i++) {
    const a = polar(i * 30 - 15, 126),
      b = polar(i * 30 + 15, 126),
      t = polar(i * 30, 145);
    root.append(
      svg("path", {
        id: `sector-${i}`,
        d: `M160 160 L${a} A126 126 0 0 1 ${b} Z`,
        class: "compass-sector",
      }),
    );
    const label = svg("text", {
      x: t[0],
      y: t[1] + 4,
      "text-anchor": "middle",
      class: "compass-label",
    });
    label.textContent = `${i * 30}°`;
    root.append(label);
  }
  const needle = svg("g", { id: "compass-needle" });
  needle.append(
    svg("line", { x1: 160, y1: 160, x2: 160, y2: 38, class: "compass-ray" }),
  );
  root.append(needle);
  root.append(
    svg("circle", {
      id: "compass-target",
      r: 6,
      class: "compass-target",
      visibility: "hidden",
    }),
    svg("circle", { cx: 160, cy: 160, r: 18, class: "compass-center" }),
  );
  root.append(
    svg("path", {
      d: "M150 150 L170 170 M170 150 L150 170",
      class: "compass-drone",
    }),
  );
}
function error(text = "") {
  $("demo-error").textContent = text;
  $("demo-error").hidden = !text;
}
function vehicle() {
  return metadata?.vehicles.find(
    (v) => v.vehicle_id === $("brain-vehicle").value,
  );
}
function active() {
  return current && !terminal.has(current.status);
}
function buttons() {
  const live = active();
  const uncertain = current?.status === "unknown";
  const v = vehicle();
  $("demo-start").disabled =
    busy ||
    Boolean(live || uncertain) ||
    !online ||
    !metadata?.provider.configured ||
    !v?.camera_configured ||
    (backend === "sandbox" && !metadata?.sandbox?.renderer_ready) ||
    !$("demo-intent").value.trim();
  $("demo-start").textContent =
    backend === "sandbox" || v?.execution_enabled ? "启动闭环" : "单轮规划";
  $("brain-preview").disabled =
    busy || !online || Boolean(live) || !v?.camera_configured;
  $("demo-intent").disabled = Boolean(live || uncertain);
  $("brain-vehicle").disabled = Boolean(live || uncertain) || busy;
  $("brain-objective").disabled = Boolean(live || uncertain) || busy;
  $("demo-pause").disabled =
    busy || !live || ["created", "queued", "pausing"].includes(current?.status);
  $("demo-pause").textContent =
    current?.status === "paused" ? "继续闭环" : "暂停并悬停";
  $("demo-reset").disabled = busy || (!live && !uncertain);
  $("demo-reset").textContent = uncertain ? "地面核验并释放" : "结束任务";
}
function metric(id, value, unit) {
  $(id).replaceChildren(
    document.createTextNode(value),
    element("small", "", unit),
  );
}
async function imageURL(path) {
  if (
    !(path?.startsWith("/v1/brain/") || path?.startsWith("/v1/sandbox/brain/"))
  )
    throw new Error("图像路径无效");
  if (urls.has(path)) return urls.get(path);
  if (pending.has(path)) return pending.get(path);
  const fetcher = (async () => {
    const token = sessionStorage.getItem("uav.apiToken"),
      headers = token ? { Authorization: `Bearer ${token}` } : {};
    const response = await fetch(path, {
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error(`图像读取失败 HTTP ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    urls.set(path, url);
    while (urls.size > 24) {
      const oldest = urls.keys().next().value;
      URL.revokeObjectURL(urls.get(oldest));
      urls.delete(oldest);
    }
    return url;
  })();
  pending.set(path, fetcher);
  try {
    return await fetcher;
  } finally {
    pending.delete(path);
  }
}
function renderFrames() {
  const frames = current?.frames || (preview ? [preview] : []);
  const signature = JSON.stringify(
    frames.map((f) => [f.observation_id, Boolean(f.detection)]),
  );
  if (signature === frameSignature) return;
  frameSignature = signature;
  $("demo-frames").replaceChildren(
    ...frames.slice(-12).map((f, i) => {
      const b = element(
        "button",
        "frame-tile" + (f.detection ? " candidate" : ""),
      );
      b.type = "button";
      b.dataset.frame = f.observation_id;
      const thumb = element("div", "frame-thumb"),
        img = element("img");
      img.alt = "观测缩略图";
      img.hidden = true;
      thumb.append(img);
      imageURL(f.image_url)
        .then((url) => {
          img.src = url;
          img.hidden = false;
        })
        .catch(() => {
          thumb.textContent = "图像已过期";
        });
      const caption = element("div", "frame-caption");
      caption.append(
        element("strong", "", fmt(f.camera.yaw_deg, 0) + "°"),
        element(
          "span",
          "frame-status",
          f.detection ? "VLM 目标" : stamp(f.captured_at),
        ),
      );
      b.append(thumb, caption);
      b.setAttribute(
        "aria-label",
        `查看 ${fmt(f.camera.yaw_deg, 0)} 度观测 ${i + 1}`,
      );
      b.onclick = () => {
        selectedFrame = f.observation_id;
        renderCamera();
      };
      return b;
    }),
  );
  if (!frames.length)
    $("demo-frames").append(
      element("p", "helper", "尚无采集图像；不会生成预设扫描结果。"),
    );
}
function renderCamera() {
  const frames = current?.frames || [],
    obs = current?.observation || preview;
  const f = selectedFrame
    ? frames.find((f) => f.observation_id === selectedFrame)
    : obs;
  if (!f) {
    selectedFrame = null;
    viewVersion++;
    $("demo-camera-image").hidden = true;
    $("demo-box").hidden = true;
    $("brain-camera-empty").hidden = false;
    $("brain-camera-empty").textContent =
      backend === "sandbox" ? "连接沙盒后采集原生云台画面" : "等待相机观测";
    $("brain-camera-source").textContent = "等待相机观测";
    for (const id of [
      "brain-pitch",
      "demo-gimbal-heading",
      "brain-frame-id",
      "brain-image-age",
      "demo-frame-label",
      "demo-camera-caption",
      "demo-camera-source",
    ])
      $(id).textContent = "—";
    $("brain-camera-note").textContent = "图像与拍摄时遥测绑定。";
    return;
  }
  const historical = Boolean(selectedFrame),
    d = historical
      ? f.detection
      : current?.decision?.observation_id === f.observation_id
        ? current.decision.target
        : null;
  $("demo-camera-image").parentElement.style.aspectRatio =
    `${f.camera.width} / ${f.camera.height}`;
  const version = ++viewVersion;
  imageURL(f.image_url)
    .then((url) => {
      if (version !== viewVersion) return;
      $("demo-camera-image").src = url;
      $("demo-camera-image").hidden = false;
      $("brain-camera-empty").hidden = true;
    })
    .catch((e) => {
      if (version === viewVersion) {
        $("brain-camera-empty").hidden = false;
        $("brain-camera-empty").textContent = e.message;
      }
    });
  $("demo-frame-label").textContent =
    `${historical ? "历史观测" : "当前观测"} · ${stamp(f.captured_at)}`;
  $("brain-image-age").textContent =
    `${Math.max(0, Math.floor(Date.now() / 1000 - f.captured_at))} s 前`;
  $("brain-camera-source").textContent =
    f.source === "drone4d_native"
      ? "Drone4D 原生 FPV · 仿真"
      : f.source === "geometric_simulation"
        ? "几何仿真 · 非实拍"
        : "HTTP 相机观测";
  $("demo-camera-source").textContent =
    f.camera.timing === "camera_timestamp"
      ? "相机时间戳"
      : f.source === "drone4d_native"
        ? "原生渲染帧"
        : f.source === "geometric_simulation"
          ? "动态渲染"
          : "接收时间估计";
  $("demo-camera-caption").textContent = d
    ? d.label
    : current?.status === "thinking"
      ? "VLM 正在分析此帧"
      : "此帧没有已返回的目标识别";
  $("brain-pitch").textContent = fmt(f.camera.pitch_deg, 0) + "°";
  $("demo-gimbal-heading").textContent = fmt(f.camera.yaw_deg, 0) + "°";
  $("brain-frame-id").textContent = f.observation_id.slice(0, 6);
  $("demo-camera-live").hidden = !historical;
  $("brain-camera-note").textContent =
    `${f.camera.source} · ${f.camera.ground_down_m === null ? "地面几何未知，目标接近与下降受限" : "已提供地面模型；按标定误差约束动作"}`;
  $("demo-box").hidden = !d;
  if (d) {
    const [l, t, r, b] = d.bbox;
    Object.assign($("demo-box").style, {
      left: `${l / 10}%`,
      top: `${t / 10}%`,
      width: `${(r - l) / 10}%`,
      height: `${(b - t) / 10}%`,
      transform: "none",
    });
    $("brain-box-label").textContent = d.label;
  }
  document.querySelectorAll("#demo-frames button").forEach((b) => {
    const selected = b.dataset.frame === f.observation_id;
    b.classList.toggle("selected", selected);
    b.setAttribute("aria-pressed", String(selected));
  });
}
function render() {
  const s = current,
    obs = s?.observation || preview,
    t =
      (backend === "sandbox" && sandboxTelemetry) ||
      s?.live_telemetry ||
      obs?.telemetry,
    target = s?.target,
    g = target?.geometry;
  $("demo-phase").textContent = s ? states[s.status] || s.status : "等待任务";
  $("brain-target-name").textContent = target?.label || "目标待识别";
  $("demo-target").textContent = target
    ? target.visible
      ? target.confirmed_observations >= 2
        ? "已连续确认"
        : "待二次确认"
      : "当前不可见"
    : "未知";
  $("demo-bearing").textContent = g ? fmt(g.bearing_deg, 0) + "°" : "—";
  $("demo-distance").textContent = g
    ? fmt(g.horizontal_distance_m) + " m"
    : "—";
  $("brain-uncertainty").textContent = g
    ? "± " + fmt(g.uncertainty_m) + " m"
    : "—";
  $("brain-target-source").textContent = g
    ? `${g.source}${target.visible ? "" : " · 历史估计，禁止沿用接近"}`
    : "目标由 VLM 识别；未获得可靠几何时不猜测距离";
  const headings = s?.scan_headings || [];
  $("demo-scan-count").textContent =
    `已观测 ${headings.length} / 12 个方位 · ${s?.frames?.length || (preview ? 1 : 0)} 帧记录`;
  for (let i = 0; i < 12; i++)
    $(`sector-${i}`).setAttribute(
      "class",
      "compass-sector" + (headings.includes(i * 30) ? " clear" : ""),
    );
  $("compass-needle").setAttribute(
    "transform",
    `rotate(${t?.heading_deg || 0} 160 160)`,
  );
  $("compass-target").setAttribute("visibility", g ? "visible" : "hidden");
  if (g) {
    const [x, y] = polar(g.bearing_deg, 110);
    $("compass-target").setAttribute("cx", x);
    $("compass-target").setAttribute("cy", y);
  }
  if (t) {
    metric("demo-altitude", fmt(t.home_down_m - t.position_ned[2]), " m");
    metric(
      "demo-speed",
      fmt(Math.hypot(...t.velocity_ned.slice(0, 2))),
      " m/s",
    );
    metric("demo-heading", fmt(t.heading_deg, 0), "°");
    metric("demo-battery", fmt(t.battery_percent, 0), "%");
    $("demo-flight-mode").textContent = t.airborne
      ? "空中 · " + (t.armed ? "已解锁" : "未解锁")
      : "地面";
    $("demo-ned").textContent =
      `N ${fmt(t.position_ned[0])} · E ${fmt(t.position_ned[1])} · D ${fmt(t.position_ned[2])} m`;
    $("brain-telemetry-source").replaceChildren(
      element("i"),
      document.createTextNode(
        t.source.includes("drone4d")
          ? "Drone4D 物理遥测"
          : t.source.includes("simulation")
            ? "运动学仿真"
            : "MAVLink 遥测",
      ),
    );
  } else {
    for (const [id, unit] of [
      ["demo-altitude", " m"],
      ["demo-speed", " m/s"],
      ["demo-heading", "°"],
      ["demo-battery", "%"],
    ])
      metric(id, "—", unit);
    $("demo-flight-mode").textContent = "等待飞行状态";
    $("demo-ned").textContent = "N — · E — · D — m";
    $("brain-telemetry-source").textContent = "等待观测";
  }
  const stages = [
    "采集观测",
    "VLM 规划",
    "几何校验",
    "执行动作",
    "重新观测",
    "任务结束",
  ];
  const index = !s
    ? 0
    : s.status === "observing"
      ? 0
      : s.status === "thinking"
        ? 1
        : s.status === "executing"
          ? 3
          : terminal.has(s.status)
            ? 5
            : 4;
  $("demo-stages")
    .querySelectorAll("li")
    .forEach((li, i) => {
      li.classList.toggle("active", i === index);
      const completed = [
        Boolean(s?.observation),
        Boolean(s?.decision),
        Boolean(
          s?.events.some((e) => ["dispatch", "rejected"].includes(e.kind)),
        ),
        Boolean(s?.events.some((e) => e.kind === "result")),
        (s?.round || 0) > 1,
        ["succeeded", "not_found", "needs_operator", "cancelled"].includes(
          s?.status,
        ),
      ];
      li.classList.toggle("done", completed[i] && i !== index);
    });
  const d = s?.decision;
  $("brain-round").textContent = `第 ${s?.round || 0} 轮`;
  $("brain-task").textContent =
    d?.task_interpretation || "同一个 VLM 负责理解任务和后续在线调整";
  $("brain-evidence").textContent =
    d?.scene_summary || "每轮使用新图像、拍摄姿态、遥测和上一次动作结果。";
  $("brain-task-plan").replaceChildren(
    ...(d?.plan || []).map((x) => element("li", "", x)),
  );
  $("brain-action").textContent = d
    ? `${actions[d.next_action.action] || d.next_action.action} · ${JSON.stringify(d.next_action)}`
    : "—";
  const result = s?.events
    .slice()
    .reverse()
    .find((e) => e.kind === "result" || e.kind === "rejected");
  $("brain-execution").textContent = result
    ? JSON.stringify(result.result)
    : "—";
  const seq = s?.events.at(-1)?.sequence || 0;
  if (seq !== eventSequence) {
    eventSequence = seq;
    $("demo-events").replaceChildren(
      ...(s?.events || [])
        .slice(-50)
        .reverse()
        .map((e) => {
          const li = element(
            "li",
            e.kind === "error" || e.kind === "rejected"
              ? "brain-event-error"
              : "",
          );
          li.append(
            element("time", "", stamp(e.at)),
            element(
              "strong",
              "",
              {
                observation: "观测",
                decision: "VLM 决策",
                dispatch: "执行",
                result: "回执",
                rejected: "动作拒绝",
                error: "错误",
                finished: "完成",
                paused: "暂停",
                cancelled: "结束",
              }[e.kind] || e.kind,
            ),
            element("span", "", e.message),
          );
          return li;
        }),
    );
  }
  $("demo-events-empty").hidden = Boolean(s?.events.length);
  $("demo-elapsed").textContent = s
    ? `${s.elapsed_s.toFixed(0)} s · ${states[s.status] || s.status}`
    : "等待任务";
  const lastError = s?.events
    .slice()
    .reverse()
    .find((e) => e.kind === "error");
  if (lastError && ["failed", "unknown"].includes(s.status))
    error(lastError.message);
  $("demo-result").textContent =
    lastError && ["failed", "unknown"].includes(s.status)
      ? lastError.message
      : s
        ? states[s.status]
        : "VLM 每次只提出一个短动作；收到执行结果后重新观察和规划。";
  if (s?.status === "thinking")
    $("demo-result").textContent =
      `VLM 正在处理新观测 · ${s.progress.elapsed_s || 0} s · ${s.progress.phase || "等待"}；当前阶段不发出新运动指令`;
  renderFrames();
  renderCamera();
  buttons();
}
async function previewFrame() {
  if (active()) return;
  const generation = backendEpoch;
  busy = true;
  buttons();
  error();
  try {
    preview = await brainRequest("/preview", {
      body: { vehicle_id: $("brain-vehicle").value },
      timeout: backend === "sandbox" ? 40000 : 10000,
    });
    current = null;
    selectedFrame = null;
    frameSignature = "";
    eventSequence = 0;
    render();
  } catch (e) {
    if (e.obsolete) return;
    error(e.message);
  } finally {
    if (generation === backendEpoch) {
      busy = false;
      buttons();
    }
  }
}
async function start() {
  if (busy || active()) return;
  const generation = backendEpoch;
  busy = true;
  buttons();
  error();
  try {
    current = await brainRequest("/missions", {
      body: {
        query: $("demo-intent").value.trim(),
        vehicle_id: $("brain-vehicle").value,
        objective: $("brain-objective").value,
      },
    });
    currentId = current.mission_id;
    sessionStorage.setItem(storageKey(), currentId);
    selectedFrame = null;
    frameSignature = "";
    eventSequence = 0;
    render();
  } catch (e) {
    if (e.obsolete) return;
    error(e.message);
  } finally {
    if (generation === backendEpoch) {
      busy = false;
      buttons();
    }
  }
}
async function control(action) {
  if (!currentId || busy) return;
  const generation = backendEpoch;
  busy = true;
  buttons();
  error();
  try {
    current = await brainRequest(`/missions/${currentId}/${action}`, {
      body: {},
      timeout: 150000,
    });
    render();
  } catch (e) {
    if (e.obsolete) return;
    error(e.message);
  } finally {
    if (generation === backendEpoch) {
      busy = false;
      buttons();
    }
  }
}
async function poll() {
  try {
    if (!metadata || !online) {
      metadata = await brainRequest();
      online = true;
      const picker = $("brain-vehicle");
      picker.replaceChildren(
        ...metadata.vehicles.map((v) => {
          const o = element("option", "", `${v.vehicle_id} · ${v.backend}`);
          o.value = v.vehicle_id;
          return o;
        }),
      );
      picker.value =
        metadata.vehicles.find((v) => v.backend === "px4")?.vehicle_id ||
        metadata.vehicles[0]?.vehicle_id ||
        "";
      const restore =
        metadata.missions.find((m) => m.mission_id === currentId) ||
        metadata.missions.find((m) => !terminal.has(m.status));
      if (restore) {
        currentId = restore.mission_id;
        sessionStorage.setItem(storageKey(), currentId);
        picker.value = restore.vehicle_id;
      } else {
        currentId = null;
        sessionStorage.removeItem(storageKey());
      }
      const sim = metadata.mode === "geometric_simulation";
      $("brain-mode").replaceChildren(
        element("i"),
        document.createTextNode(
          metadata.mode === "drone4d"
            ? "Drone4D · VLM 在线规划"
            : metadata.mode === "hardware_disconnected"
              ? "实机接口待连接"
              : sim
                ? "几何仿真 · 真实 VLM"
                : "实机图像与遥测",
        ),
      );
      $("brain-control-note").textContent =
        metadata.mode === "drone4d"
          ? "执行器：Drone4D 原生物理与云台"
          : sim
            ? "执行器：运动学仿真"
            : metadata.hardware_control_enabled
              ? "实机执行已配置"
              : "观察模式 · 不执行";
      $("demo-source").textContent =
        `${metadata.provider.provider} / ${metadata.provider.model} · ${metadata.provider.configured ? "密钥已配置" : "未配置 DASHSCOPE_API_KEY"}`;
      $("brain-provider").textContent = metadata.provider.endpoint;
      $("demo-plan").textContent = JSON.stringify(
        { policy: metadata.policy, vehicles: metadata.vehicles },
        null,
        2,
      );
      if (
        !currentId &&
        picker.value &&
        metadata.vehicles.find((v) => v.vehicle_id === picker.value)
          ?.camera_configured &&
        (backend !== "sandbox" || metadata.sandbox.renderer_ready)
      )
        await previewFrame();
    }
    if (currentId) {
      current = await brainRequest(`/missions/${currentId}`);
      online = true;
      $("demo-intent").value = current.query;
      $("brain-vehicle").value = current.vehicle_id;
      $("brain-objective").value = current.objective;
      render();
    } else render();
  } catch (e) {
    if (e.obsolete) return;
    online = false;
    error(e.message);
    if (e.message.includes("mission not found")) {
      currentId = null;
      current = null;
      metadata = null;
      sessionStorage.removeItem(storageKey());
    }
    buttons();
  } finally {
    setTimeout(poll, 700);
  }
}
export function updateBrainContext(snapshot, connected) {
  if (!connected) {
    online = false;
    buttons();
  }
}
export function initBrainUI() {
  setupCompass();
  [
    "采集观测",
    "VLM 规划",
    "几何校验",
    "执行动作",
    "重新观测",
    "任务结束",
  ].forEach((label, i) => {
    const li = element("li");
    li.append(
      element("span", "", String(i + 1)),
      document.createTextNode(label),
    );
    $("demo-stages").append(li);
  });
  $("demo-form").onsubmit = (e) => {
    e.preventDefault();
    start();
  };
  $("brain-preview").onclick = previewFrame;
  $("demo-pause").onclick = () =>
    control(current?.status === "paused" ? "resume" : "pause");
  $("demo-reset").onclick = () =>
    control(current?.status === "unknown" ? "reconcile" : "cancel");
  $("demo-intent").oninput = () => {
    if (current && !active()) {
      current = null;
      currentId = null;
      sessionStorage.removeItem(storageKey());
      selectedFrame = null;
      frameSignature = "";
      eventSequence = 0;
      render();
    }
    error();
    buttons();
  };
  $("brain-vehicle").onchange = () => {
    current = null;
    currentId = null;
    preview = null;
    selectedFrame = null;
    frameSignature = "";
    sessionStorage.removeItem(storageKey());
    previewFrame();
  };
  $("demo-camera-live").onclick = () => {
    selectedFrame = null;
    renderCamera();
  };
  function view() {
    const consoleView = location.hash === "#console";
    const nextBackend =
      location.hash === "#hardware" || consoleView ? "hardware" : "sandbox";
    if (nextBackend !== backend) {
      backend = nextBackend;
      backendEpoch++;
      metadata = null;
      online = false;
      current = null;
      preview = null;
      currentId = sessionStorage.getItem(storageKey());
      busy = false;
      selectedFrame = null;
      frameSignature = "";
      eventSequence = 0;
      viewVersion++;
      $("demo-camera-image").hidden = true;
      $("demo-box").hidden = true;
      $("brain-camera-empty").hidden = false;
      $("brain-camera-empty").textContent = "等待此工作区的新观测";
      error();
      render();
    }
    $("demo-title").textContent =
      backend === "sandbox" ? "沙盒在线意图任务" : "实机意图控制";
    $("demo-console").hidden = consoleView;
    $("demo-footer").hidden = consoleView;
    $("control-console").hidden = !consoleView;
    $("control-footer").hidden = !consoleView;
    for (const [id, on] of [
      ["demo-tab", backend === "sandbox"],
      ["hardware-tab", backend === "hardware" && !consoleView],
      ["console-tab", consoleView],
    ]) {
      $(id).classList.toggle("selected", on);
      if (on) $(id).setAttribute("aria-current", "page");
      else $(id).removeAttribute("aria-current");
    }
    window.dispatchEvent(
      new CustomEvent("brain-workspace", { detail: { backend } }),
    );
  }
  $("demo-tab").onclick = () => {
    location.hash = "sandbox";
  };
  $("hardware-tab").onclick = () => {
    location.hash = "hardware";
  };
  $("console-tab").onclick = () => {
    location.hash = "console";
  };
  window.addEventListener("drone4d-state", (event) => {
    sandboxTelemetry =
      event.detail.vehicles.find(
        (v) => v.vehicle_id === $("brain-vehicle").value,
      )?.telemetry || null;
    if (backend === "sandbox") render();
  });
  window.addEventListener("drone4d-changed", () => {
    if (backend === "sandbox") metadata = null;
  });
  window.addEventListener("hashchange", view);
  view();
  render();
  poll();
}
