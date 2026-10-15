import {
  $,
  element,
  request,
  notice,
  actionLabels,
  jobLabels,
  activeJob,
  previewIntent,
} from "./api.js";
let data = null,
  online = false,
  plan = null,
  origin = "",
  compiling = false,
  submitting = false,
  requestKey = null,
  submitted = false,
  selectedJob = null,
  selectedDetail = null,
  jobIds = "",
  previewVersion = 0,
  previewController = null;
const storageKey = "uav.pendingSubmission";
const name = (action) => actionLabels[action.action] || action.action;
function renderPlan() {
  const target = $("plan-content");
  target.replaceChildren();
  if (!plan)
    target.append(element("p", "empty-copy", "输入任务，生成可执行的调度计划"));
  else
    for (const [index, action] of plan.actions.entries()) {
      const row = element("div", "plan-row");
      row.append(
        element("span", "plan-number", String(index + 1).padStart(2, "0")),
      );
      const detail = element("div");
      detail.append(element("strong", "", name(action)));
      const p = action.params;
      const params =
        action.action === "bench.motor_test"
          ? `${p.pwm_us} μs · ${p.duration_s} s · 电机 ${p.motor_sequence}`
          : Object.entries(p)
              .map(([k, v]) => `${k}=${v}`)
              .join(" · ");
      detail.append(
        element(
          "p",
          "",
          `${action.vehicle_id}${params ? " · " + params : ""}${action.depends_on.length ? " · 依赖 " + action.depends_on.join(", ") : ""}`,
        ),
      );
      row.append(detail);
      target.append(row);
    }
  $("plan-json").textContent = plan ? JSON.stringify(plan, null, 2) : "";
  $("plan-source").textContent = origin;
  $("json-button").disabled = !plan;
  $("step-intent").classList.toggle("complete", Boolean(plan));
  $("step-validate").classList.toggle("complete", Boolean(plan));
  $("step-dispatch").classList.toggle("complete", submitted);
  updateButtons();
}
function updateButtons() {
  const ready =
    online &&
    data?.control_enabled &&
    plan?.actions.every(
      (a) =>
        data.vehicles.find((v) => v.vehicle_id === a.vehicle_id)?.connected,
    );
  $("intent").disabled = submitting || Boolean(requestKey && !submitted);
  $("execute-button").disabled = !ready || submitting || submitted || compiling;
  $("execute-button").textContent =
    data?.mode === "offline"
      ? "离线推演 · 不执行"
      : submitting
        ? "正在提交…"
        : submitted
          ? "已提交调度"
          : requestKey
            ? "查询 / 重试本次提交"
            : "执行计划";
  $("compile-button").disabled =
    compiling ||
    submitting ||
    Boolean(requestKey && !submitted) ||
    !online ||
    !data?.compiler.configured;
  $("compile-button").textContent = compiling ? "正在翻译…" : "生成调度计划";
  $("cancel-preview").hidden = !compiling;
  document
    .querySelectorAll("[data-example]")
    .forEach(
      (b) =>
        (b.disabled =
          compiling ||
          submitting ||
          Boolean(requestKey && !submitted) ||
          !online),
    );
}
function invalidatePlan() {
  previewVersion++;
  plan = null;
  origin = "";
  requestKey = null;
  submitted = false;
  $("plan-json").hidden = true;
  $("json-button").setAttribute("aria-expanded", "false");
  renderPlan();
}
export function updateWorkspace(snapshot, connected) {
  data = snapshot;
  online = connected;
  if (data?.mode === "offline" && requestKey) {
    // Keep an unresolved hardware submission in sessionStorage for a later real
    // connection, but do not let it lock an unrelated offline planning session.
    requestKey = null;
    plan = null;
    origin = "";
    submitted = false;
    renderPlan();
  }
  $("compiler-note").textContent = !online
    ? "服务断开，等待重新连接"
    : data.compiler.configured
      ? "DeepSeek 已配置 · 生成计划后可查看并执行"
      : "DeepSeek 未配置：自然语言翻译暂不可用；快捷示例可生成固定测试计划。";
  const offline = data?.mode === "offline";
  $("offline-capabilities").hidden = !offline;
  $("planning-context").textContent = JSON.stringify(
    data?.planning_context || [],
    null,
    2,
  );
  document.querySelectorAll("[data-example]").forEach((button) => {
    const bench = ["motor", "probe"].includes(button.dataset.example);
    button.hidden = offline ? bench : !bench;
  });
  $("intent").placeholder = offline
    ? "例如：ap-1 起飞2米，向机头前进3米，悬停2秒后降落"
    : "例如：让模型机的 1 号电机转动 5 秒";
  $("job-empty").querySelector("p").textContent = offline
    ? "离线推演仅生成计划，不派发任务或产生飞控回执"
    : "任务执行后，回执和结果将在这里显示";
  if (online && offline) {
    $("compiler-note").textContent = data.compiler.configured
      ? `${data.compiler.provider} / ${data.compiler.model} · ${data.compiler.endpoint} · 不发送指令`
      : "离线界面已就绪。请在启动终端设置 DEEPSEEK_API_KEY 后重启，才能调用真实模型。";
  }
  const jobs = data?.jobs || [];
  if (requestKey) {
    const found = jobs.find((j) => j.request_key === requestKey);
    if (found) {
      submitted = true;
      requestKey = null;
      sessionStorage.removeItem(storageKey);
      selectedJob = found.job_id;
      renderPlan();
    }
  }
  const signature = JSON.stringify(jobs.map((j) => [j.job_id, j.status]));
  if (signature !== jobIds) {
    jobIds = signature;
    renderJobs(jobs);
  }
  if (!selectedJob && jobs.length) {
    selectedJob = jobs[0].job_id;
    renderJobs(jobs);
  }
  $("job-empty").hidden = jobs.length > 0;
  $("job-count").textContent = jobs.length ? `最近 ${jobs.length} 项` : "";
  $("job-detail").hidden = !selectedJob;
  $("cancel-button").disabled =
    !online ||
    !selectedDetail ||
    !activeJob(selectedDetail) ||
    selectedDetail.status === "cancelling";
  $("reconcile-button").disabled = !online;
  updateButtons();
}
function renderJobs(jobs) {
  $("job-list").replaceChildren(
    ...jobs.map((job) => {
      const button = element(
        "button",
        "job-item" + (selectedJob === job.job_id ? " selected" : ""),
      );
      const title = element(
        "span",
        "job-title",
        job.plan.query || job.plan.actions.map(name).join(" → "),
      );
      title.append(
        element(
          "span",
          "job-time",
          new Date(job.created * 1000).toLocaleTimeString("zh-CN", {
            hour12: false,
          }) +
            " · " +
            job.plan.actions.length +
            " 个动作",
        ),
      );
      button.append(
        title,
        element(
          "span",
          "job-badge " + job.status,
          jobLabels[job.status] || job.status,
        ),
      );
      button.onclick = () => {
        selectedJob = job.job_id;
        selectedDetail = null;
        renderJobs(data.jobs);
        pollJob().catch((e) => notice(e.message));
      };
      return button;
    }),
  );
}
export async function pollJob() {
  if (!online || !selectedJob) return;
  const id = selectedJob,
    detail = await request(`/v1/jobs/${encodeURIComponent(id)}`);
  if (id !== selectedJob) return;
  selectedDetail = detail;
  $("job-detail").hidden = false;
  $("job-status").textContent =
    `${jobLabels[detail.status] || detail.status} · ${detail.job_id.slice(0, 16)}`;
  $("cancel-button").hidden = !activeJob(detail);
  $("reconcile-button").hidden = detail.status !== "unknown";
  $("cancel-button").disabled = !online || detail.status === "cancelling";
  const lines = [];
  for (const event of detail.events || []) {
    const at = new Date(event.at * 1000).toLocaleTimeString("zh-CN", {
      hour12: false,
    });
    if (event.kind === "command_sent")
      lines.push(
        `[${at}] TX  command=${event.command} params=${JSON.stringify(event.params)}`,
      );
    else if (event.kind === "command_ack")
      lines.push(
        `[${at}] ACK command=${event.message.command} result=${event.message.result} (${event.message.result === 0 ? "ACCEPTED" : "未接受"})`,
      );
    else if (event.error) lines.push(`[${at}] ${event.kind}: ${event.error}`);
  }
  for (const action of detail.plan.actions) {
    const result = detail.results[action.action_id];
    if (result)
      lines.push(
        `${action.action_id} · ${jobLabels[result.status] || result.status}${result.error ? " · " + result.error : ""}${result.physical_rotation_verified === false ? " · 已验证输出遥测，物理转动需现场观察" : ""}`,
      );
  }
  if (detail.status === "unknown")
    lines.push(
      "结果不确定：未自动重试。请核实设备状态并按项目流程 reconcile。",
    );
  $("job-events").replaceChildren(
    ...lines
      .slice(-100)
      .map((line) =>
        element(
          "div",
          "event-line" + (/不确定|失败|error/.test(line) ? " error" : ""),
          line,
        ),
      ),
  );
}
export function initWorkspace() {
  $("reconcile-button").onclick = async () => {
    if (!selectedJob) return;
    $("reconcile-button").disabled = true;
    try {
      await request(`/v1/jobs/${encodeURIComponent(selectedJob)}/reconcile`, {
        method: "POST",
      });
      notice(
        "已核实新鲜地面 / 未解锁状态，原任务未重放；若曾丢失 ACK，请重启服务后再复用该命令。",
        true,
      );
      await pollJob();
    } catch (error) {
      notice(error.message);
    } finally {
      $("reconcile-button").disabled = !online;
    }
  };
  $("cancel-preview").onclick = () => previewController?.abort();
  $("intent").addEventListener("input", () => {
    previewController?.abort();
    invalidatePlan();
    notice("");
  });
  $("json-button").onclick = () => {
    const open = $("plan-json").hidden;
    $("plan-json").hidden = !open;
    $("json-button").setAttribute("aria-expanded", String(open));
  };
  $("intent-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = $("intent").value.trim();
    if (!query || compiling || !online || !data?.compiler.configured) return;
    invalidatePlan();
    const version = previewVersion;
    compiling = true;
    updateButtons();
    notice("");
    previewController = new AbortController();
    const started = performance.now();
    $("compile-status").hidden = false;
    $("compile-progress").textContent = "正在连接 DeepSeek 官方…";
    try {
      const phases = {
        connecting: "正在连接 DeepSeek 官方",
        waiting: "DeepSeek 已接受请求，等待模型输出",
        reasoning: "模型正在推理",
        generating: "正在生成调度计划",
      };
      const result = await previewIntent(
        query,
        (event) => {
          $("compile-progress").textContent =
            `${phases[event.phase] || "正在生成"} · ${Math.round(event.elapsed_s || 0)} 秒`;
        },
        previewController.signal,
      );
      if (version === previewVersion) {
        plan = result.plan;
        $("compile-progress").textContent =
          `计划生成完成 · ${((performance.now() - started) / 1000).toFixed(1)} 秒 · 未发送指令`;
        origin =
          data?.mode === "offline"
            ? "DeepSeek 真实翻译 · 离线能力校验通过 · 未发送指令"
            : "DeepSeek 翻译 · 能力校验通过";
        if (data?.mode === "offline") {
          $("plan-json").hidden = false;
          $("json-button").setAttribute("aria-expanded", "true");
        }
        renderPlan();
      }
    } catch (error) {
      if (error.name === "AbortError")
        $("compile-progress").textContent = "已停止生成 · 未发送指令";
      else {
        $("compile-progress").textContent =
          `未生成计划 · ${((performance.now() - started) / 1000).toFixed(1)} 秒`;
        notice(error.message);
      }
    } finally {
      previewController = null;
      compiling = false;
      updateButtons();
    }
  };
  document.querySelectorAll("[data-example]").forEach(
    (button) =>
      (button.onclick = async () => {
        const vehicle = data?.vehicles.find(
          (v) => v.vehicle_id === $("vehicle-select").value,
        );
        const examples = {
          movement: `${vehicle?.profile === "flight" ? vehicle.vehicle_id : data.vehicles.find((v) => v.profile === "flight")?.vehicle_id || "ap-1"} 起飞2米，向机头前进3米，再后退1米，悬停2秒后降落`,
          mixed:
            "让 ap-1 和 px4-1 分别起飞2米，各向东移动1米，等两架都到达后一起降落；不要操作 model-1",
          limits:
            "让 model-1 向前飞3米，再让 ap-1 起飞到120米；如果能力不支持，请拒绝生成计划",
        };
        if (button.dataset.example in examples) {
          $("intent").value = examples[button.dataset.example];
          invalidatePlan();
          notice("");
          return;
        }
        if (!vehicle || vehicle.profile !== "model_bench") {
          notice("这两个示例适用于模型机；请选择 model_bench 机型。");
          return;
        }
        const motor = button.dataset.example === "motor";
        const query = motor
          ? `让 ${vehicle.vehicle_id} 的 1 号电机以 1231 微秒转动 5 秒`
          : `对 ${vehicle.vehicle_id} 进行 1.5 米起飞指令探针测试，并完成降落收尾`;
        $("intent").value = query;
        invalidatePlan();
        notice("");
        if (data.compiler.configured) return;
        const version = previewVersion;
        const candidate = {
          query,
          actions: [
            {
              action_id: motor ? "motor" : "probe",
              vehicle_id: vehicle.vehicle_id,
              action: motor ? "bench.motor_test" : "bench.takeoff_probe",
              params: motor
                ? { motor_sequence: 1, pwm_us: 1231, duration_s: 5 }
                : { altitude_home_m: 1.5 },
              depends_on: [],
              timeout_s: motor ? 20 : 60,
            },
          ],
        };
        try {
          const response = await request("/v1/plans/validate", {
            body: candidate,
          });
          if (version === previewVersion) {
            plan = response.plan;
            origin = "固定测试计划 · 未调用模型";
            renderPlan();
          }
        } catch (error) {
          notice(error.message);
        }
      }),
  );
  $("execute-button").onclick = async () => {
    if (!plan || submitting || submitted) return;
    requestKey ||= crypto.randomUUID();
    sessionStorage.setItem(
      storageKey,
      JSON.stringify({ plan, key: requestKey }),
    );
    submitting = true;
    updateButtons();
    notice("");
    try {
      const job = await request("/v1/plans", { body: plan, key: requestKey });
      submitted = true;
      sessionStorage.removeItem(storageKey);
      selectedJob = job.job_id;
      selectedDetail = job;
      renderPlan();
      notice("计划已提交，正在跟踪飞控回执和遥测结果。", true);
    } catch (error) {
      if (error.status) {
        requestKey = null;
        sessionStorage.removeItem(storageKey);
        notice(error.message);
      } else
        notice(
          "提交响应未确认。可点击“查询 / 重试本次提交”，系统将使用同一幂等键，不重复派发已接收任务。",
        );
    } finally {
      submitting = false;
      updateButtons();
    }
  };
  $("cancel-button").onclick = async () => {
    if (!selectedJob) return;
    $("cancel-button").disabled = true;
    try {
      await request(`/v1/jobs/${encodeURIComponent(selectedJob)}/cancel`, {
        method: "POST",
      });
      notice("已请求取消，等待控制器停止或降落收尾。", true);
      await pollJob();
    } catch (error) {
      notice(error.message);
    }
  };
  try {
    const pending = JSON.parse(sessionStorage.getItem(storageKey));
    if (pending?.plan?.actions && pending.key) {
      plan = pending.plan;
      requestKey = pending.key;
      $("intent").value = plan.query;
      origin = "恢复待确认提交 · 重试沿用原幂等键";
      renderPlan();
    }
  } catch {
    sessionStorage.removeItem(storageKey);
  }
}
