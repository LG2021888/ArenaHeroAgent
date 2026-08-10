# Arena Hero Agent

这是一个基于官方 `arena-hero` Python SDK 的持续运行 Agent。SDK 负责 WebSocket 认证、Ping/Pong、重连和命令安全重试；`arena_agent.py` 只负责读取每个 `Turn` 并构造完整计划。

## 安装

需要 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env`，填入 Agent token；或者在当前 PowerShell 会话中设置：

```powershell
$env:ARENA_HERO_API_KEY = "your-agent-token"
```

程序不会把 token 写入源代码，也不会打印 token。`.env` 已被 `.gitignore` 忽略。

## 运行

持续运行：

```powershell
python .\arena_agent.py
```

也可以直接双击项目目录中的 `run_arena_agent.bat` 启动；它会自动使用 `.venv`，不需要每次手动激活虚拟环境。

只提交一轮进行连通性验证：

```powershell
python .\arena_agent.py --max-turns 1
```

幂等键包含进程启动时生成的唯一会话标识和当前 Tick。同一 Tick 的重试会复用同一个键；重启 Agent 后会使用新的键空间，避免与上一次运行发生冲突。

可用参数：

- `--max-population 30`：本策略的生产上限；游戏本身没有人口上限。传入 `0` 表示策略不设置人口上限。
- 默认先恢复 `12 Worker + 3 Vanguard + 4 Ranger = 19` 基础阵容，再按 Worker、Vanguard、Ranger 顺序扩容到 `16 + 6 + 8 = 30`；也可以用 `--worker-target`、`--vanguard-target`、`--ranger-target` 调整最终目标。
- 生产价格由 SDK `unit_cost(UnitType, population)` 按生产前的当前存活人口 `N` 计算：`N <= 19` 使用基础价，`N = 20–24` 使用第一档涨价，`N = 25–29` 使用第二档涨价；没有每 Tick 自动维护费。
- `--spawn-unit AUTO|WORKER|VANGUARD|RANGER`：默认 `AUTO` 按编队目标生产，指定单位名称后强制只生产该类型。
- `--beacon-policy RETREAT|HOLD`：默认 `RETREAT`，Beacon 在 Core 近距离可见时让 Core 逐步离开；`HOLD` 保持当前位置。
- `--no-spawn`：只控制现有单位，不生产新单位。
- `--no-combat`：关闭可见敌人追击和攻击。
- `--submit-retries 3`：同一回合提交失败时使用同一个幂等键重试。
- `--reconnect-max-delay 30`：连接层错误后的最大重连等待秒数。
- `--max-reconnect-attempts 8`：连续连接失败达到次数后安全停止，避免无限重连。
- `--log-level DEBUG|INFO|WARNING|ERROR`：调整日志详细程度。
- `--log-file arena_agent.log`：将关键启动、每 Tick 摘要、重连和错误日志保存到轮转文件；传入空值可关闭文件日志。
- `--state-file arena_agent_state.json`：保存障碍、资源和探索覆盖记忆，重启后恢复。
- `--trace-file arena_agent_trace.jsonl`：保存每 Tick 的 JSONL 战局快照，包括 Worker 当前模式、撤退目标、路线代价和停滞计数，可用于回放分析。
- `--stats-file arena_agent_stats.json`：保存本次运行的资源、威胁、任务和事件统计。

## 策略

- Worker 携带货物时返回 Core，并在同格执行 `DEPOSIT`。
- 空载 Worker 优先采集当前可见或近期记忆中的资源点；没有有效目标时从 Core 左侧开始分区探索，发现资源后立即转为采集。
- 多个 Worker 会分到不同探索方向：左、右、上、下，避免一起挤在同一条路线。
- Worker 会保持当前探索航点，不会因为每 Tick 的区域记忆重算而互相追逐；到达航点、路线不可达、连续 4 Tick 没有实际位移，或相对上一 Tick 的路径代价连续 3 Tick 没有下降后切换目标。新发现障碍导致路线整体变长时，只要剩余代价继续下降就不会误判为停滞。
- 探索 Worker 会保留最近 6 个经过位置，并对回访格增加路径代价，减少折返和短循环。
- 探索使用持久化地图 chunk 的未观察前沿：每个 Worker 在一个扇区连续向外推进 4 个 chunk 后轮换；路线不可达或停滞时立即换扇区。重启后根据已保存的覆盖边界继续向外，不会重新扫描 Core 周围的固定四环。
- 敌方 Vanguard/Ranger 进入 Worker 近距离后，Worker 会先撤离，并避开 Vanguard 邻格和 Ranger 三格射线；敌方 Worker/Core 不会触发战斗撤退。
- 远程空载 Worker 遇到敌方战斗单位后进入 `EVADE -> RETURN -> COOLDOWN -> SCOUT`：先回到 Core 附近，冷却 3 Tick，再恢复探索，不会马上重走旧路线；Trace 会将这段明确记录为 `SCOUT_RETURN`，不与探索停滞混淆。
- Worker 死亡产生的 `WORKER_CARGO_DROPPED` 会进入高优先级资源记忆，附近空载 Worker 会优先回收；资源耗尽或超过记忆 TTL 后自动清除。
- 生产策略先生产 4 个 Worker，再补 1 个 Vanguard 和 1 个 Ranger 建立最低防线；随后恢复 `12/3/4` 基础阵容，再按 Worker、Vanguard、Ranger 顺序扩容到 `16/6/8`。最低防线前不保留生产储备，基础阵容阶段保留 10 资源，扩容阶段保留 15 资源；战斗敌人进入警戒范围后暂停非必要生产，无安全退路且资源足够时可紧急生产防守单位。
- Beacon 处于地面且距离 Core 小于 8 格时，Core 会在没有更高等级威胁时选择增加与 Beacon 距离的合法方向，避免 Core 长时间停在 Beacon 附近。
- 日常移动会避开已知障碍、敌方占据格和当前可计算的攻击危险格，并在直线路径受阻时尝试局部绕行；普通格最多进入 1 个 Unit，Core 可与 1 个 Unit 同格，已占用或已预留的目的格不会再接收其他 Unit。
- 障碍位置累积到独立的永久记忆中，用于选择下一步移动方向。
- Vanguard 在相邻有战斗敌人时 `SWEEP`，Ranger 在合法射程和无遮挡条件下 `SHOOT`；优先处理正在攻击 Core 或确认追击的敌人，守卫岗位避开资源和危险格。
- 活动敌军只触发防守和合法反击，不会触发远程追击；只有连续两个 Tick 位置不变且当前没有活动敌军压力时，才允许 Ranger 在合法射程内清除静止目标，非首位 Vanguard 最多在 6 格内有限靠近。
- 威胁状态按 `NORMAL -> ALERT -> PRE_EVADE -> ENGAGED -> BREAKOUT` 处理：只跟踪 Vanguard/Ranger；结合 2 Tick 活动记忆、追击分数和 16 Tick 进入射程预测提前撤离，威胁消失后保持 8 Tick 谨慎期，近期受击进入交战，多方向逼近时突围。
- 控制台同时显示独立的生命周期和任务摘要：`ACTIVE/RESPAWNING/RECOVERY` 与 `ECONOMY/SCOUT/GUARD/RECOVERY`，它们是诊断层，不会覆盖具体威胁事实。
- Core 撤退方向优先最小化下一 Tick 预计伤害，再比较敌军距离和移动连续性；移动目的地风险变差时会取消移动。受损防守单位在同类仍有后备且资源充足时返回 Core 治疗。
- Core 处理低 HP 或低护盾后，按官方 SDK 动态价格分阶段补齐 `12/3/4 -> 16/6/8`；同一 Tick 先执行 Worker `DEPOSIT`，再把实际可交付资源用于生产判断，Core 移动时不会把交付或生产计入计划。
- 网络传输错误和临时 API 错误不会立即退出：同一 Tick 先用进程会话幂等键重试，仍失败则指数退避并重建 SDK 客户端；连续重连超过上限后安全停止。永久 API 错误和幂等冲突会记录状态码、错误码、消息和 details 后停止，避免掩盖非法命令。
- WebSocket 初始出现 `state arrived before tick` 时最多自动重连 3 次；如果是 `invalid Arena Hero WebSocket message` 或 `invalid command acknowledgement`，会保守停止并输出 SDK 版本和底层校验详情，提示检查服务端协议与 `arena-hero` 版本是否匹配。
- SDK 已升级到 `arena-hero 0.2.9`；新版状态不再依赖 `population_tier` 和 `upkeep_next_tick`，程序不再显示或模拟自动维护费。仅在实际安装旧版 SDK 时启用窄范围状态兼容。
- 每轮都整体替换计划，不复用旧 Turn 的控制器对象。
