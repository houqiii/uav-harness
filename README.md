# UAV Harness

自然语言 → DeepSeek 结构化计划 → Harness 调度 → ArduPilot / PX4 后端 → MAVLink → 飞控。独立 Python 项目，支持串口直连、UDP 路由、HTTP 原子动作接口和异构飞机 DAG 调度；QGroundControl 是可选监控端。

当前面向多旋翼。ArduCopter 4.5 模型机的普通解锁、无 GPS 垂直起飞命令和电机响应已有现场证据。PX4 1.16 的命令映射、Offboard 预发送和异构调度通过真实 MAVLink 报文的协议测试对端验证，**尚未完成 PX4 固件 SITL 或实机飞行验收**。内置 simulator 是软件协议测试对端。

## 快速运行

另一台 Mac 插上已调通的模型机后，可直接运行交互终端：

```bash
bash scripts/mac-bench.sh
# 连接后输入：status、motor、probe、quit
```

脚本自动安装依赖、选择唯一 USB 串口。独立 `uav>` 输入行支持编辑和历史命令，底部状态栏显示实时遥测；发送参数和 ACK 显示在输入行上方。首次连接不会自动转电机；输入 `motor` 才执行 1231 µs、5 秒单电机测试。完整说明见 [Mac 模型机终端](docs/mac-bench.md)。无硬件试运行加 `--mock`。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/uav-harness --config examples/mock-fleet.json serve
```

打开 `http://127.0.0.1:8080/docs` 查看 OpenAPI 和交互式接口；默认示例包含 ArduPilot、PX4、无 GPS 模型机三种会话，全是协议模拟。

也可直接查看仓库内的 [OpenAPI 契约](docs/openapi.json)。

CLI 可直接执行异构示例并输出逐动作结果与 MAVLink 回执：

```bash
.venv/bin/uav-harness --config examples/mock-fleet.json run examples/heterogeneous-plan.json --request-key mixed-demo-1
.venv/bin/python -m pytest -q
```

## 调度接口

| API | 用途 |
|---|---|
| `GET /v1/vehicles` | 飞机身份、状态、后端及可用动作 |
| `POST /v1/actions` | 调度一个原子动作 |
| `POST /v1/plans` | 提交含依赖的多机动作计划 |
| `GET /v1/jobs/{id}` | 执行状态、逐动作证据和协议事件 |
| `POST /v1/jobs/{id}/cancel` | 取消待执行动作，并尝试已控制飞机的原生保持或模型机收尾 |
| `POST /v1/jobs/{id}/reconcile` | 用新鲜地面/未解锁状态核实 UNKNOWN；不重放任务 |
| `POST /v1/intents` | DeepSeek 翻译；默认返回计划，`execute:true` 提交调度 |

提交动作、计划和意图时必须提供 `Idempotency-Key`。同键同内容返回原任务，同键不同内容被拒绝。键和任务结果持久化在 SQLite；进程重启把未完成任务标为 UNKNOWN，禁止自动重放。

模型机电机测试示例（当前 mock 配置下只驱动协议对端）：

```bash
curl -s http://127.0.0.1:8080/v1/actions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: motor-test-1' \
  -d '{"action_id":"motor","vehicle_id":"model-1","action":"bench.motor_test","params":{"motor_sequence":1,"pwm_us":1231,"duration_s":5},"timeout_s":20}'
```

统一动作包括 `vehicle.arm`、`vehicle.disarm`、`flight.takeoff`、`flight.move_relative`、`flight.hold`、`flight.land`。模型机只开放 arm/disarm/land 和 `bench.motor_test`、`bench.takeoff_probe`，不开放位置飞行。`bench.takeoff_probe` 自带解锁、Land、解除武装和恢复初始模式，**完成表示指令与输出遥测证据，不表示达到起飞高度**。

`flight.move_relative` 的坐标约定：`local_enu` 为 x 东、y 北、z 上；`body_flu` 为 x 前、y 左、z 上，使用派发时的真实航向转换并冻结目标。上升/下降用 z 正/负表示，属于位置控制，需要有效定位。

