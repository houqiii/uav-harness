export const $ = (id) => document.getElementById(id);
export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export const jobLabels = {
  queued: "等待执行",
  running: "执行中",
  cancelling: "正在收尾",
  succeeded: "已完成",
  failed: "执行失败",
  unknown: "结果不确定",
  cancelled: "已取消",
  reconciled: "已核实地面状态",
  not_executed: "未执行",
};
export const actionLabels = {
  "bench.motor_test": "单电机测试",
  "bench.takeoff_probe": "起飞探针",
  "vehicle.arm": "普通解锁",
  "vehicle.disarm": "普通解除武装",
  "flight.takeoff": "起飞",
  "flight.land": "降落",
  "flight.move_relative": "相对移动",
  "flight.hold": "悬停",
  "flight.yaw": "调整航向",
};
export const activeJob = (job) =>
  ["queued", "running", "cancelling"].includes(job.status);
export function notice(text, info = false) {
  $("notice").textContent = text;
  $("notice").className = "notice" + (info ? " info" : "");
  $("notice").hidden = !text;
}
export async function request(
  path,
  { body, key, method, timeout = 8000 } = {},
) {
  const headers = {};
  const token = sessionStorage.getItem("uav.apiToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
    cache: "no-store",
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`服务返回非 JSON 响应（${response.status}）`);
  }
  if (!response.ok) {
    const text =
      response.status === 401
        ? "需要访问令牌，请打开“连接设置”"
        : response.status === 404 &&
            path.startsWith("/v1/sandbox") &&
            data.detail === "Not Found"
          ? "当前服务没有沙盒接口；请用 bash scripts/mac-dashboard.sh 重启服务后刷新页面"
          : data.error ||
            (Array.isArray(data.detail)
              ? data.detail.map((e) => e.msg).join("；")
              : data.detail) ||
            `HTTP ${response.status}`;
    const error = new Error(text);
    error.status = response.status;
    throw error;
  }
  return data;
}

export async function previewIntent(
  query,
  onProgress,
  signal,
  path = "/v1/intents/preview-stream",
) {
  const headers = {
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
  };
  const token = sessionStorage.getItem("uav.apiToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  const deadline = AbortSignal.timeout(200000);
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  deadline.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  let reader;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers,
      body: JSON.stringify({ query, execute: false }),
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(
        response.status === 401
          ? "需要访问令牌，请打开“连接设置”"
          : body.error || `HTTP ${response.status}`,
      );
    }
    if (!response.body) throw new Error("浏览器未提供响应流");
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 1048576) throw new Error("响应流过大，已停止生成");
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop();
      for (const block of blocks) {
        const data = block
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!data) continue;
        const event = JSON.parse(data);
        if (event.type === "status") onProgress(event);
        else if (event.type === "error") throw new Error(event.error);
        else if (event.type === "done") return event;
      }
    }
    throw new Error("生成连接在完整计划返回前关闭；未执行任何指令");
  } finally {
    signal?.removeEventListener("abort", abort);
    deadline.removeEventListener("abort", abort);
    await reader?.cancel().catch(() => {});
  }
}
