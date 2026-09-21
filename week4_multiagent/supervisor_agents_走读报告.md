# supervisor_agents.py 代码走读报告

> 对象：`supervisor_agents.py`（Week 4：多 Agent 拓扑 + 人机协同 + 可观测性）
> 依据：一次完整真实运行（6 轮派发、22 次 LLM 调用、152.4s、人工闸门一次放行、评估 4/4）

## 全景：从"一个人"到"一个团队"改了什么

四周的累积视角：Week 1 造了**工具循环**（Agent 如何行动），Week 2 造了**编排**（单 Agent 的 FSM），Week 3 造了**生命周期**（经验跨运行存活），Week 4 把前两者的单元**复制多份并分层**——一个决策层（Supervisor）+ 多个执行层（Worker）。

```
Week 2:  run() ──► phase_execute ──► execute_tool          一个循环干所有事
Week 4:  run() ──► supervisor_step ──► worker_run ──► run_tool
                        ▲                    │
                        └──── 黑板回写 ───────┘            决策与执行分离，各层可复制
```

关键的结构对比：Week 2 的 FSM 里"下一步干什么"由**转移表**（写死的图）决定；Week 4 换成由 **Supervisor 的 LLM 在运行时决定**——转移表变成了 prompt。这是拓扑控制权从"代码"到"模型"的迁移，也是多 Agent 系统弹性和不可预测性的共同来源。

本周文件的数据流一图：

```
问题 ─► Supervisor（看黑板）─route─► Worker（只看任务+自己的工具）
                ▲                        │ 工具调用 → run_tool（关键动作过人工闸门）
                └──── findings 写回黑板 ──┘
        循环至 Supervisor 判定 finish → 最终答案 + Trace 汇总 + 评估清单
```

---

## 第 1 站：Worker 注册表——"一个 Agent"被压缩成一条数据（106-119 行）

```python
WORKERS = {
    "weather_agent": {"role": "气象数据专家…", "tools": ["get_weather", "get_air_quality"]},
    "math_agent":    {"role": "计算专家…",     "tools": ["calculator"]},
    "editor_agent":  {"role": "编辑…",         "tools": ["publish_report"]},
}
```

前三周的"Agent"是一整段代码；这里一个 Worker = **角色说明（进 system prompt）+ 工具子集（进 schema 渲染）**，两条数据。新增一个 Agent 只加一条注册表项——`worker_run` 对具体是谁零感知。

工具子集就是**权限边界**：298-299 行 Worker 试图调不属于自己的工具会收到 `Error: 你没有工具…`。对比 Week 1 把全部 TOOLS 给一个循环——多 Agent 系统里"谁能干什么"第一次成为显式设计。生产的对应物：每个 Agent 独立的 credential/权限集，某个 Agent 被注入攻击时爆炸半径被它的工具子集限制住。

`ALL_TOOLS = {**TOOLS, **LOCAL_TOOLS}`（91 行）是注册表的合流：Week 1 的三个工具不动，本地新增的 `publish_report` 叠上去——**扩展靠合并，不靠修改**（和 `AugmentedGraph` 子类化同一哲学）。

---

## 第 2 站：黑板模式——Worker 之间的唯一通信介质（173-181 行）

```python
@dataclass
class SharedState:
    question: str
    findings: list[str] = ...      # 每个 Worker 的最终产出一条
    rounds: int = 0
```

三个设计决定：

1. **写回的是 Worker 的 answer，不是它的中间过程**（334 行 `state.findings.append(f"[{to}] {result}")`）——weather_agent 内部传错参数重试的过程不进黑板，Supervisor 只看结论。这是**上下文隔离的另一半**：Worker 的试错留在 Worker 里，不污染全局视野（对比：把整个 trace 给 Supervisor = 单 Agent 的滚雪球 messages 换个名字）。
2. **黑板是追加流，不分区**——findings 就是一个 list，谁写的靠 `[weather_agent]` 前缀区分。玩具规模够用；生产对应物是结构化黑板（LangGraph 的 `TypedDict` State，每个 node 声明读写哪些字段——那是**字段级**的隔离，比这里的列表级更严）。
3. **Supervisor 的决策输入 = 问题 + 黑板全文**（265 行）——黑板越写越长，Supervisor 的 prompt 随轮数线性膨胀。本轮 6 轮没问题；生产长任务需要黑板条目压缩（Week 2 `compress` 的多 Agent 版）或归档。**已知缺陷**。

---

## 第 3 站：Supervisor——决策与执行的分离（251-267 行）

输出契约只有两个动作：

```json
{"action": "route", "to": "...", "task": "..."}   // 派发
{"action": "finish", "answer": "..."}             // 收工
```

两个值得看的细节：

