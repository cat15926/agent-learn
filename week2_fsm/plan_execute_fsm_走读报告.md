# plan_execute_fsm.py 代码走读报告

> 对象：`plan_execute_fsm.py`（Week 2：Plan / Execute / Replan / Finish 状态机）
> 依据：一次真实运行（"北京/上海/深圳哪里适合户外跑步"，触发两次 REPLANNING，7 步计划闭环）

## 全景：从"一个循环"到"一台状态机"

Week 1 的架构是 `react_loop()` 一个函数包打天下——状态隐式藏在 `messages` 里，控制流藏在 `if/else` 里。Week 2 把它拆成三种角色：

```
        ┌────────────────────────────────────────────────┐
        │  run() 主循环 = "事件泵"（无业务逻辑）             │
        │    while phase != DONE:                         │
        │       相位函数(client, s) → (event, payload)     │
        │       s = reduce(s, event, payload)             │
        └───────┬────────────────────────┬───────────────┘
                ▼                        ▼
        ┌──────────────┐         ┌──────────────┐
        │ 4 个相位函数   │         │   reduce()   │
        │ plan/execute │  emit   │  Reducer +   │
        │ replan/finish│─事件──► │  转移表+派生路由│ ──► 改 s.phase
        └──────┬───────┘         └──────────────┘
               │ 都只读 s、emit 事件，绝不直接改状态
               ▼
        ┌──────────────┐
        │  AgentState  │  ← 唯一的状态载体（S_t）
        └──────────────┘
```

**读/写分离**是全文件的骨架：相位函数是纯"读侧"（读状态、调 LLM、干活），`reduce()` 是唯一"写侧"。这就是 LangGraph 里"节点返回局部更新、框架合并进 State"的手写版。

---

## 第 0 站：分层复用（`plan_execute_fsm.py:39-48`）

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "week1_react"))
from react_engine import MODEL, TOOLS, execute_tool
```

工具层原封不动地从 Week 1 import——`TOOLS` 注册表、`execute_tool` 的三层防线、安全求值器全部复用。这是"单一事实来源"设计的第二次收益：Week 2 只关心**编排**，工具一个字没重写。架构分层的判据就在这：换一种编排范式（ReAct → FSM），工具层应该零改动。

---

## 第 1 站：`AgentState`——状态显式化（`plan_execute_fsm.py:62-74`）

```python
@dataclass
class AgentState:
    question: str
    phase: str = "PLANNING"      # ← FSM 当前在哪个节点
    plan: list[str]              # ← 任务列表（Plan-and-Execute 的"计划"）
    step_idx: int = 0            # ← 执行进度指针
    done_results: list[str]      # ← 已完成步骤的结果
    retries: int = 0             # ← 当前步骤连续失败计数
    last_error: str = ""         # ← 最近错误（喂给重试/重规划的反馈）
    final_answer: str = ""
    llm_calls: int = 0           # ← 全局花费计数
```

逐字段和 Week 1 对照，看"隐式"变"显式"意味着什么：

| Week 1（藏在哪） | Week 2（显式字段） | 显式化的收益 |
|---|---|---|
| messages 数组的长度和位置 | `phase` + `step_idx` | 控制流可打印、可断点、可单测 |
| assistant 轮里模型自己维护的隐性计划 | `plan` | 计划成了**数据**——可以整体替换（replan 的前提！） |
| 散落在各轮 Observation 里 | `done_results` | 可压缩、可摘要、可持久化（→ Week 3 记忆的接入点） |
| 无（模型失败了就再试，不计数） | `retries` + `last_error` | 重试有了**预算**，错误有了**去向** |
| 无（while 循环无上限感知） | `llm_calls` | 花费可观测 |

关键认知：**`retries`/`llm_calls` 这类字段在 Week 1 里根本不存在**——不是忘了写，而是消息列表架构下"没有地方放它们"。状态机逼你回答"Agent 的状态到底是什么"，这就是 FSM 设计的第一收益。

---

## 第 2 站：转移表 + `reduce()`——公式落地（`plan_execute_fsm.py:81-125`）

学习计划里的公式 `S_{t+1} = Reducer(S_t, ΔS)` 对应这一个函数，三段式：

```python
def reduce(state, event, payload):
    # ① 应用增量 ΔS —— 事件如何改数据
    if event == "planned":      state.plan = payload
    elif event == "step_done":  state.done_results.append(payload); state.step_idx += 1; state.retries = 0
    elif event == "step_failed": state.retries += 1; state.last_error = payload
    elif event == "replanned":  state.plan = state.plan[:state.step_idx] + payload; state.retries = 0
    elif event == "answered":   state.final_answer = payload

    # ② 查表转移 —— (当前相位, 事件) → 下一相位
    state.phase = TRANSITIONS.get((state.phase, event), state.phase)

    # ③ 派生路由 —— 表表达不了的转移，看数据说话
    if state.phase == "EXECUTING":
        if state.step_idx >= len(state.plan):  state.phase = "FINISHING"
        elif state.retries >= MAX_RETRIES:     state.phase = "REPLANNING"
    return state
