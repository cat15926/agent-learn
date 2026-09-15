# FSM 如何实现自我纠错

> 对象：`plan_execute_fsm.py`（Week 2）+ `react_engine.py`（Week 1 的错误处理层）
> 核心澄清：FSM 本身不会纠错——它没有智能。真正的"纠错"仍然是 LLM 读到错误文本后换一种做法；
> FSM 的贡献是给纠错提供了"结构"：通道、预算、升级路径。

## 纠错的完整数据流（对照代码）

```
工具抛异常
   │
   ▼
execute_tool() 捕获 → "Error: ValueError: 未收录城市: 深圳…"   ← 错误文本化
   │
   ▼
phase_execute() 判定失败 → return ("step_failed", result)     ← 错误变成事件
   │
   ▼
reduce() ①应用增量: retries += 1; last_error = result         ← 错误有了"去向"（状态字段）
   │
   ▼ (仍在 EXECUTING，派生路由: retries < 2 → 不动)
   │
   ▼
下一次 phase_execute() 读 s.last_error → 注入 Prompt:          ← 错误"定向回喂"
   "该任务已失败 1 次，最近错误：… 请换一种可行的做法"
   │
   ▼
LLM 读到错误 → 换参数重试（北京→beijing）→ 成功 → step_done → retries 清零
```

---

## 五个环节

### 环节 1：错误文本化（Week 1 遗产，`react_engine.py:187-190`）

```python
except Exception as e:
    return f"Error: {type(e).__name__}: {e}"
```

异常被捕获后**不抛出、不吞掉，转成字符串**。这是整个闭环的起点：LLM 只能读文本，所以一切反馈必须先变成文本。Python 的 traceback 对模型毫无用处，`ValueError: 未收录城市: 深圳（支持: …）` 这种带"怎么办"信息的才有用。

### 环节 2：错误变成事件（`plan_execute_fsm.py:230,236`）

```python
ok = not result.startswith("Error")
return ("step_done", result) if ok else ("step_failed", result)
```

失败不是 return None 或 raise，而是和成功走**同一条返回通道**，只是事件名不同。这让"失败"成为状态机的一等公民，而非异常路径。

### 环节 3：错误有了去向（`reduce()` 第①段，`plan_execute_fsm.py:107-108`）

```python
elif event == "step_failed":
    state.retries += 1
    state.last_error = payload
```

FSM 化的关键一步。对比 Week 1：错误反馈是"躺在 messages 历史里等着模型自己看到"；Week 2 把它提升为**显式状态字段 `last_error`**——去向是确定的：下一次执行器一定会读到它，而不是指望模型在长历史里注意到它。

### 环节 4：定向回喂（`phase_execute` 的 retry_part，`plan_execute_fsm.py:207-212`）

```python
retry_part = (
    f"\n注意：该任务已失败 {s.retries} 次，最近错误：{s.last_error}\n"
    "请换一种可行的做法，不要重复同样失败的调用。"
    if s.retries else ""
)
```

三个细节：

- **条件注入**——`retries=0` 时这段不出现，Prompt 保持干净；
- **附带行为指令**——"换一种做法，不要重复"——对抗"盲目重复同一调用"；
- **只注入最近一次错误**——`last_error` 每次被覆盖，反馈是最新的而非堆积的（对比 Week 1 全历史重放）。

**纠错的智能就发生在这里之后**：模型读到"未收录城市: 北京"，推理出"参数示例是拼音格式"，改传 `beijing`。这一步是 LLM 在做，不是状态机在做。

### 环节 5：预算与升级（`reduce()` 第③段，`plan_execute_fsm.py:123-124`）

```python
elif state.retries >= MAX_RETRIES:
    state.phase = "REPLANNING"
```

自我纠错必须**有止损**，否则"错误→重试→错误→重试"就是死循环。两级递进：

- **重试（便宜）**：参数级纠错——改格式、改拼写，多数失败是这种，1 次重试就够；
- **重规划（贵但改变问题）**：重试 2 次还失败，说明不是参数错而是**任务本身不可行**——升级给规划器，让它绕过、降级或舍弃。

### 运行轨迹中的升级链（步骤 3 查深圳天气）

```
失败 1: get_weather("深圳")     ✗ → last_error 注入
失败 2: get_air_quality("深圳") ✗ ← 模型已经在"换一种做法"——换了工具
retries=2 → 派生路由切 REPLANNING → 规划器降级/舍弃
```

失败 2 值得细看：模型的重试不是重复同一调用，而是**主动换了个工具**——retry_part 里那句"换一种可行的做法"在起作用。但深圳根本不在任何工具里，参数级纠错无解，于是预算耗尽、正确升级。

---

## 一个反例证明"结构"的必要性

Week 2 第一次运行（收紧约束**之前**）：查深圳失败后，重试时模型把城市换成广州——"技术上成功"了，`step_done`，但任务是假的。这说明：

> 反馈闭环（环节 1-4）只保证"模型对错误有反应"，**不保证反应是对的**。
> 语义边界（"不得替换任务中的对象"）和升级路径（环节 5）才是把"有反应"约束成"正确纠错"的围栏。

---

## 总结

| 组件 | 职责 | 谁提供智能 |
|---|---|---|
| 错误文本化 | 让错误可被 LLM 阅读 | 你的代码 |
| 事件化 + `last_error` | 让错误有确定去向 | FSM |
| 定向回喂 + 行为指令 | 让错误一定被看到、并附纠错导向 | FSM + Prompt |
| 真正的纠错决策（换拼音/换工具/放弃） | 想出另一种做法 | **LLM** |
| 预算 + 升级路径 | 纠错失败时止损、换问题 | FSM |

准确的说法：**FSM 实现"自我纠错的工程学"（通道、预算、升级），LLM 实现"纠错的智能"（读懂错误、换一种做法）**。

这也是 Reflexion 论文（Shinn et al., 2023）的标准架构：论文里的 verbal feedback 对应环节 1+4，论文外的工程实践补上了环节 5 的止损。
