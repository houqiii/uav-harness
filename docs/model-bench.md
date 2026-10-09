# 2026-10-09 ArduCopter 模型机

飞控实测为 ArduCopter 4.5.0、system/component=1/1，固件板卡 ID=1010。模型机不能飞行，用户现场确认电机动作。串口 57600；现有模板中的设备路径应按本机枚举修改。

## 今天调通的条件

最初普通解锁失败，提示 `AHRS: EKF3 not started`、`Need Alt Estimate`。用户确认 CAN GNSS 未接，原 GPS_TYPE=9（DroneCAN）且 EKF 位置/速度来源依赖 GPS；首选缺失罗盘仍被使用，有数据的内部罗盘被禁用。无遥控器又导致解锁后 radio failsafe。

按用户授权调整并读回：

| 参数 | 原值 | 模型机测试值 |
|---|---:|---:|
| GPS_TYPE | 9 | 0 |
| EK3_SRC1_POSXY | 3 | 0 |
| EK3_SRC1_VELXY | 3 | 0 |
| EK3_SRC1_VELZ | 3 | 0 |
| COMPASS_USE | 1 | 0 |
| COMPASS_USE2 | 0 | 1 |
| FS_THR_ENABLE | 1 | 0 |

保留 EKF3、气压计高度来源和罗盘航向来源。ARMING_CHECK=0 是原有配置，本轮没有再修改。项目不自动写这些参数；接回传感器或改为真实飞行用途时，需根据实际硬件重新选择配置，不能直接套用模型机 profile。

AltHold 下，NAV_TAKEOFF 的 param3=0 要求水平导航，返回 FAILED；param3=1 返回 ACCEPTED。使用未接通的动力输出路径时，遥测数值升高但电机无动作。接入稳压电源后，同样的单电机测试实际转动，解锁/起飞测试期间多台电机转动或转速提高，均由用户现场确认。

## 可复现探针

`bench.motor_test`：普通 PWM 输出 1231 μs、单电机、最多 5 秒。飞控内置计时停止；Harness 等待地面未解锁及输出遥测证据。每次都先读协议与 PWM 范围。

`bench.takeoff_probe`：检查姿态/高度估计，AltHold，普通解锁，22 `[0,0,1,0,0,0,1.5]`，观察输出，然后 Land；等待 ON_GROUND 才普通解除武装，恢复初始模式。ACK 或普通解除武装失败不被当成结束成功，不使用 force 参数。

软件结果中的 `physical_rotation_verified:false` 表示程序未接到 RPM/现场观察传感器；历史用户观察在 `evidence/2026-10-09-model.json` 单独标明来源。`altitude_reached:false` 表示没有到高验收。完整地图飞行需要有效水平定位，不会用伪造位置放行。