```

四个精读点：

**a) ①和②分离的深意。** `step_done` 的增量是"记录结果、推进指针、清零重试"，它的表转移是 `EXECUTING → EXECUTING`（自环）——**数据变了，相位没变**。真正换相位的时机由第③段看着 `step_idx`/`retries` 的值决定。更新和路由是两件事，混在一起写就会长成 Week 1 那种面条 if/else。

**b) 第②段的查表为什么故意缺两条边。** `("EXECUTING","replan_needed")` 和 `("EXECUTING","all_done")` 不在表里，因为这两个转移**不取决于发生了什么事件，而取决于状态里计数器的值**——事件 `step_failed` 既可能导致原地重试（`retries=1`），也可能导致重规划（`retries=2`）。静态表表达不了条件转移，这就是 LangGraph `add_conditional_edges(lambda s: "replan" if s.retries>=2 else "execute")` 的本质。

**c) `"replanned"` 增量的拼接式。** `state.plan[:state.step_idx] + payload`——保留已完成的前缀，只换尾部。运行中两次重规划后计划从 5 步变 6 步再变 7 步，`done_results` 里北京/上海的结果始终没动。**Dynamic Re-planning 的 "dynamic" 就体现在这行：改的是未来，不推翻过去。**

**d) 一个隐藏的正确性细节。** `"replanned"` 和 `"step_done"` 都把 `retries` 清零。如果 `"replanned"` 忘了清零：回到 EXECUTING 后第③段立即判定 `retries >= MAX_RETRIES` → 再次 REPLANNING → 无限循环。FSM 里**每个进入 EXECUTING 的入口都必须维护 retries 不变量**，这是状态机的典型陷阱。

---

## 第 3 站：`call_llm`——上下文从"历史"变成"渲染"（`plan_execute_fsm.py:141-149`）

```python
def call_llm(client, system: str, user: str) -> str:
    response = client.messages.create(..., messages=[{"role": "user", "content": user}])
```

对比 Week 1 的 `react_loop`：那里每次都把滚雪球的 `messages` 全量重发；这里**每次调用都是单轮**，`user` 参数由相位函数现场拼装。

> **Week 1：状态 = messages 本身（历史即上下文）。
> Week 2：状态 = AgentState，messages 只是每次从状态"渲染"出来的视图。**

谁是 source of truth 变了。好处：上下文天然有界（不受轮数影响，只受 `done_results` 摘要长度影响）；代价：执行器每步"失忆"——它不知道自己上一步的原始输出，只知道 `last_error` 和已完成摘要。两种架构各有适用面：长对话用前者，长任务流水线用后者。

---

## 第 4 站：`compress`——上下文治理的最小实现（`plan_execute_fsm.py:158-163`）

```python
def compress(text, limit=RESULT_CHARS):     # 160 字符/结果
    if len(text) <= limit: return text
    head, tail = limit * 3 // 5, limit * 2 // 5
    return text[:head] + " …[已压缩]… " + text[-tail:]
```

策略是**保头保尾**：头部通常是结论，尾部常有细节。这是"滑动摘要"的确定性替身——生产中会花一次廉价 LLM 调用生成真摘要，权衡相同：**花 200 token 的摘要钱，省每轮重发 2000 token 的历史钱**。调用点在 `phase_execute` / `phase_replan` 拼 `done_part` 时（205、253 行）——注意 `phase_finish`（283 行）**故意不压缩**：最终汇总要全文。"预算策略是按相位定的"，不是全局一刀切。

---

## 第 5 站：四个相位函数——四种 Prompt 契约

每个相位 = 专属 system Prompt + 从状态渲染的 user 上下文 + 固定的输出解析。它们是状态机的"节点逻辑"：

| 相位 | 输入（从 s 渲染） | 输出契约 | emit 事件 |
|---|---|---|---|
| `phase_plan` (171) | 问题 | 编号任务列表 | `planned` |
| `phase_execute` (192) | 问题+已完成摘要+当前任务+失败警告 | `{"tool":…, "args":…}` 单 JSON | `step_done`/`step_failed` |
| `phase_replan` (239) | 问题+已完成+失败步骤+剩余旧计划 | 修订的剩余列表 或 IMPOSSIBLE | `replanned`/`give_up` |
| `phase_finish` (280) | 问题+全部结果 | 自然语言答案 | `answered` |

**`phase_execute` 里的纠错通道**（207-212 行）：`retries > 0` 时注入"已失败 N 次，最近错误：…，请换一种做法"。这是 Week 1 Reflexion 闭环的 FSM 化——反馈不再靠历史里躺着的那条错误消息，而是**显式字段 `last_error` 定向注入**。

**`phase_replan` 的降级出口**（266-268 行）：`IMPOSSIBLE` → `give_up` → 直达 FINISHING 带着已有结果收尾。重规划不是无限循环的许诺，**规划器有权认输**——但要留下"部分结果 + 如实说明缺失"的交代（最终答案里"深圳数据缺失，无法评估"正是 FINISHING 的 system Prompt 中"如实说明"指令的产物）。

**解析容错的降级**（221-227 行）：`json.loads` 失败或 `KeyError` → `step_failed`，错误文本成为 payload。和 Week 1 的三岔路口同理：解析失败也是可重试的业务事件，不是崩溃。

---

## 第 6 站：`run()` 主循环——零业务逻辑的事件泵（`plan_execute_fsm.py:294-314`）

```python
while s.phase != "DONE":
    if s.llm_calls >= MAX_LLM_CALLS: break          # 全局保险丝（按花费熔断，比 Week 1 的 MAX_STEPS 更本质）
    if s.phase == "PLANNING":    s = reduce(s, "planned", phase_plan(client, s))
    elif s.phase == "EXECUTING": s = reduce(s, *phase_execute(client, s))
    ...
