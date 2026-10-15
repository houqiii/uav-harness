# VLM 在线任务闭环

主工作区为“无人机群意图控制系统 / VLM 在线任务”。同一个 Qwen VLM 理解用户意图、读图并根据执行反馈重新选择动作。该路径不调用 DeepSeek；旧的 DeepSeek 指令工作台仍保留。

```text
自然语言任务
    ↓
获取新图像 + 拍摄时相机位姿 + 遥测 + 能力 + 机群状态
    ↓
Qwen VLM：任务解释、画面证据、更新计划、一个下一步动作
    ↓
程序校验目标确认、图像时效、几何、能力、任务边界
    ↓
相机/云台/飞控端口执行短动作 → 实际结果 → 下一轮新观测
```

后端拥有任务生命周期，浏览器仅显示服务端状态；切换标签页不会停止任务。每架飞机同一时间只有一个任务所有者，其他 Harness 控制请求会被拒绝。每轮最多派发一个动作，不预生成整个接近航线。

## 无硬件运行

```bash
export DASHSCOPE_API_KEY='自己的百炼官方密钥'
# 可选：默认 qwen3-vl-plus
export QWEN_VL_MODEL='qwen3-vl-plus'
bash scripts/mac-dashboard.sh --offline
```

打开 http://127.0.0.1:8080，先“采集观测”，再输入任务并“启动闭环”。“按指令自动判断”由 VLM 区分只查找和靠近观察；也可明确选择目标约束。任务首次接受的目标范围随后不能被模型擅自扩大。

离线模式不打开串口/UDP 飞控，不发送 MAVLink。后端以独立种子生成地面人物及几何相机画面，飞机移动/转向/云台俯仰会实际改变投影和下一轮图像。模型只收到图像与观测数据，不收到世界人物坐标或检测答案。运动学仿真加速执行，用于检查控制闭环，不代表真实飞行动力学。画面为简单合成图；识别质量、真实场景分辨率及云端模型决策质量需另外验证。

没有密钥仍能打开页面和采集观测，启动任务会明确报错；没有固定结果或备用供应商。正式接口使用官方 DashScope 兼容端点，默认北京地域：

```bash
# 仅在账户和模型属于其他地域时按官方配置修改，禁止第三方网关
export QWEN_BASE_URL='https://dashscope-intl.aliyuncs.com/compatible-mode/v1'
```

密钥只由服务端环境读取。API 的单次总时限为 90 秒，连接时限 5 秒，读取空闲时限 25 秒，并发推理上限 3。取消任务会取消等待中的推理。任务另有总时限及轮数限制。

本次真实请求返回 HTTP 400 / `Arrearage`，服务端已明确显示“百炼账户欠费或状态受限”。因此目前验证了真实请求/错误传递，以及受控模型响应下的在线反馈闭环；**尚未验证该账户的真实视觉识别和多轮决策效果**。账户恢复后重启任务即可继续，不会转用其他模型或伪造成功。

## 动作与几何

VLM 可选：`observe`、`takeoff`、`yaw`、`gimbal`、`approach`、`altitude`、`hold`、`finish`。目标检测返回当前帧 0..1000 坐标框及地面接触点是否可见。输出经 Pydantic 严格校验，不支持任意 Python、shell、MAVLink 参数或设备地址。

搜索高度 50 m、扫描步长 30°、最大前进段 10 m、观察间距 20 m 是可配置策略默认值。目标出现在哪、是否需要再次确认、向哪里移动、何时调整高度和悬停，由每轮观测和模型输出决定。用户只说“找到”时，模型不能自动扩展成靠近任务。

相机内参、拍摄位姿及已知地面平面将目标框底边中心投影到 NED 世界坐标。程序计算方向和水平距离，并按不确定度、观察间距、单段上限和任务半径裁剪位移。VLM 不能自报“目标 12 米”作为执行依据。没有标定、地形信息或可见接触点时禁止依赖该投影靠近。下降必须满足地面净空约束；目标相对运动至少需要两次空间一致的新鲜观测，丢失后停止接近并重新观察。

当前为平面地面近似，误差裕度不是概率保证；不适用于坡地、遮挡地面接触点或未校正镜头畸变的直接测距。30° 已观测扇区仅记录采样航向，不能证明 500 m 场地无盲区；实际覆盖还需结合镜头视场、地形、目标像素大小和路线设计。

## 接入 PX4 / ArduPilot 和相机

参考 [brain-px4-http.json](../examples/brain-px4-http.json)。这是默认禁止实机控制的配置模板，型号、固件、system/component ID、UDP 路由和相机参数必须按实际设备填写。

```bash
bash scripts/mac-dashboard.sh --config examples/brain-px4-http.json
```

代码边界：

