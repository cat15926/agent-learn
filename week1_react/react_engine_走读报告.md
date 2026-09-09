# react_engine.py 代码走读报告

> 对象：`react_engine.py`（练习 1：手写零框架 ReAct 引擎）
> 依据：一次真实运行（"比较北京和上海哪个适合户外跑步，并计算温差"，5 步闭环成功）

## 全景：这个文件就是一个最小 Agent

```
用户问题 ──► react_loop() ──► API 调用 ──► 模型输出文本
                ▲                             │
                │                             ▼
        Observation 回喂              parse_action() 解析
        (messages.append)                     │
                ▲                     ┌───────┴────────┐
                │                     ▼                ▼
                └──────────── execute_tool()      Final Answer → 返回
                              (异常→错误文本)
```

整个 Agent 的"智能"只来自两样东西：**模型** + **循环**。框架（LangChain 等）做的事无非是把这几段包装起来。

---

## 第 0 站：安全执行层（`react_engine.py:31-50`）

```python
_BIN_OPS = { ast.Add: operator.add, ... }   # 白名单
def _safe_eval(node):
    if isinstance(node, ast.Constant) ...:  # 数字字面量 → 直接返回
    if isinstance(node, ast.BinOp) ...:     # 二元运算 → 递归左右子树
    raise ValueError(...)                   # 其他一切节点 → 拒绝
```

**为什么先讲它**：`calculator` 的输入是**模型生成的字符串**，等价于不可信用户输入。如果直接 `eval(expression)`，模型输出 `__import__('os').system('rm -rf /')` 就完蛋了。这里的做法是先把字符串解析成 **AST（语法树）**，然后只对「数字」和「白名单运算符节点」递归求值，树里出现任何其他节点类型（函数调用、变量名、属性访问）立即抛错。

对应学习计划中"安全执行：代码沙箱、只读/写入权限隔离"的最小形态：**不是过滤文本，而是限制可执行的语法节点集合**。

---

## 第 1 站：工具注册表（`react_engine.py:59-116`）

```python
TOOLS = {
    "get_weather": {
        "fn": get_weather,              # 宿主侧真正执行的 Python 函数
        "schema": { "name": ..., "description": ..., "parameters": {...} },  # 给模型看的文档
    },
    ...
}
```

关键设计：**函数与 Schema 分离但绑定在同一个注册表项里**。

- `fn` 是**你的代码**要调用的——模型永远看不到它；
- `schema` 是**模型**唯一能看到的——它据此决定"何时调用、传什么参数"。

三个字段各司其职：

| 字段 | 作用 | Demo 中的体现 |
|---|---|---|
| `description`（工具级） | 决定模型**选不选**这个工具 | Step 1 选 `get_weather` 而非 `calculator` |
| 参数级 `description` | 决定参数**格式** | Step 2 看到示例 `'shanghai'` 后改用拼音 |
| `required` + `additionalProperties: False` | 机器可校验的契约 | `execute_tool()` 的硬校验依据 |

教学细节（66-69 行）：`fake_db` 键是拼音，传中文会 `raise ValueError`——一个**确定会失败的工具**，用于反复观察"错误 → 纠错"闭环（Demo Step 1→2 撞上）。

---

## 第 2 站：系统 Prompt 的组装（`react_engine.py:123-147`）

```python
tool_docs = "\n\n".join(f"### {name}\n{description}\n参数 JSON Schema:\n{json.dumps(...)}" ...)
return f"""你是一个严格遵循 ReAct 范式的推理助手。可以使用以下工具：

{tool_docs}
...（格式约定 + 注意事项）"""
```

做了两件事：

1. **把 Schema 动态渲染进 Prompt**——单一事实来源。加第三个工具只需改 `TOOLS`，Prompt 自动跟上。（生产中对应"工具列表的确定性"，也关系到 prompt caching：工具列表不稳定会打碎缓存前缀。）

2. **用 Prompt 定义一套文本协议**：

```
Thought: ...        ← 模型的推理（CoT）
Action: 工具名       ← 决策
Action Input: {...}  ← 参数（JSON）
（等待）Observation: ... ← 环境返回的结果
```

注意最后那条"注意事项"（146-147 行）：*"工具报错时，阅读 Observation 中的错误信息，调整参数重试"*——这句是 **Reflexion 的开关**。没有它，模型面对错误更倾向于道歉而不是重试。

> 历史注脚：原生 Function Calling 出现之前，全世界的 Agent 都靠这套文本协议 + 正则解析工作。手写这段的意义在于看清框架帮你做了什么。

---

## 第 3 站：解析器（`react_engine.py:154-174`）

```python
_ACTION_RE = re.compile(
    r"Action:\s*(?P<name>\S+)\s*\n\s*Action Input:\s*(?P<input>\{.*?\})\s*$",
    re.DOTALL,
)
```

逐段拆：

| 片段 | 作用 |
|---|---|
| `Action:\s*(?P<name>\S+)` | 捕获工具名（非空白串） |
| `\n\s*Action Input:` | 要求 Action 和 Action Input 分两行、顺序固定 |
| `(?P<input>\{.*?\})` | 捕获 JSON 对象体；`?` 非贪婪 + `re.DOTALL`（`.` 也匹配换行）——JSON 跨行也能抓到 |
| `search` 而非 `match` | 从整段输出中**找**协议块，容忍前面有多余的 Thought 文本 |

`parse_action()` 是一个**三岔路口**，返回值决定循环走向：

