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