- **task 的自包含要求写进了 prompt**（260 行"它是只看任务不看对话历史的专家"）——Supervisor 写任务书时必须假设读者什么都没见过。运行中第 4 轮的派发是满分示范：`"北京当前气温为 26℃，上海当前气温为 29℃"`——把黑板上的数字**复述进任务**，math_agent 因此完全不需要天气上下文。**任务书质量 = 团队上限**，这是多 Agent 系统里最值钱的一条 prompt 工程。
- **"不要为了用而用工具"**（261 行）——防 Worker 用满的另一个方向：材料够了就 finish。没有这句，Supervisor 倾向把每个 Worker 都点一遍名（模型有"完整性偏好"）。

**Router 是它的退化形式**：如果问题的答案一次分类就能定（"这是数学题还是天气题"），第一轮 route 之后第二轮必然 finish——Supervisor 自动退化为 Router。反过来不成立：Router 处理不了"查数据→算温差→写报告"这种**串行依赖**（第 5 轮 editor 的任务里带着第 4 轮 math 的结果，Router 一次分类看不到这个依赖）。选型口诀：**依赖链长用 Supervisor，一问一选用 Router**。

---

## 第 4 站：人工闸门——驳回如何变成反馈回路（217-244 行）

HITL 的实现挂在**工具执行入口**而不是 prompt 里：

```python
def run_tool(agent, name, args, trace):
    if name in CRITICAL_TOOLS:                    # ① 声明式标记
        approved, reason = confirm_gate(name, args)
        if not approved:
            return f"Error: 人工驳回——{reason}。请修改后重试或放弃发布。"   # ② 驳回 = Observation
    ...
```

三步机制，每步都有生产对应：

| 步 | 本文件 | 生产对应 |
|---|---|---|
| ① 哪些动作要人批 | `CRITICAL_TOOLS` 集合 | 权限策略引擎（按动作类型/金额分级） |
| ② 挂起等人 | `input()` 阻塞终端 | LangGraph `interrupt_before`（状态持久化，可跨进程恢复）；生产是工单/审批流 |
| ③ 驳回后怎样 | 包成 `Error: 人工驳回——原因` 喂回 Worker → Worker 改写重试 → 仍不过则 Supervisor 看到"未产出"重新规划 | 同——**驳回意见结构化回流**是 HITL 的价值所在，不然人只是个二值开关 |

② 是玩具和生产的分水岭：`input()` 阻塞意味着进程必须活着等人；LangGraph 的 interrupt 把整个图状态序列化存盘，人可以明天再批。**挂起 ≠ 阻塞**——这是手写版和框架版最实质的差距。

一个安全细节（222-224 行）：`EOFError`（非交互环境）**默认驳回而非默认放行**——不可逆动作的失败模式要向安全侧倾斜（fail-closed）。本次验证跑就是管道输入 `Y` 通过的，CI 里则会自动驳回。

---

## 第 5 站：TraceCollector——可观测性作为一等公民（126-166 行）

记账发生在 `call_llm`（195 行）和 `run_tool`（236/243 行）内部——**调用方无感知**。这一点是能落地的关键：如果要求每个相位手写记账，三周内必然漏。

真实运行的 Trace 表暴露的三个事实（这就是可观测性的价值——不跑不知道）：

| 观察 | 数字 | 推论 |
|---|---|---|
| editor 一次调用 21.4s / 770 out token | 全场最贵 | 长文本生成集中在 editor——降成本优先改它（便宜模型/流式） |
| math_agent 全程 1.4s+3.0s / 132 out token | 全场最便宜 | 路由降本的经典对象：这种活不值得用旗舰模型 |
| weather_agent 每个 Worker 先传中文报错再自愈 | 3 轮各踩 1 次 | **同一坑每轮重踩**——Worker 没有共享记忆，见缺陷清单① |

对比 Week 3：A/B 实验度量的是**结果**（纠错轮数），Trace 度量的是**过程**（谁花了多少）。生产里前者是验收指标，后者是优化依据——两张表回答不同的问题。

`resp.usage.input_tokens/output_tokens` 是 SDK 自带的，零成本获取——真实项目里最常见错误是调完 API 不记 usage，等账单来了才补。

---

## 第 6 站：运行轨迹复盘——教科书级的一次失败

真实运行的第 1 轮值得逐帧看，它演示了**任务粒度与 Worker 预算的冲突**：

```
第1轮派发: weather_agent，task = "查北京+上海的天气和空气质量"（4 项数据）
  → 4 步预算: get_weather(北京)✗ → get_weather(beijing)✓ → get_air_quality(beijing)✓
              → get_weather(shanghai)✓  ← 步数耗尽，answer 没来得及输出
  → 黑板写入: "（Worker 步数耗尽，未产出结果）"
第2轮: Supervisor 看到空手而归，把任务拆成单城 —— 成功
第3轮: 另一城 —— 成功
```