```python
if "Final Answer:" in text:  return "FINAL", 答案      # → 循环出口
m = _ACTION_RE.search(text)
if not m:                    return None, "格式错误..."   # → 解析失败，也喂回去
if name not in TOOLS:        return None, "未知工具..."   # → 同上
json.loads 失败:              return None, "不是合法JSON..." # → 同上
return name, args                                      # → 正常执行
```

**最重要的一条设计原则**：解析失败不 `raise`、不退出，而是把错误说明当作返回值传下去。模型输出的是自由文本，格式违规是**常态而非异常**——处理方式只能是"把错在哪告诉它，让它重来"。这和传统编程里"解析失败就崩"的直觉完全相反。

---

## 第 4 站：工具执行的三层防线（`react_engine.py:177-190`）

```python
missing = [...required 里不在 args 的]     # 防线1：必填参数缺失
extra   = [...args 里不在 schema 的]       # 防线2：多余参数
try:
    return str(TOOLS[name]["fn"](**args))  # 防线3：函数体异常
except Exception as e:
    return f"Error: {type(e).__name__}: {e}"
```

第 1、2 层是**基于 Schema 的参数校验**，第 3 层是**兜底捕获**——`except Exception` 后不是 log & crash，而是**格式化成字符串继续往下走**。三条路径的出口长得一模一样：一段 `Error: ...` 文本。这就是"错误也是一种 Observation"的统一抽象。

---

## 第 5 站：主循环——Agent 的心脏（`react_engine.py:201-236`）

一轮迭代六步：

```python
for step in range(1, MAX_STEPS + 1):                   # 步数保险丝(198行, MAX_STEPS=8)
    response = client.messages.create(...)             # ① 调 API
    text = "".join(b.text for b in response.content    # ② 抽出文本块
                   if b.type == "text").strip()
    name, payload = parse_action(text)                 # ③ 解析
    if name == "FINAL": return payload                 # ④ 出口
    observation = execute_tool(name, payload)          # ⑤ 执行(或错误文本)
    messages.append({"role": "assistant", "content": text})              # ⑥a 入史
    messages.append({"role": "user", "content": f"Observation: {observation}"})  # ⑥b 回喂
```

**⑥ 是整个 ReAct 的灵魂**。Observation 之所以用 `role: "user"` 回喂，是因为协议里只有 user/assistant 两种角色——"环境"只能借 user 之口说话。走到 Step 3 时，`messages` 长这样：

```python
[
  {"role": "user",      "content": "我想比较北京和上海…温差…"},           # 原始问题
  {"role": "assistant", "content": "Thought: …\nAction: get_weather\nAction Input: {\"city\": \"北京\"}"},  # Step1(失败调用也要留!)
  {"role": "user",      "content": "Observation: Error: ValueError: 未收录城市: 北京…"},                    # 失败的记录
  {"role": "assistant", "content": "Thought: …改用拼音…\nAction: get_weather\nAction Input: {\"city\": \"beijing\"}"},
  {"role": "user",      "content": "Observation: beijing 今日天气: 晴, 26°C…"},
  # 下一轮 API 调用会把以上全部 + Step3 的输出一起发过去
]
```

三个推论，直接通向后几周的内容：

1. **失败调用也留在历史里**——模型 Step 2 的纠错之所以发生，是因为它**看得见** Step 1 的错误。筛掉失败记录反而会削弱它。（memory 的雏形 → Week 3）
2. **API 无状态，每轮全量重发**——`messages` 就是 Agent 的全部短期记忆，轮数多了 token 线性膨胀，必须有滑动窗口/压缩（→ Week 2 上下文治理）。
3. **`MAX_STEPS` 是唯一的安全带**——Prompt 约束（147 行）是软限制，8 步上限才是硬限制。生产级 Agent 还会加：重复检测（连续 N 次相同 Action 直接熔断）、花费上限。

**② 里的小细节**：`response.content` 是内容块列表（可能有 thinking 块、text 块），只拼 `text` 块。因为没用原生 `tools` 参数，不会有 `tool_use` 块——模型把调用协议写死在纯文本里。

---

## 对照运行轨迹复盘

| Demo 输出 | 代码路径 |
|---|---|
| Step 1 传 `{"city": "北京"}` 报错 | ③解析 OK → ⑤防线3 捕获 `ValueError` → 错误文本成为 Observation |
| Step 2 改传 `beijing` 成功 | ⑥a 入史的 Step1 错误 + 147 行 Prompt 指令，共同促成纠错 |
| Step 4 `29 - 26` → `3` | `calculator` → `ast.parse` → `_safe_eval` 递归求值 |
| Step 5 输出 Final Answer | ③ 返回 `"FINAL"` → ④ `return payload` 退出循环 |

---

## 自测问题

> 如果把第 234 行的 `f"Observation: {observation}"` 改成只回喂 `observation`（去掉前缀），引擎还能工作吗？哪个环节会先出问题？

**答案**：多数情况仍能跑，但**退化**——`Observation:` 这个词是文本协议的一部分，去掉后模型需要自己推断"这段 user 消息是工具结果还是用户新发言"，在多轮对话里容易混淆两者的边界；解析器 `_ACTION_RE` 不受影响（它只解析 assistant 输出）。这也是为什么原生 Function Calling 用**结构化的 `tool_result` 内容块**而不是文本前缀来消除歧义。

---

## 后续方向

- a) 写原生 `tool_use` 对照版（约 20 行差异：解析被 API 接管）
- b) 进 Week 2：把 `messages` 升级成带 Reducer 的显式状态机
- c) 构造更刁钻的失败（非法 JSON、循环调用）观察纠错边界
