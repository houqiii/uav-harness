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

### 2026-10-16: calibrated candidate centring and zoom bounds

Mission `3f93b4133ac34c4f8af979c1949a1dcb` demonstrated two independent
control issues. At 2x zoom the recorded box was near the right edge
(`[890.625,722.2222,902.34375,750]` normalized). Increasing to 3x put its
projected right edge beyond the image. Subsequent gimbal commands copied
world pitch -22.5 degrees directly into the native body-relative actuator;
observed camera pitch was about -25.7 to -26.5 degrees rather than -22.5.

Added model-selected `aim_target`: the runner uses the current recorded
candidate's calibrated image ray (or a recent last bearing while stationary),
then the Drone4D adapter transforms the world look vector through inverse
body attitude into native FLU gimbal azimuth/elevation. This matches the
P600 GX40 zero-rotation mount and Drone4D's Rz(azimuth) Ry(-elevation)
convention. The planner chooses whether to aim, zoom or approach; no target
truth or predetermined flight route is supplied. Raw gimbal actions retain
their prior actuator semantics. Hardware does not advertise this new tool.

Observations include measured optical zoom for the native GX40. A proposed
zoom that would crop the currently detected box is rejected with specific
centring feedback. Tests verify the recorded 2x-to-3x crop, calibrated ray
projection to image centre, last-bearing reacquisition, and attitude
compensation using an independent quaternion-vector rotation calculation.

One isolated live Qwen replay of the saved round-10 image, with recorded
prior action/result context and the new capability, selected `aim_target`
and a tentative bbox instead of another zoom. No flight command was
executed during replay. Full native camera-motion/approach success remains
to be verified online; this record does not claim an end-to-end pass.

### 2026-10-16: active perception before visible-foot confirmation

Cancelled mission `c57394d7456f41d28cd98bc3a62dcd2b` kept a plausible red/white
candidate visible while rounds 11–15 repeatedly selected aim_target. All
reported no visible ground contact, which blocked the existing approach tool.
Aiming could not add the missing image detail. Spatial confirmation also
varied as the tiny-box estimates oscillated between views.

Added model-selected `inspect_target(distance_m<=15)` under an explicit
candidate-inspection policy, enabled by default for the Drone4D sandbox only.
It needs a current magnified (>=1.5x) candidate detection, an airborne approach
mission, calibrated ground and a usable downward ray; it does not require
feet or a two-observation confirmation. It makes a horizontal movement then
returns to the normal fresh-image loop. Find-only goals and lost detections
cannot use it. No target truth is used.

The step is bounded by the requested distance, policy step, geofence, altitude
and a conservative horizontal-range lower bound. For this sandbox's ground
person scenario, the bound subtracts ground uncertainty and 3m possible entity
height, adds 2 degrees of ray error, and retains the horizontal standoff.
This is an explicit scenario assumption, not a range measurement or generic
hardware guarantee. Inspection does not populate target geometry and cannot
satisfy final monitoring-distance acceptance. Normal grounded approach and
finish requirements remain unchanged.

Replaced accumulated prompt rules with ordered action priorities and added
feedback about consecutive camera-only actions. Three isolated live calls
used the saved round-14 frame and reconstructed action/result context:
the first instruction revision still chose aim_target, the compact prompt
chose zoom 3x, and with explicit active-perception feedback the model chose
inspect_target(15). Grounding yielded a horizontal NED offset approximately
[3.477, -14.591, 0]. No native flight action was dispatched during replay.
Regression tests exercise simulated movement, missing-feet acceptance,
disabled policy, find-only goals, unzoomed/lost detections, horizon, near-target
and geofence rejection. This is not an end-to-end native mission success.

### 2026-10-16: search rotation must not become exploration translation

The latest cancelled run selected search_move(120deg,30m) and then
search_move(150deg,30m) in rounds 2 and 3 while reporting target=null.
This was the separately advertised free exploration capability, not
inspect_target bypassing its detection requirement.

Added allow_search_reposition=false as the default policy. The native adapter
omits search_move unless explicitly enabled, and the runner independently
rejects it under the default policy even if a stale/misbehaving planner asks
for it. No-current-candidate search changes the viewing direction in place;
inspect_target still requires a current magnified candidate and approach
retains its grounded detection requirements. Optional research exploration
remains available only with explicit policy opt-in.

