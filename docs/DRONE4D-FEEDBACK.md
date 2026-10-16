# Drone4D 2.0 接入后的研究需求建议

基于 2026-10-16 的 SDK、原生前端和在线独立会话接入。供与同事讨论；尚未向外发送或创建 issue。

优先建议提供统一的 **observation** 接口：RGB/深度/分割可独立订阅，同时返回 simulation timestamp、vehicle ID、camera ID、内参、world-to-camera、曝光期间姿态与控制状态。现在 RGB 在浏览器渲染端，外部 agent 必须维护 WebGL 页面；云端 headless renderer 或 SDK 原生 capture 会显著简化 VLM 闭环、批量评测和多机同步观测。真值和训练标签应使用独立权限通道，防止评测模型意外获得目标坐标。

第二优先是 **执行语义可检查**：统一 command handle、accepted/running/terminal、遥测达标条件、取消句柄和幂等重试行为。REST 的 timeout_ms 当前只等首个结果，容易被误用为飞行时限；建议 SDK 参数命名直接区分。云台 operator 指向 60 秒后到期，建议 SDK 提供持久指向 handle/续期或显式 TTL，并将到期事件反馈给控制方。goto 的 direct 与 safe_transit 应明确声明是否支持 yaw，或拒绝被忽略的参数；当前路径分支不消费 yaw_rad，原地扫描必须另外处理。

视觉任务需要原生人物/车辆的材质、衣着、帽子、姿态和遮挡；小目标识别还需要真实光学变焦、分辨率、光照与噪声模型。希望能按目标外观创建可重复场景，不必由外部应用补画网格，同时让规划器只能看到渲染结果。

为 swarm brain 研究，建议加入多机 observation barrier（同一仿真 tick 的多个机位）、固定 seed 的环境重置、暂停/单步、动作与观测轨迹导出、episode ID、确定的失败原因。报告搜索耗时、覆盖率、重识别一致性、误检/漏检、目标发现与接近误差、碰撞/越界、通信预算和模型调用成本。这些比一个固定成功的演示更利于建立可复现的 benchmark。

后续可扩展通信受限和异步决策：每机局部视图、延迟/丢包、共享记忆的访问控制、多机冲突仲裁和任务接管。保留完全相同的工具契约接入 PX4 / ArduPilot，比较同一策略在仿真与硬件中的差异。