| 组件 | 当前实现 | 实机接入时的工作 |
|---|---|---|
| VLM Planner | `brain/provider.py`，官方 Qwen 图像+遥测+历史 | 配置可用账户/模型，不替换任务逻辑 |
| Mission | `brain/runner.py`，状态/目标记忆/校验/在线循环 | 保留；通过实拍和 SITL 调整策略 |
| Vehicle Port | `brain/hardware.py` → 既有 Harness | 校验具体固件映射、限高、完成判据 |
| Camera Port | 配置的 HTTP JPEG/PNG snapshot → JPEG | 相机若提供 RTSP/SDK，写桥接服务或替换 `capture()` |
| Gimbal | MAVLink Gimbal Manager 指令和姿态回报 | 核对设备 ID、manager 支持、坐标约定和独占控制 |
| Simulation Port | 几何相机/运动学执行 | 保留无硬件回归环境 |

Snapshot 接口每次请求必须抓取新图，返回 `X-Capture-Timestamp: <UTC Unix 秒>`，相机桥接和主机需要对时。只有明确确认接口返回新帧时才可设置 `trust_receipt_time:true`，该方式会标为收包时间估计。缓存视频截图和长期重复旧帧不能靠改时间戳变成新观测。

配置图像实际 `width/height/fx/fy/cx/cy`、经校正的相机坐标和固定安装姿态，或实际返回姿态的云台 ID；`offset_body_frd` 为机体前/右/下杆臂，米。`ground_down_m` 是同一 LOCAL_NED 原点下的地面 D 坐标，并非相对 Home 高度。未知时保持 null，只观察不靠近。示例内参数仅说明格式，不是可直接用于任何镜头的标定。

请求前后核对完整相机姿态和飞机位置，运动期间抓帧会被拒绝；推理后再次核对相机姿态、位置、航向及观测时间。设备支持的更精确时间同步可替换此静止采样接口。

真正派发飞控需同时满足 `allow_control:true` 和 `brain.allow_hardware_control:true`，且是 `flight` 档案。`horizontal_space_cleared:true` 才开放目标相对前进能力，代表操作者已确认操作空间；当前没有避障传感器规划。任务策略的最大/搜索高度不得超过飞机档案限高，程序不会自动提高限高。默认 20 m 档案不能执行 50 m 任务，需要操作者按实机环境调整。模型机 `model_bench` 档案不能执行此位置飞行任务。

PX4 使用既有 Offboard proof-of-life 和 20 Hz setpoint；转向新增 `flight.yaw`，保持位置并等待实际航向反馈。每段完成后交给飞控原生保持，避免云端推理期间残留运动设定值。ArduPilot 和 PX4 继续分别通过自己的适配器执行。

## 暂停、异常和恢复

推理期间暂停会取消请求并保持位置；继续后重新抓图。重复暂停不会打断第一次保持的收尾。正常结束、任务时限、API 失败均尝试保持。人工切换模式后，任务进入 unknown 并让出控制，不重新抢回 Offboard，也不自动切换回保持模式。

实机动作中途取消时，原动作的完成结果仍为 unknown；直接调用原生保持收尾，不通过会被 unknown 拒绝的新任务。该任务不会自动续跑。由操作者接管并落地/解除武装后，点击“地面核验并释放”，以新鲜遥测核验并释放所有权。没有新鲜地面且未解锁证据时拒绝释放。超时 ACK 的命令编号仍保留原 Harness 连接的阻断状态，需要重建连接后才可复用；核验不重放任何命令。

任务记录与帧缓存在服务端内存，事件最多 200 条，图像最多 16 帧。飞控动作仍由 SQLite journal 持久保存；进程重启不会恢复/重放未完成的物理动作。

## API

| API | 内容 |
|---|---|
| `GET /v1/brain` | 提供商状态、能力、策略、任务快照 |
| `POST /v1/brain/preview` | `{vehicle_id}`：采集观测，不调用模型、不执行动作 |
| `POST /v1/brain/missions` | `{query,vehicle_id,objective:auto/find/approach}`：创建任务 |
| `GET /v1/brain/missions/{id}` | 最新观测、目标、决策、实际结果、事件 |
| `POST /v1/brain/missions/{id}/pause` | 取消在途推理/动作，尝试保持 |
| `POST /v1/brain/missions/{id}/resume` | 仅可继续已确认暂停的任务 |
| `POST /v1/brain/missions/{id}/cancel` | 结束任务并尝试保持 |
| `POST /v1/brain/missions/{id}/reconcile` | unknown 任务的地面核验和所有权释放 |
| `GET /v1/brain/missions/{id}/frames/{observation_id}` | 带 API 鉴权的 JPEG |

原 `/v1/demo/preview-stream` 返回 410，浏览器固定脚本已移除。既有 `/v1/intents` 和“指令工作台”兼容旧的 DeepSeek 编译路径。

当前实现提供机群索引和多飞机独立任务所有权，**没有完成 swarm 的跨机任务分配、空域冲突消解或联合路径规划**。协议模拟测试不能替代 PX4/ArduPilot 固件 SITL 和实机验收。
