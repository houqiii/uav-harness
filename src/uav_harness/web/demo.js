import { $, element, previewIntent } from "./api.js";
import { DemoMission, PRESET_QUERY, STAGES, PHASES } from "./demo-state.js";

const mission = new DemoMission();
let service = null,
  online = false,
  proposal = null,
  parsedQuery = "",
  controller = null,
  parsing = false,
  version = 0,
  selectedFrame = null,
  eventCount = -1,
  lastTick = performance.now();
const svgNS = "http://www.w3.org/2000/svg";
const normalize = (s) => s.trim().replace(/[，。！？,.!?\s]/g, "");
const preset = () =>
  normalize($("demo-intent").value) === normalize(PRESET_QUERY);
const time = (s) =>
  `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
function svg(tag, attrs = {}) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function point(degrees, r) {
  const a = (degrees * Math.PI) / 180;
  return [160 + Math.sin(a) * r, 160 - Math.cos(a) * r];
}
function buildCompass() {
  const root = $("demo-compass");
  for (const r of [52, 94, 126])
    root.append(svg("circle", { cx: 160, cy: 160, r, class: "compass-ring" }));
  for (let i = 0; i < 12; i++) {
    const a = i * 30 - 15,
      p = point(a, 126),
      q = point(a + 30, 126);
    root.append(
      svg("path", {
        id: `sector-${i}`,
        d: `M160 160 L${p} A126 126 0 0 1 ${q} Z`,
        class: "compass-sector",
      }),
    );
    const t = point(i * 30, 145),
      label = svg("text", {
        x: t[0],
        y: t[1] + 4,
        "text-anchor": "middle",
        class: "compass-label",
      });
    label.textContent = [
      "N · 0°",
      "30°",
      "60°",
      "E · 90°",
      "120°",
      "150°",
      "S · 180°",
      "210°",
      "240°",
      "W · 270°",
      "300°",
      "330°",
    ][i];
    root.append(label);
  }
  const needle = svg("g", { id: "compass-needle" });
  needle.append(
    svg("line", { x1: 160, y1: 160, x2: 160, y2: 37, class: "compass-ray" }),
    svg("path", { d: "M155 42 L160 32 L165 42", class: "compass-arrow" }),
  );
  root.append(needle);
  const dot = point(120, 110);
  root.append(
    svg("circle", {
      id: "compass-target",
      cx: dot[0],
      cy: dot[1],
      r: 6,
      class: "compass-target",
      visibility: "hidden",
    }),
  );
  root.append(
    svg("circle", { cx: 160, cy: 160, r: 23, class: "compass-center" }),
  );
  root.append(
    svg("path", {
      d: "M148 148 L172 172 M172 148 L148 172 M153 160 H167 M160 153 V167",
      class: "compass-drone",
    }),
  );
  for (const [x, y] of [
    [148, 148],
    [172, 148],
    [148, 172],
    [172, 172],
  ])
    root.append(svg("circle", { cx: x, cy: y, r: 5, class: "compass-drone" }));
}
function buildFrames() {
  for (let i = 0; i < 12; i++) {
    const b = element("button", "frame-tile");
    b.type = "button";
    b.dataset.index = i;
    const thumb = element("div", "frame-thumb");
    const img = element("img");
    img.src = "/assets/search-field.jpg";
    img.alt = "";
    img.hidden = true;
    thumb.append(
      img,
      element("span", "frame-placeholder", String(i + 1).padStart(2, "0")),
    );
    const caption = element("div", "frame-caption");
    caption.append(
      element("strong", "", `${i * 30}°`),
      element("span", "frame-status", "等待"),
    );
    b.append(thumb, caption);
    b.onclick = () => {
      selectedFrame = i;
      render();
    };
    $("demo-frames").append(b);
  }
}
function showError(text = "") {
  $("demo-error").hidden = !text;
  $("demo-error").textContent = text;
}
function updateButtons() {
  const locked = mission.running;
  $("demo-start").disabled =
    parsing ||
    locked ||
    (!preset() && parsedQuery !== $("demo-intent").value.trim());
  $("demo-start").lastChild.textContent =
    mission.phase === "idle" ? "开始演练" : "重新演练";
  $("demo-parse").disabled =
    parsing ||
    locked ||
    !online ||
    !service?.compiler.configured ||
    !$("demo-intent").value.trim();
  $("demo-parse").textContent = parsing ? "正在解析…" : "DeepSeek 解析";
  $("demo-intent").disabled = locked;
  $("demo-scenario").disabled = locked || parsing;
  $("demo-pause").disabled = !locked;
  $("demo-pause").textContent = mission.paused ? "继续演练" : "暂停演练";
  $("demo-cancel-parse").hidden = !parsing;
}
function renderPlan() {
  $("demo-plan").textContent = JSON.stringify(
    proposal || {
      task: "search_and_observe_person",
      target: "戴红帽子的人",
      search_altitude_m: 50,
      scan_step_deg: 30,
      scan_frames: 12,
      initial_horizontal_distance_m: 80,
      stand_off_m: 20,
      move_step_m: 10,
      intent_source: "固定模板，未调用模型",
      vision_source: "预设结果",
      telemetry_source: "模拟数据",
      executable: false,
    },
    null,
    2,
  );
}
function stageIndex() {
  return {
    idle: 0,
    climb: 1,
    scan: 2,
    confirm: 3,
    approach: 4,
    complete: 5,
    not_found: 2,
    holding: 4,
  }[mission.phase];
}
function render() {
  const m = mission,
    stage = stageIndex();
  $("demo-stages")
    .querySelectorAll("li")
    .forEach((li, i) => {
      li.classList.toggle("done", i < stage || m.phase === "complete");
      li.classList.toggle("active", i === stage && m.phase !== "complete");
      if (i === stage) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
  $("demo-phase").textContent = (m.paused ? "已暂停 · " : "") + PHASES[m.phase];
  $("demo-phase").classList.toggle(
    "interrupted",
    ["holding", "not_found"].includes(m.phase),
  );
  $("demo-scan-count").textContent =
    `${m.phase === "scan" ? "扫描中" : m.frames.length === 12 ? "扫描结束" : "等待开始"} · ${m.frames.length} / 12`;
  $("compass-needle").setAttribute("transform", `rotate(${m.heading} 160 160)`);
  $("compass-target").setAttribute(
    "visibility",
    m.candidate ? "visible" : "hidden",
  );
  const frameIndex =
    selectedFrame ??
    (m.phase === "scan" ? m.frames.length - 1 : m.candidate ? 4 : -1);
  for (let i = 0; i < 12; i++) {
    const f = m.frames[i],
      b = $("demo-frames").children[i],
      target = i === 4 && m.scenario !== "missing";
    const labels = {
      pending: "分析中",
      candidate: "发现候选",
      clear: "已检查",
    };
    b.className = `frame-tile ${f?.status || ""}${frameIndex === i ? " selected" : ""}`;
    b.setAttribute("aria-pressed", String(frameIndex === i));
    b.setAttribute(
      "aria-label",
      `${i * 30}度，${labels[f?.status] || "等待采集"}`,
    );
    b.disabled = !f;
    b.querySelector(".frame-status").textContent = labels[f?.status] || "等待";
    b.querySelector("img").hidden = !f;
    b.querySelector("img").style.transform = target ? "none" : "scale(3.3)";
    b.querySelector("img").style.transformOrigin = `${i % 2 ? 0 : 100}% 0%`;
    b.querySelector(".frame-placeholder").hidden = Boolean(f);
    $(`sector-${i}`).setAttribute(
      "class",
      `compass-sector ${f?.status || ""}${frameIndex === i ? " selected" : ""}`,
    );
  }
  $("demo-target").textContent =
    m.phase === "holding"
      ? "复核中断"
      : m.confirmed
        ? "已确认"
        : m.candidate
          ? "发现候选"
          : m.phase === "not_found"
            ? "未发现"
            : "等待扫描";
  $("demo-bearing").textContent = m.candidate ? "120°" : "—";
  $("demo-distance").textContent =
    m.distance === null ? "—" : `${m.distance.toFixed(1)} m`;
  $("demo-approach").value = m.travelled;
  $("demo-approach-label").textContent = `${m.travelled.toFixed(0)} / 60 m`;
  $("demo-altitude").replaceChildren(
    document.createTextNode(m.altitude.toFixed(1)),
    element("small", "", " m"),
  );
  $("demo-speed").replaceChildren(
    document.createTextNode(m.speed.toFixed(1)),
    element("small", "", " m/s"),
  );
  $("demo-heading").replaceChildren(
    document.createTextNode(
      String(Math.round(m.heading) % 360).padStart(3, "0"),
    ),
    element("small", "", "°"),
  );
  $("demo-battery").replaceChildren(
    document.createTextNode(String(90 - Math.floor(m.elapsed / 15))),
    element("small", "", "%"),
  );
  $("demo-flight-mode").textContent =
    `PX4 · ${m.phase === "idle" ? "等待任务" : m.paused ? "演练暂停" : m.speed > 0 ? "位置移动" : "位置保持"}`;
  const north = m.travelled * Math.cos((120 * Math.PI) / 180),
    east = m.travelled * Math.sin((120 * Math.PI) / 180);
  $("demo-ned").textContent =
    `N ${north.toFixed(1)} · E ${east.toFixed(1)} · D ${(-m.altitude).toFixed(1)} m`;
  $("demo-elapsed").textContent = `演练时间 ${time(m.elapsed)}`;
  renderCamera(frameIndex);
  if (eventCount !== m.events.length) {
    eventCount = m.events.length;
    $("demo-events").replaceChildren(
      ...m.events
        .slice()
        .reverse()
        .map((e) => {
          const li = element("li");
          li.append(
            element("time", "", time(e.time)),
            element("strong", "", e.title),
            element("span", "", e.detail),
          );
          return li;
        }),
    );
    $("demo-events-empty").hidden = m.events.length > 0;
  }
  $("demo-result").textContent =
    m.reason ||
    (m.phase === "complete"
      ? "演练完成 · 水平距离 20 m，高度 50 m，保持悬停。"
      : m.paused
        ? "演练已暂停，位置、采集与分析进度保持不变。"
        : "预设流程：50 m 高度 → 12 方向扫描 → 确认 → 分段接近 → 悬停");
  $("demo-result").classList.toggle("error-copy", m.phase === "holding");
  updateButtons();
}
function renderCamera(index) {
  const m = mission,
    f = m.frames[index],
    historical = selectedFrame !== null;
  const scanFrame = historical || m.phase === "scan";
  const lost = m.phase === "holding" && m.scenario === "lost" && !historical;
  const showTarget =
    (scanFrame ? f?.found : m.candidate) && m.phase !== "not_found" && !lost;
  const targetView =
    (scanFrame ? index === 4 && m.scenario !== "missing" : m.candidate) &&
    !lost;
  const preview = m.phase === "idle";
  const zoom = !historical && m.confirmed ? 1 + m.travelled / 120 : 1;
  $("demo-camera-image").style.transform =
    targetView || preview ? `scale(${zoom})` : "scale(3.3)";
  $("demo-camera-image").style.transformOrigin =
    targetView || preview ? "50% 50%" : `${index % 2 ? 0 : 100}% 0%`;
  $("demo-box").hidden = !showTarget;
  $("demo-box").style.transform = `scale(${zoom})`;
  $("demo-frame-label").textContent = historical
    ? `扫描帧 ${String(f.id).padStart(2, "0")} · ${time(f.capturedAt)}`
    : preview
      ? "演示素材预览"
      : m.confirmed
        ? "接近复核 · 示意画面"
        : f
          ? `扫描帧 ${String(f.id).padStart(2, "0")} · ${time(f.capturedAt)}`
          : "等待采集";
  const yaw = scanFrame && f ? f.heading : m.heading;
  $("demo-gimbal-heading").textContent =
    String(Math.round(yaw) % 360).padStart(3, "0") + "°";
  $("demo-zoom").textContent = "1.0×";
  $("demo-camera-caption").textContent =
    scanFrame && f
      ? `${f.heading}° · ${f.found ? "发现候选" : f.status === "pending" ? "分析中" : "未发现匹配目标"}`
      : m.phase === "holding"
        ? m.scenario === "lost"
          ? "目标丢失 · 预设场景"
          : "视觉超时 · 最后一帧"
        : m.confirmed
          ? "目标已确认 · 预设结果"
          : m.candidate
            ? "发现候选 · 预设结果"
            : "方位示意 · 非真实航拍";
  $("demo-camera-source").textContent = "AI 生成素材";
  $("demo-camera-live").hidden = !historical;
}
async function parseIntent() {
  if (parsing || mission.running) return;
  const query = $("demo-intent").value.trim();
  if (!query) return showError("请输入任务描述。");
  showError();
  const current = ++version;
  controller = new AbortController();
  parsing = true;
  proposal = null;
  parsedQuery = "";
  updateButtons();
  renderPlan();
  const started = performance.now();
  const phases = {
    connecting: "连接官方接口",
    waiting: "等待模型响应",
    reasoning: "解析任务",
    generating: "生成结构化任务",
  };
  $("demo-source").textContent = "DeepSeek · 连接官方接口…";
  const timer = setInterval(() => {
    if (current === version)
      $("demo-source").textContent =
        `DeepSeek · 正在解析 · ${Math.floor((performance.now() - started) / 1000)} s`;
  }, 1000);
  try {
    const result = await previewIntent(
      query,
      (e) => {
        if (current === version)
          $("demo-source").textContent =
            `DeepSeek · ${phases[e.phase] || "正在解析"} · ${e.elapsed_s || 0} s`;
      },
      controller.signal,
      "/v1/demo/preview-stream",
    );
    if (current !== version) return;
    proposal = result.demo;
    parsedQuery = query;
    $("demo-source").textContent =
      `DeepSeek 真实解析完成 · ${((performance.now() - started) / 1000).toFixed(1)} s`;
    renderPlan();
  } catch (e) {
    if (current !== version) return;
    const aborted = controller?.signal.aborted;
    $("demo-source").textContent = aborted
      ? "解析已停止 · 未生成任务"
      : "DeepSeek 解析失败 · 未生成任务";
    if (!aborted)
      showError(e.name === "AbortError" ? "解析超时，请重试。" : e.message);
  } finally {
    clearInterval(timer);
    if (current === version) {
      parsing = false;
      controller = null;
      updateButtons();
    }
  }
}
function cancelParse() {
  version++;
  controller?.abort();
  controller = null;
  parsing = false;
}
export function updateDemoService(snapshot, connected) {
  service = snapshot;
  online = connected;
  $("demo-parse").title = !connected
    ? "本地服务未连接"
    : !snapshot?.compiler.configured
      ? "启动服务时设置 DEEPSEEK_API_KEY，即可调用真实模型"
      : "通过 DeepSeek 官方接口解析任务";
  updateButtons();
}
export function initDemo() {
  buildCompass();
  buildFrames();
  STAGES.forEach((label, i) => {
    const li = element("li");
    li.append(
      element("span", "", String(i + 1)),
      document.createTextNode(label),
    );
    $("demo-stages").append(li);
  });
  $("demo-form").onsubmit = (e) => {
    e.preventDefault();
    parseIntent();
  };
  $("demo-cancel-parse").onclick = () => {
    cancelParse();
    $("demo-source").textContent = "解析已停止 · 未生成任务";
    updateButtons();
  };
  $("demo-intent").oninput = () => {
    cancelParse();
    proposal = null;
    parsedQuery = "";
    showError();
    mission.reset();
    selectedFrame = null;
    eventCount = -1;
    render();
    $("demo-source").textContent = preset()
      ? "固定任务模板 · 未调用模型"
      : "请先通过 DeepSeek 解析新指令 · 当前演练支持红帽子人员搜索";
    renderPlan();
    updateButtons();
  };
  $("demo-start").onclick = () => {
    if (
      mission.running ||
      parsing ||
      (!preset() && parsedQuery !== $("demo-intent").value.trim())
    )
      return;
    if (!proposal) $("demo-source").textContent = "固定任务模板 · 未调用模型";
    showError();
    selectedFrame = null;
    eventCount = -1;
    mission.start($("demo-scenario").value);
    render();
  };
  $("demo-pause").onclick = () => {
    mission.paused ? mission.resume() : mission.pause();
    render();
  };
  $("demo-reset").onclick = () => {
    cancelParse();
    mission.reset();
    selectedFrame = null;
    eventCount = -1;
    if (!proposal)
      $("demo-source").textContent = preset()
        ? "固定任务模板 · 未调用模型"
        : "请先通过 DeepSeek 解析新指令";
    showError();
    render();
  };
  $("demo-camera-live").onclick = () => {
    selectedFrame = null;
    render();
  };
  function view() {
    const control = location.hash === "#console";
    $("demo-console").hidden = control;
    $("demo-footer").hidden = control;
    $("control-console").hidden = !control;
    $("control-footer").hidden = !control;
    for (const [id, active] of [
      ["demo-tab", !control],
      ["console-tab", control],
    ]) {
      $(id).classList.toggle("selected", active);
      if (active) $(id).setAttribute("aria-current", "page");
      else $(id).removeAttribute("aria-current");
    }
    if (control) {
      mission.pause();
      if (parsing) {
        cancelParse();
        $("demo-source").textContent = "切换工作区，解析已停止";
      }
    }
    render();
  }
  $("demo-tab").onclick = () => {
    location.hash = "demo";
  };
  $("console-tab").onclick = () => {
    location.hash = "console";
  };
  window.addEventListener("hashchange", view);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      mission.pause();
      render();
    }
    lastTick = performance.now();
  });
  setInterval(() => {
    const now = performance.now(),
      dt = Math.min((now - lastTick) / 1000, 0.3);
    lastTick = now;
    if (mission.running && !mission.paused && !$("demo-console").hidden) {
      mission.tick(dt * Number($("demo-rate").value));
      render();
    }
  }, 100);
  renderPlan();
  view();
}
