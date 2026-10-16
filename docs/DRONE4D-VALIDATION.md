# Drone4D 接入验证记录

日期：2026-10-16。客户端 macOS ARM，Drone4D 源码 `b4d83e3`，公开服务 `https://drone4d.agentnetwork.org.cn`，服务构建 `20261014-153115-e57d6be2c8`。模型为百炼官方 `qwen3-vl-plus`；没有模拟模型回复。

## 已验证

- 本地全套 Python 检查 **120 passed**；覆盖原有 Harness、VLM 闭环和新增沙盒适配。存在一项 Starlette/httpx 弃用提示。
- 原生前端构建、TypeScript 检查通过；启动脚本能够复用匹配的构建。源码和桥接哈希写入本地构建清单。
- 公开服务成功创建独立会话；实际仿真遥测确认地面初始化、起飞到 50 m、30° 分步转向和位置保持。
- 浏览器原生 FPV 采集成功，包含拍摄时相机标定、姿态和时间。真实 Qwen 调用得到扫描、候选目标和变焦提议；没有把场景目标坐标提供给模型。
- 运行中切换实机页没有改变沙盒任务的执行器。未接实机时，实机页不提供虚构遥测或执行能力。移动尺寸曾检查无横向溢出；固定 1280×720 渲染调整后的移动回归尚待复验。
- 创建额度不足时按 Retry-After 等待；初始化失败保留会话；已有有效会话可在本地服务重启后复用。停止过程中排空迟到的 SDK 指令，再确认保持状态。
- 实测发现原生页面续期会使 SDK 的旧 token 失效，已改为统一维护和持久化当前 token；REST 与续期串行，WS 重连使用当前 token。自动化测试验证了新旧浏览器凭据、最新上游凭据和落盘结果。修复后已持续在线运行并跨过令牌续期，REST、原生视图和命令继续工作。

## 尚未完成的端到端验收

**未取得一条最终成功的“起飞 → 环视 → 视觉确认 → 分段接近 → 到达观察间距 → 悬停”运行记录。** 不应据此声称整个任务已经跑通。

实际测试曾遇到树冠遮挡、远距离人物像素过小、模型目标语义漂移和会话 token 失效。已调整开阔道路场景、提供光学变焦、固定采集分辨率，并将初次接受的任务目标作为每轮明确输入。树冠遮挡时模型没有得到可靠脚点，控制器拒绝接近，属于预期行为。

后续在用户打开的 Chrome 页面恢复了原生渲染和真实 Qwen 规划，验证了接近动作与遥测位移。当前更新为 300 m 以外起步、最终三维距离不超过 50 m 的远距任务，本轮尚未取得完整成功记录。

## 继续验收

1. 启动 `bash scripts/mac-sandbox.sh --http-port 8768`，手动打开 `http://127.0.0.1:8768/#sandbox`。
2. 连接沙盒，等待会话、飞机和原生渲染器均就绪。有效凭据会复用已有会话。若连接中断触发上游安全落地，等安全动作完成后再试，不强制解除 SAFETY_ACTIVE。
3. 输入“帮我找一个戴着红帽子的人，接近到观察位置后悬停”，使用“按自然语言理解”启动。
4. 核对起飞实际高度、扫描覆盖、两次独立视觉确认、足点几何、每次不超过 30 m 的实际位移，以及最终三维距离和实际悬停状态。另测仅寻找、未发现、遮挡、目标丢失、取消和断线恢复。
5. 任务跨续期运行，确认 REST、WS、图像和命令均继续工作。PX4 SITL/HIL 与实机仍需独立验收；当前在线环境使用 FleetSim mock 物理后端。

## 远距版本新增检查

- 自动化验证：探索移动可在尚未发现人物时由模型提出，执行前检查活动范围和已知人物最小间隔；水平 40 m、高度 50 m 不满足 50 m 三维距离。
- 独立评测验证：仅放大相机无法通过距离验收；实际遥测位移计入航程，目标真值坐标不进入模型观测。
- 场景重置按钮在线通过，复用会话 `sb-ew2pnra3b2`，没有额外创建会话。种子 17 的实际初始人物距离为 333.97 m。
- 远距在线实测：Qwen 自主起飞至 50 m 并进行分步环视，第 13 轮因 API ConnectTimeout 终止，已确认悬停；实际人物距离仍约 337.7 m，因此没有通过远距验收。随后增加网络超时的有限重试（每次重新采图），相关 27 项测试通过。完整远距接近仍需在线复验。

## 约 200 米与双视角版本

