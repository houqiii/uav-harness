// A deterministic browser-only rehearsal. This module has no network or flight API.
export const PRESET_QUERY = "帮我找一个戴着红帽子的人";
export const STAGES = [
  "意图解析",
  "爬升",
  "环视扫描",
  "目标确认",
  "分段接近",
  "悬停",
];
export const PHASES = {
  idle: "等待任务",
  climb: "爬升至搜索高度",
  scan: "环视扫描",
  confirm: "目标确认",
  approach: "分段接近",
  complete: "已到达观察位置",
  not_found: "未发现目标 · 悬停等待",
  holding: "复核中断 · 悬停等待",
};

export class DemoMission {
  constructor() {
    this.reset();
  }
  reset() {
    Object.assign(this, {
      phase: "idle",
      running: false,
      paused: false,
      elapsed: 0,
      phaseTime: 0,
      altitude: 0,
      heading: 0,
      speed: 0,
      distance: null,
      travelled: 0,
      frames: [],
      events: [],
      candidate: false,
      confirmed: false,
      scenario: "found",
      leg: 0,
      legTime: 0,
      reason: "",
    });
  }
  log(title, detail) {
    this.events.push({ time: this.elapsed, title, detail });
  }
  start(scenario = "found") {
    if (!["found", "missing", "lost", "timeout"].includes(scenario))
      throw new Error("未知演练场景");
    if (this.running) throw new Error("演练已经开始");
    this.reset();
    this.scenario = scenario;
    this.running = true;
    this.phase = "climb";
    this.log("任务开始", "PX4 流程模拟 · 目标：戴红帽子的人");
    this.log("爬升", "预设搜索高度 50 m；不发送实机指令");
  }
  pause() {
    if (this.running) this.paused = true;
  }
  resume() {
    if (this.running) this.paused = false;
  }
  change(phase) {
    this.phase = phase;
    this.phaseTime = 0;
  }
  hold(reason, phase = "holding") {
    this.phase = phase;
    this.reason = reason;
    this.running = false;
    this.speed = 0;
    this.log("悬停等待", reason);
  }
  tick(seconds) {
    if (
      !this.running ||
      this.paused ||
      !Number.isFinite(seconds) ||
      seconds <= 0
    )
      return;
    // Subdivide time so accelerated playback cannot skip a capture or review.
    let remaining = Math.min(seconds, 60);
    while (remaining > 0 && this.running) {
      const dt = Math.min(0.1, remaining);
      remaining -= dt;
      this.elapsed += dt;
      this.phaseTime += dt;
      this.advance(dt);
    }
  }
  advance(dt) {
    if (this.phase === "climb") {
      this.altitude = Math.min(50, (this.phaseTime / 4) * 50);
      if (this.phaseTime >= 4) {
        this.altitude = 50;
        this.change("scan");
        this.log("开始环视", "每 30° 采集一张示意帧，共 12 个方向");
      }
    } else if (this.phase === "scan") {
      this.heading = Math.min(359.9, (this.phaseTime / 13.2) * 360);
      while (
        this.frames.length < 12 &&
        this.phaseTime >= this.frames.length * 1.1
      ) {
        const i = this.frames.length;
        this.frames.push({
          id: i + 1,
          heading: i * 30,
          capturedAt: this.elapsed,
          status: "pending",
          found: false,
          readyAt: this.elapsed + 1.4 + (i % 3) * 0.4,
        });
        this.log(
          "采集画面",
          `${i * 30}° · 帧 ${String(i + 1).padStart(2, "0")} · 等待预设分析`,
        );
      }
      for (const f of this.frames) {
        if (f.status === "pending" && this.elapsed >= f.readyAt) {
          f.found = f.id === 5 && this.scenario !== "missing";
          f.status = f.found ? "candidate" : "clear";
          if (f.found) {
            this.candidate = true;
            this.log("发现候选", "120° · 戴红帽子的人（预设识别结果）");
          }
        }
      }
      if (
        this.frames.length === 12 &&
        this.frames.every((f) => f.status !== "pending")
      ) {
        if (!this.candidate)
          this.hold("12 个方向均未发现匹配目标（预设场景）", "not_found");
        else {
          this.change("confirm");
          this.log("转向复核", "扫描结束，转向 120° 并补拍确认");
        }
      }
    } else if (this.phase === "confirm") {
      this.heading = 120;
      if (this.phaseTime >= 2.5) {
        this.confirmed = true;
        this.distance = 80;
        this.change("approach");
        this.log("目标已确认", "水平距离 80 m（模拟真值）；保持 50 m 高度");
        this.log("分段接近", "每段 10 m，停稳后复核；观察距离 20 m");
      }
    } else if (this.phase === "approach") {
      this.legTime += dt;
      const fraction = Math.min(1, this.legTime / 2);
      this.distance = 80 - this.leg * 10 - fraction * 10;
      this.travelled = 80 - this.distance;
      this.speed = this.legTime < 2 ? 5 : 0;
      if (this.legTime >= 3.2) {
        this.leg++;
        this.legTime = 0;
        this.distance = 80 - this.leg * 10;
        this.travelled = this.leg * 10;
        this.speed = 0;
        if (this.leg === 2 && ["lost", "timeout"].includes(this.scenario)) {
          this.hold(
            this.scenario === "lost"
              ? "目标丢失，停止接近；等待人工处理（预设场景）"
              : "视觉分析超时，停止接近；等待人工处理（预设场景）",
          );
          return;
        }
        this.log(
          "图像复核",
          `同一目标再次确认 · 水平距离 ${this.distance} m（预设结果）`,
        );
        if (this.distance <= 20) {
          this.change("complete");
          this.running = false;
          this.log(
            "任务完成",
            "距离目标水平 20 m · 高度 50 m · 保持悬停（模拟）",
          );
        }
      }
    }
  }
}
