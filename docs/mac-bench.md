# Mac 插线运行模型机

同一台已调通的 ArduCopter 4.5 模型机，system/component=1/1，板卡 ID=1010，57600 波特率。使用数据线接入另一台 Mac，沿用今天的模型机配置和稳压供电。`motor` 会实际驱动电机，应在模型机台架、拆桨状态下运行。关闭占用该串口的 QGC 连接或 MAVProxy，让本程序持有串口。

## 第一次启动

```bash
git clone https://github.com/houqiii/uav-harness.git
cd uav-harness
bash scripts/mac-bench.sh
```

已有仓库先 `git pull --ff-only`，再执行脚本。首次运行会创建 `.venv` 并安装锁定依赖。脚本需要 Python 3.11+，优先查找已有 Python（包括 Homebrew 常用路径和 uv 管理的 3.12）；找不到时，如果已有 uv 或 Homebrew，会安装 Python 3.12。两者都没有时需先安装 Python 3.12。也可用 `PYTHON_BIN=/实际路径/python3.12 bash scripts/mac-bench.sh` 指定解释器。

只有一个 USB 串口时自动选择；有多个时列出设备，使用实际路径指定：

```bash
bash scripts/mac-bench.sh --port /dev/cu.usbserial-XXXX
```

无需编辑 JSON、启动 HTTP 服务、配置模型 API 或打开 QGC。启动只请求身份和遥测，输入动作命令后才控制电机。脚本不修改飞控参数。

## 启动后输入

```text
status
motor
probe
quit
```

- `status`：完整当前遥测。后台每秒自动显示 mode、armed、landed、姿态、电压/电量和 8 路 PWM；缺失或过期消息明确显示 missing/stale。
- `motor`：电机序号 1，1231 µs，5 秒，飞控计时停止。`motor 2` 可选择序号 2（序号是飞控电机测试顺序）。只有在已知普通 PWM 协议与输出范围内才派发。
- `probe`：AltHold 普通解锁，发送 1.5 米垂直起飞探针，观察输出，随后 Land、等待地面、普通解除武装并恢复初始模式。模型机不能飞行；此动作不证明实际达到 1.5 米。
- `land`：需要时请求 Land，并等待地面/未解锁；不强制解除武装。
- `quiet` / `watch`：暂停/开启遥测显示，接收和记录持续运行。
- `quit` 或第一次 Ctrl+C：等待已有任务收尾后关闭连接。

终端里的 `[TX]` 是发送指令，`[ACK]` 是飞控回执，`[FC]` 是飞控文本，`[RX]` 是实时遥测摘要，`[RESULT]` 是完成判定。`ACCEPTED` 只表示指令被接受；`succeeded` 还要求相应遥测效果。看到 PWM 响应并不能自动确认电机物理转动，需现场观察；缺动力供电时仍可能有 PWM 数值。

任务结果不确定时不会自动重试或重放。保留 `state/bench-console/` 下的 SQLite、MAVLink tlog 并核实状态，不通过删除状态目录绕过未确认任务。遥测是每秒显示摘要，底层持续收取并记录原始包。

## 先在无硬件环境查看界面

```bash
bash scripts/mac-bench.sh --mock
```

模拟模式使用独立临时状态，明确标注 MOCK，不打开真实串口。若只看真实模型机遥测，使用 `bash scripts/mac-bench.sh --observe`，此时拒绝控制动作。