## 接入 DeepSeek

```bash
export DEEPSEEK_API_KEY='填写自己的密钥'
export DEEPSEEK_MODEL='deepseek-flash'
# 可选 DEEPSEEK_BASE_URL，默认 https://api.deepseek.com
# 环境变量在启动服务前设置；已有服务需先退出，再重新启动。
.venv/bin/uav-harness --config examples/mock-fleet.json serve
```

在另一个终端请求：

```bash
curl -s http://127.0.0.1:8080/v1/intents \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: intent-preview-1' \
  -d '{"query":"两架飞机起飞2米，向东移动1米后降落","execute":false}'
```

请求 `execute:true` 时，经类型、DAG、能力、范围和实时状态检查后自动调度。模型只产生结构化动作计划；持续设定值、ACK、超时与完成判据由本地进程管理。测试用模拟 API 响应验证这一完整链路，外部 DeepSeek API 需要自行配置有效密钥。

## 接入真实飞控及 QGC

`examples/model-serial.json` 是今天模型机的串口直连模板，`examples/model-router.json` 连接既有 MAVProxy UDP 路由，`examples/mixed-udp.json` 演示 ArduPilot/PX4 分链路配置。硬件模板默认 `allow_control:false`；`inspect` 只做身份与遥测请求。

```bash
.venv/bin/uav-harness --config examples/model-serial.json inspect
```

每个串口由一个进程持有。直连时应先停用持有同一设备的路由器/QGC 串口连接。服务自行发送 GCS 心跳、请求消息频率，按 system/component ID 分发会话，并核对配置的固件/板卡。

准备执行模型机动作时，在自己的配置中设 `allow_control:true`，再启动 `serve` 或执行 `examples/model-probe.json`。不会自动修改 GPS、罗盘、failsafe 或 arming 参数。今天的模型机配置和恢复原值见 [模型机说明](docs/model-bench.md)。

`links[].observers` 可将收到的遥测镜像到 `127.0.0.1:14550`，供 QGC 查看；这是只读监控，不接受 QGC 回传指令。需要双向人工地面站时，使用独立 MAVProxy 路由并将 Harness 配置为 UDP 客户端。各地面站使用不同 MAVLink system ID。QGC 是否打开不决定 Harness 的通信循环。

遥测接收也完全独立于 QGC：Harness 自行订阅、解析并保存心跳、模式、armed、估计器、位置、姿态、电池、输出和 ACK；通过 `GET /v1/vehicles` 查询当前状态，通过任务 API 查询完成证据。不需要监控界面时，将 `observers` 设为空数组即可。

HTTP 默认仅绑定本机；对外绑定时必须设置 `HARNESS_API_TOKEN`，客户端使用 `Authorization: Bearer ...`。服务按单进程运行；多个 HTTP worker 会破坏设备所有权与调度租约。

## 执行语义

- ACK 只表示接受；飞行动作还需新鲜位置/模式/落地/armed 遥测证明完成。
- ArduPilot 起飞高度相对 Home；PX4 NAV_TAKEOFF 转换为 Home AMSL + 目标高度。两者不共用模式编号。
- PX4 进入 Offboard 前持续发送当前位置超过 1 秒；进入后冻结新目标并以 20 Hz 发送。
- 每架飞机只有一个活动任务；不同飞机可以同时执行。依赖失败后不派发后继动作。
- 控制器重启、遥测失效、未确认效果或模式被外部改变会产生 UNKNOWN；无自动重试/重放。
- UNKNOWN 核实不会将原任务改成成功；缺 ACK 的命令 ID 在当前连接上仍被阻止，核实后应重启连接再复用。
- 飞行高度/相对距离、有效定位和已知电量按配置检查；普通解除武装只在地面执行。

详细设计：[架构与扩展](docs/architecture.md)、[验证边界](docs/verification.md)。原始运行日志、SQLite 和密钥留在本地，不随源码发布。