One isolated live replay of the saved round-2 image and preceding result
selected a gimbal adjustment with target=null; the resulting command has no
translation. Regression tests verify server-side rejection with a stale
search_move capability, yaw execution preserving position, rejection of
candidate inspection without a detection, and continued positive coverage
of magnified-candidate inspection. 53 related tests passed. No native flight
command was sent by the replay.

### 2026-10-16: candidate-first planning and native yaw emergency descent

Inspected mission 60d22710a6d546109484795d6bb3e5e9 and the existing
session's native events without creating/resetting a session. No descent
command occurred: round 9 reached heading 270 at ~50m altitude; round 10
asked for yaw 90 to finish a scan checklist despite describing a small red
candidate. Native event 273 reports SAF.CTRL.YAW_ERR (179.398 degrees,
threshold 90), entering ELAND/CONTROLLED. Event 274 confirms that transition;
399 records touchdown. Thus this was both an inappropriate planner scan
priority and an adapter failure to implement a large turn within native
controller constraints, not a billing/access limit or a planned descent.

The adapter now implements yaw with <=60-degree setpoint segments, waiting
for measured heading, position and speed after each segment, keeping the
original position/altitude. It does not change the model's requested final
heading or disable native safety. Tests cover 270->90, wrapped headings and
179-degree turns, and require settlement before each subsequent dispatch.

Rewrote planner instructions around visible evidence: no candidate => search
in place; localized tentative candidate => interrupt scan, centre if needed,
zoom; magnified candidate => bounded horizontal inspection; confirmed target
=> range-directed observation. Explicitly distinguish task goal from current
phase and tentative detection from identity confirmation. Historical evidence
failures are now labeled completion evidence, not general action blockers.
Removed the two-observation gate on grounded movement; current-frame geometry
and distance/fence bounds remain required. Final found acceptance retains its
confirmation and physical range checks. Missing metric geometry directs the
planner to candidate inspection rather than redundant observe rituals.

Isolated real Qwen replays, without flight dispatch:
- Saved failing round 10: now reports a tentative bounding box and aim_target,
  instead of yaw 90 to complete coverage.
- Saved magnified round 14: the first rewrite still chose zoom 3.5; after
  clarifying that the existing 2x image already satisfies magnified inspection,
  selected inspect_target(15), no visible feet required. Grounded command was
  NED approximately [4.023,-14.451,0], i.e. current candidate bearing with
  zero vertical displacement. This uses the real stored image, not scene truth.

Full regression suite: 136 passed. These replays and adapter regression tests
are not a completed end-to-end native mission. Existing repository-wide lint
style findings (imports/modernization, etc.) remain outside this fix.
- Saved empty round 2 under the final prompt: target=null and yaw(120),
  preserving position rather than inventing candidate-directed translation.

### 2026-10-16: stop repeating completed centring before magnification

Retrieved the user's cancelled 16-round run (5cc2298ea70a4de2bff67fafdad15108)
and stored its native frames locally for isolated replay. Rounds 9–15 all
selected aim_target, all measured gimbal operations succeeded, and zoom
remained 1.0 throughout. After initial pointing, the VLM repeatedly treated
small, changing bbox offsets as reasons to restart “centre then zoom”. No
camera command failure caused this loop. Existing context included the
nested SDK UNVERIFIED acceptance receipt alongside measured completion,
and did not quantify how much zoom was possible without exact centring.

Added a calibrated zoom envelope using the current proposal's bbox and camera
principal point, retaining a 10% image margin. Successful native gimbal results
now explicitly report sensor-confirmed pointing and unchanged resolution.
Planner history uses measured results rather than nested transport receipts,
and reports when the preceding aim already completed.

A proposal check returns feedback to the same VLM on the same image if it
proposes another centring action after successful pointing when magnification
is already possible. An oversized zoom receives its computed maximum. A
magnified, still-visible candidate can receive a bounded-inspection suggestion
only when the existing inspection geometry permits it. First pointing, edge
candidates, aircraft displacement, missing current detections and disabled
inspection retain their distinct behavior. No candidate, tool, flight direction
or successful action is inserted by the checker. The VLM must issue a revised
Decision. At most two corrections are allowed within the original 90-second
budget, after which no action is dispatched. Existing freshness, range and
completion checks still apply.

Real Qwen calls on stored images, without native flight dispatch:
- This run's round 10 selected zoom 1.5 after one same-frame correction for an
  oversized zoom, instead of executing another aim_target.