链条上三层各自都没有 bug（Supervisor 派的活合理、Worker 每步都对、预算 4 步不算小），但**组合**起来失败了。这是多 Agent 系统的典型失效模式：**没有坏组件，只有坏接口**——派发粒度和步数预算是两个独立旋钮，必须一起调。Supervisor 随后的自我修复（自动拆小任务）展示了这类系统的韧性来源：失败信息（"步数耗尽"）留在黑板上，下一轮决策看得到。

另外注意第 1 轮的浪费：4 次工具调用的结果全部随 Worker 一起丢弃（只有 answer 进黑板）——**部分完成的子任务不可回收**。生产修法：Worker 中间产物也写黑板（带进度标记），或派发前由 Supervisor 评估所需步数。

---

## 已知缺陷清单（按危害排序）

1. **Worker 无共享记忆**：拼音坑 3 轮重踩（每次都先传"北京"报错再自愈）。Week 3 的 `MemorySystem` 没接进 `worker_run`——长期记忆应该挂在系统级（各 Worker 共享）或 Worker 级（各自私有）。**跨周缝合欠账**，和 Week 3 没接 Week 2 的 compress 是同一类病。
2. **黑板无限增长**（第 2 站已析）：长任务的 Supervisor prompt 随轮数线性膨胀。
3. **Worker 中间产物丢弃**（第 6 站已析）：步数耗尽 = 全部重做。
4. **Supervisor 无重派上限**：同一个 Worker 失败 N 次仍可无限派发（MAX_ROUNDS 是全局熔断，不是单任务熔断）——对比 Week 2 的 retries 是单步级的。会话里"步数耗尽→原样重派"理论上可以烧满 10 轮。
5. **`parse_json` 无重试**（267 行）：Supervisor 输出解析失败直接强制收工——对比 Worker 侧有"格式错误重试"。决策层的容错反而比执行层薄。
6. **人工闸门在工具级而非意图级**：人批的是"这次 publish_report 调用"，不是"发布这个意图"——Worker 换个 title/body 重试时人会看到语义重复的确认。生产要做意图级去重（同一意图只确认一次）。

---

## 自测问题

> Supervisor 每轮都拿到黑板全文（问题 + findings），Worker 只拿到自己的任务。为什么不让 Worker 也看黑板——信息更多不是更好吗？

**答案**：三个理由，按重要性排。**① 上下文隔离是功能不是省料**：weather_agent 若看到 math_agent 已算出"温差 3℃"，它自己回答时可能顺手带上这个数字——但它无权验证该数字，错误会以"多个 Agent 都这么说"的假象固化（共识幻觉）。**② 职责边界防越权**：看到全局的 Worker 会做超出任务书的事（editor 看到原始数据后会自己重算温差而不是用 math 的结论），分工退化。**③ Token 经济**：每个 Worker 每步都背黑板全文，成本乘以 Worker 数 × 步数。需要跨 Worker 信息时，正确通道是 Supervisor 写进 task（第 4 轮把 26℃/29℃ 复述进任务书就是标准做法）——**信息流动由决策层编排，而不是靠共享上下文漂浮**。这正是"Supervisor 模式"区别于"消息总线模式"（AutoGen 群聊）的本质：前者信息按需路由，后者信息全员广播。

---

## 与前面各周文件的缝合

- **Week 1**：`TOOLS`/`execute_tool` 第三次被复用（55-56 行）——注册表作为单一事实来源，三个星期期的代码都还在服役。
- **Week 2**：`MAX_ROUNDS` 是 `MAX_LLM_CALLS` 熔断思路的多 Agent 版；Supervisor≈动态转移表；但单步 retries 机制**没有**平移过来（缺陷④）。人工闸门的"驳回→Observation→重试"正是 Week 2 `step_failed` 事件流的对应物。
- **Week 3**：完全没有 import——本周最大的欠账（缺陷①）。`parse_json` 是 Week 3 `consolidate` 容错解析的同款（`re.search(r"\{.*\}")`），老教训第三次兑现。
- **LangGraph 对照**（文件头 36-41 行已列）：手写版把"图"折叠成了一个 while 循环 + 一个返回 JSON 的 LLM——练习 2 用 LangGraph 重写时，每一行能映射到哪个 API 调用，就是检验本周是否学透的试纸。

## 后续方向

- 练习 2（计划内）：用真 LangGraph 重写本拓扑——重点体验 `interrupt_before` 的**持久化挂起**和 conditional edge 的类型安全
- 缝合欠账：把 Week 3 `MemorySystem` 挂到 `worker_run`（拼音坑 3 轮重踩的直接修法）
- 缺陷④的单任务熔断：同一 (to, task) 派发失败 2 次即标记该 Worker 不可用