```

整个循环体只做一件事：**按当前相位调度节点函数，把它的 (event, payload) 喂给 reduce**。它不知道什么是天气、什么是重试——把 `phase_plan`~`phase_finish` 换成完全不同的四个节点，`run()` 一行不改。这就是"编排与逻辑分离"的手写证明，也是它配得上叫"引擎"而 Week 1 只能叫"循环"的原因。

---

## 对照运行轨迹复盘（第二次运行）

| 运行输出 | 代码路径 |
|---|---|
| 步骤1 北京天气：失败→拼音重试成功 | `step_failed`(retries=1) → 派生路由不动 → 原地重试 → `step_done` 清零 |
| 步骤3 深圳：两次失败（先 weather 后换工具试 AQI） | `step_failed` ×2 → `retries>=2` → **派生路由切 REPLANNING** |
| 第一次 REPLAN："降级补偿，试深圳 AQI" | `phase_replan` → `replanned` → `plan = plan[:3] + 新尾部`，retries 清零 |
| 深圳 AQI 又两次失败 | 第二次进入 REPLANNING（状态机允许重入，但每次都有 LLM 成本） |
| 第二次 REPLAN："彻底舍弃深圳" | 再拼一次前缀——北京/上海结果仍在 |
| 修订计划第 5-7 步产生 `calculator('0')` 垃圾调用 | **接口失配**：计划节点是推理型任务，执行器契约强制工具调用 |
| 最终答案识别并丢弃无标注数据 | `phase_finish` 不压缩全文 + "如实说明"指令 |

## 已知缺陷（比成功更值钱）

垃圾调用的根因链：**replan 输出的自然语言步骤没有类型约束 → 执行器契约只认工具调用 → 模型被逼着"制造"一次调用**。修法三选一（难度递增）：

1. 执行器允许 `{"tool": null}`（跳过），`reduce` 加 `("EXECUTING","skipped")` 转移 —— 课后练习
2. 计划步骤改成结构化 `{type: "tool"|"reason", ...}`
3. 给推理型步骤专门加一个 REASONING 相位 —— LangGraph 多节点编排的雏形

## 自测问题

> `("REPLANNING","give_up")` 为什么指向 FINISHING 而不是直接 DONE？

**答案**：`final_answer` 只在 FINISHING 被填充。直达 DONE 意味着返回一个 `final_answer=""` 的状态，调用方拿到空答案却以为流程正常结束。FSM 设计通则：**终止节点之前必须经过"产出节点"**——所有路径汇聚到 FINISHING 再出闸，保证无论怎么走，出口契约（有答案）不变。对照 LangGraph：所有条件边最终都路由到 END 前的汇聚节点，同一道理。

---

## 与 LangGraph 的概念映射

| 本文件 | LangGraph |
|---|---|
| `TRANSITIONS` 转移表 | `add_node` + 固定边 |
| `reduce()` 第③段 | `add_conditional_edges` |
| `AgentState` dataclass | `StateGraph(StateSchema)` 的 State 定义 |
| 相位函数返回 (event, payload) | 节点返回局部状态更新（partial state） |
| `run()` 事件泵 | `graph.invoke()` / `graph.stream()` 内部循环 |
| `compress()` 上下文治理 | 摘要节点 / `ClearToolUses` 上下文编辑 |

## 后续方向

- 课后练习：`{"tool": null}` + `("EXECUTING","skipped")` 转移，消灭垃圾调用
- Week 3：`done_results` 外置为跨会话记忆（向量/图谱），`AgentState` 只留工作记忆
