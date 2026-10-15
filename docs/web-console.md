# 网页控制台

在已拉取的项目目录运行。先退出占用同一串口的终端控制台或地面站连接，等待任务收尾。

```bash
git pull --ff-only
bash scripts/mac-dashboard.sh
```

打开 <http://127.0.0.1:8080>。脚本复用终端入口的 Python 查找、虚拟环境和锁定依赖安装流程，不需要 Node/npm。没有硬件时运行 `bash scripts/mac-dashboard.sh --mock`，界面标记为协议模拟。

多串口用 `--port /dev/cu.usbserial-XXXX`，只观察用 `--observe`，HTTP 端口用 `--http-port 8081`。已有机群配置可用 `bash scripts/mac-dashboard.sh --config examples/mock-fleet.json`；该方式保留配置中的机型、链路、控制开关、状态目录和 HTTP 绑定。默认实机入口与已验收终端使用相同的 ArduCopter 4.5 / board 1010 / sys 1 / comp 1 模型机配置，且共用 `state/bench-console` 的任务记录与未决结果约束。

## 一句话调度

在启动服务的同一终端配置已有 DeepSeek 密钥；密钥只在后端使用，不要填进网页或提交到 Git。

```bash
export DEEPSEEK_API_KEY='自己的密钥'
bash scripts/mac-dashboard.sh
```

1. 输入“让模型机 model-1 的 1 号电机以 1231 微秒测试 5 秒”，点击“生成调度计划”。模型只翻译，预览阶段不派发控制。
2. 查看动作、目标飞机、参数、依赖或展开 JSON；点击“执行计划”提交同一份计划。修改原文后必须重新生成。
3. 在执行记录中查看 TX、定向 ACK、动作结果；右上 PWM、姿态、模式、armed、落地状态随真实遥测更新。ACK 接受与任务完成分开呈现。

未配置 DeepSeek 时，“单电机测试”“起飞探针”可生成明确标注的固定测试计划。它们只在所选 model_bench 机型下开放，不能当作自然语言翻译结果。没有定位的模型机不会开放前后移动，起飞探针只证明指令/输出和收尾。

任务提交响应丢失时，页面保留原幂等键；点击“查询 / 重试本次提交”不会重复派发已经接收的任务。待确认提交在本浏览器会话内保存，刷新后可恢复；服务中的近期任务从 SQLite 加载。取消任务会继续等待原收尾逻辑，结果不确定时明确显示 UNKNOWN，不自动重放。可点“核实地面状态”调用既有 reconcile 接口；它只核实新鲜地面/未解锁状态，不把原任务改为成功，丢失 ACK 后仍需重启服务才能复用相应命令。

右上飞机选择器仅切换监测对象和快捷示例的目标。自然语言可指定机群中的其他飞机；以生成计划里展示的 vehicle_id 为准。

## 云台画面与姿态

“连接设置”中为当前飞机填写 HTTP(S) 视频地址并选择格式：

- MJPEG：直接通过浏览器图像元素显示。
- 原生视频：浏览器支持的 MP4；Safari 可直接播放 HLS。其他格式需要视频网关提供浏览器可播放的输出。

RTSP 不能直接放入网页播放，需要将设备 RTSP 先转为 MJPEG、可播放的 HTTP 视频等地址。本项目不内置转码网关。视频配置仅保存在当前浏览器，服务端不代理任意地址；未接入、流错误和服务离线均明确显示。

姿态被动接收同一 MAVLink 系统上各组件广播的 `GIMBAL_DEVICE_ATTITUDE_STATUS`，提供俯仰、横滚、航向、航向参考系、组件/设备 ID、故障标志及消息时效；多个云台可切换。`VIDEO_STREAM_INFORMATION` 用于显示设备宣告的视频元信息，消息存在不代表画面已连通。未收到姿态就显示“未接入”，即便视频可播也不虚构角度。载荷接收与飞控身份/定向 ACK 匹配相互独立。协议依据：[Gimbal v2](https://mavlink.io/en/services/gimbal_v2.html)、[Camera v2](https://mavlink.io/en/services/camera.html)。

云台必须通过现有链路广播/路由这些消息；本入口不自动配置设备输出频率，不提供新增云台控制动作。真实云台与视频尚待设备现场验收。

## 鉴权与退出

服务设置 `HARNESS_API_TOKEN` 时，在网页“连接设置”的 API 访问令牌里填写相同值。静态界面可加载，所有数据和控制 API 仍要求 Bearer。令牌保留在当前浏览器会话中，不出现在 URL。

关闭标签页不会取消已提交任务。停止服务用终端 Ctrl+C，等待已有任务收尾。HTTP 服务保持单进程、默认本机绑定。