按用户要求，默认人物改为距起点约 200 m（种子 17 实测 205.28 m），独立评测起点范围同步为 180–230 m；最终仍按三维距离 ≤50 m 验收。没有修改模型识别策略或加入目标特异规则。五处候选位置在线查询 DTM 为 0 m，DSM 不超过 0.01 m。

原生前端构建通过，相关 Python 测试 44 项通过。Chrome 桌面 1440 宽页面验证了同屏 FPV、第三人称跟随、真实 Qwen 观测摘要和计划；历史轮次可展开，包含帧编号与动作。原生第三人称不领取采集请求，不向模型供图。实测起飞 50 m、转向、暂停悬停及切页恢复；页面无横向溢出，浏览器无应用错误。小屏自动换行，未实测移动端。此轮是布局与接口验证，未宣称完整目标接近任务成功。

### 2026-10-16: unobstructed 150 m search placement

The north-side candidate `[-10, -190]` was present in the scene but occluded.
The native `segment_los` query from launch ENU `[-31.9, -337.1, 50]` to
`[-10, -190, 1.7]` returned `visible=false`, with the first obstruction near
`[-13.28, -212, 8.92]`. This was a scenario visibility failure, not evidence
of a missing person mesh or a VLM recognition failure.

Retained only west-side positions `[-180, -300]`, `[-175, -300]`, and
`[-185, -300]`. Native `ground_dtm` returned 0 m at all three positions;
`height_dsm` returned approximately 0.01, 0, and 0 m. With `eps_m=0.1`,
`segment_los` returned true for all six rays from the launch position at
50 m altitude to each candidate's feet (+0.1 m) and head (+1.7 m).
World content version: `9e05c2364f58`; derive hash: `d98119e8`.

These are scene-authoring checks only. Target coordinates and visibility
oracle results are not supplied to the planner. Person dimensions, initial
camera direction, model prompts, and autonomous action selection are unchanged.
This verifies native DSM line of sight; it does not establish VLM recognition
success or a completed online mission. Restart the backend and create/reset
the sandbox to apply the new placements to an existing session.

### 2026-10-16: isolated replay of model validation failure

Replayed the saved final observation from mission
`d4bfbf5ca00445aa9b936b38f5469d5d` (frame
`7b952ddea00240c899599b0b11fd9bb9`), with recorded action/result context.
No sandbox commands were dispatched. This is a reproduction, not recovery of
an originally discarded response.

The first live Qwen call with the previous prompt reproduced `target.bbox`
validation failure: `[1150, 320, 1160, 330]` was expressed in pixels for a
1280×720 image, while the internal contract required 0–1000 coordinates.
The second call with the explicit coordinate-space schema returned a valid
pixel box and a valid gimbal action. The adapter normalized the box to
`[898.4375, 416.6667, 910.15625, 444.4444]` before internal validation.

The model-facing schema now requests `bbox_coordinate_space` (`pixels` or
`normalized_1000`). Only explicitly tagged pixels are converted. Untagged
legacy boxes retain the normalized convention; ambiguous or out-of-bounds
boxes are rejected, never guessed or clamped. Unit tests cover conversion,
legacy rejection and bounds enforcement. This verifies the format fix,
not task completion or correctness of the proposed camera motion.

### 2026-10-16: candidate inspection before further pitch/search

Inspected cancelled mission `d339e20e33a04bcb92b1210e431bc3c6`.
Rounds 8 and 9 described a tiny colored candidate but returned `target=null`
and commanded pitch -25 then -35 degrees. Round 10 lost the candidate and
resumed yaw scanning. Missing structured detections also prevented candidate
bearing memory from being populated.

Updated the planner instructions to record tentative detections, inspect a
localized task-relevant color patch before dismissing it as noise, prefer
modest zoom that preserves the candidate in frame, and avoid using downward
pitch as a substitute for magnification. A centring correction remains allowed
when zoom would crop the candidate. A post-zoom image counts toward existing
confirmation requirements; approach still requires current grounded evidence.
No scene coordinates or scripted motion were added to model context.

Three isolated live Qwen calls used saved original images and reconstructed
prior action/result context, without dispatching any flight action. The initial
prompt revision selected zoom 2x with a tentative bbox on round 9, but still
dismissed the round-8 patch. After clarifying that an unresolved relevant color
patch must first be inspected, replay of round 8 selected zoom 2x and recorded
its tentative bbox. This tests candidate inspection decisions, not a completed
closed-loop approach. The revised prompt does not guarantee all future model
outputs; geometric approach guards remain in force.