- This run's round 15 selected zoom 2.0 directly, no correction required.
- Previously saved magnified round 14 selected inspect_target(10), with feet
  unresolved; the controller produced NED [2.361,-9.717,0], a horizontal step.
These are isolated action-selection results, not an end-to-end mission claim.

Regression: 141 tests passed. Added coverage for same-frame VLM correction,
centred-but-not-perfect boxes, edge clipping, real displacement, absent current
detections, disabled inspection, missing ground information, bounded correction
attempts and zoom-envelope limits. git diff --check passed.

### 2026-10-16: remove strategy overrides and fix image-coordinate ambiguity

This section supersedes the zoom-envelope/same-frame correction mechanism above.
The user requested a clean harness whose planning does not depend on this scene.
Removed the computed maximum-zoom suggestion, its 10% margin calculation, all
same-image semantic reprompting, and the 1.5x inspection permission threshold.
The VLM now returns one decision per observation; history feedback reports facts
only. Device bounds, current-image provenance, physical range/fence checks and
geometric crop validation remain. No target position, color-specific detector,
or scenario-specific zoom/heading is inserted into runtime planning.

The cancelled run `2f78d964402e44adb2ed52c3c57bfa73` exposed two distinct issues:

- Round 8's native 1280x720 image shows the tiny target around (895,347), while
  the old pixel-coordinate response put it around (895,487). This commanded
  pitch -24.23 degrees and pointed too far down. Round 9 still had the target
  near the upper part of the frame; the previous heuristic nevertheless favored
  zoom. Additionally, the semantic-repair echo sent internally normalized boxes
  without a coordinate tag, creating an ambiguous second-conversion path. Raw
  original responses were not retained, so double conversion in that particular
  response cannot be proved. The new model schema/prompt exclusively requests
  normalized_1000; explicit legacy pixel responses are still parsed for replay.
  There is no longer an ambiguous repair echo.
- The mission's accepted objective was `find`, despite a user request to approach
  and monitor within a distance. The task_goal schema now explicitly describes
  the final user objective, separately from the current search phase. Decision
  events record task_goal and task_interpretation for audit. A subsequent real
  Qwen call classified the same instruction as `approach`.

The original run issued no move/inspect_target/approach command. After zoom and
loss of detection, it issued yaw(90), leaving its position essentially unchanged.
Thus that run's observed wrong-direction turn was a search decision, not evidence
of a NED/ENU translation-axis reversal.

Validation used the existing native session; no new session or full mission was
created. Source frames and diagnostic results are locally retained under ignored
`state/debug-vlm/direction/`:

- Normalized-coordinate replay of original frame 8 localized bbox
  [695,465,705,475], producing world yaw 283.6535 / pitch -16.7599. Executing this
  direction in the native adapter produced an actual camera yaw 283.5925 /
  pitch -16.3595; the newly captured target was near the image center.
- A diagnostic 2x zoom (operator/test-selected, NOT claimed as VLM-selected)
  retained the target near the center. Independent model calls still sometimes
  missed the 1x candidate or chose another aim on the 2x image. The harness does
  not replace those model outputs to fabricate successful planning.
- An adapter-only 5m inspection calibration used a current VLM bbox and generated
  NED offset [1.2145,-4.8503,0], bearing 284.0575 degrees. Actual displacement
  between diagnostic captures was [1.164,-4.854,0.003], horizontal 4.9916m,
  bearing 283.4850 degrees. This was an explicitly selected calibration move,
  not an autonomous VLM approach. It tests visual bearing through the native
  motion adapter without using scene truth.
- An independent regression uses a camera pointing west-northwest while the
  aircraft points east, checking that the native ENU destination follows the
  camera ray and does not add an unrelated body-yaw command. Resolution tests
  verify normalized boxes are not rescaled twice at three image aspect ratios.

Full suite: 143 passed. After adding objective descriptions and event audit
fields, focused brain/geometry/planner tests: 43 passed. This is not a completed
50m autonomous mission claim. The already running dashboard process still uses
its previously loaded backend until restarted with scripts/mac-dashboard.sh.

The final single-frame Qwen check on the native 2x image, including the actual
preceding gimbal/zoom results, correctly used task_goal=approach and localized
a candidate, but still selected aim_target. That output was not replaced with
inspect_target and was not presented as autonomous flight success. Tool schema
descriptions now distinguish pointing, optical zoom and horizontal inspection;
there are no numerical scene-specific planning suggestions.
