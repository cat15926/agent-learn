# -*- coding: utf-8 -*-
"""
练习 1：手写零框架 ReAct 引擎
================================

不使用 LangChain / LangGraph 等任何框架，仅用 Anthropic 官方 SDK 的
`client.messages.create()` 这一个原始 API，手动实现完整的 ReAct 循环：

    Thought → Action → Action Input → (本地执行工具) → Observation → ... → Final Answer

四个任务要求对应位置：
  1. 工具函数 + 手写 JSON Schema（三个）    -> 见 TOOLS 注册表
  2. 系统 Prompt 强制 ReAct 输出格式        -> 见 SYSTEM_PROMPT
  3. while 循环：解析 → 执行 → 回喂        -> 见 react_loop()
  4. 错误容错：异常包装成 Observation 喂回  -> 见 execute_tool() 与 parse_action()

运行方式：
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python react_engine.py
"""

import ast
import json
import operator
import re

import anthropic

# 安全执行 calculator：白名单运算符 + AST 解析，拒绝任何 eval() 直接执行
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node):
    """递归求值 AST 节点，只允许数字与四则运算 —— 防止注入任意代码。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不允许的表达式节点: {type(node).__name__}")


# ---------------------------------------------------------------------------
# 1. 工具函数 + 手写 JSON Schema
#    Schema 就是「给模型看的 API 文档」：模型只依据 name/description/parameters
#    来决定何时调用、传什么参数 —— 描述写得越精确，调用质量越高。
# ---------------------------------------------------------------------------

def get_weather(city: str) -> str:
    """模拟天气 API（本地写死的数据，无需联网）。"""
    fake_db = {
        "beijing": "晴, 26°C, 湿度 40%, 东北风 2 级",
        "shanghai": "多云, 29°C, 湿度 75%, 东南风 3 级",
        "guangzhou": "雷阵雨, 31°C, 湿度 85%, 无持续风向",
    }
    key = city.strip().lower()
    if key not in fake_db:
        # 抛出的异常会被包装成 Observation 喂回模型，让它自己纠错
        raise ValueError(f"未收录城市: {city}（支持: 北京/上海/广州）")
    return f"{city} 今日天气: {fake_db[key]}"


def calculator(expression: str) -> str:
    """只支持四则运算与幂的安全计算器。"""
    tree = ast.parse(expression, mode="eval")
    return str(_safe_eval(tree.body))


def get_air_quality(city: str) -> str:
    """模拟空气质量 API。注意：与 get_weather 不同，这里同时接受中文和拼音。"""
    fake_db = {
        "beijing": "AQI 45（优）, PM2.5: 12",
        "shanghai": "AQI 108（轻度污染）, PM2.5: 38",
        "guangzhou": "AQI 65（良）, PM2.5: 20",
        "北京": "AQI 45（优）, PM2.5: 12",
        "上海": "AQI 108（轻度污染）, PM2.5: 38",
        "广州": "AQI 65（良）, PM2.5: 20",
    }
    key = city.strip().lower() if city.strip().isascii() else city.strip()
    if key not in fake_db:
        raise ValueError(f"未收录城市: {city}（支持: 北京/上海/广州 及其拼音）")
    return f"{city} 空气质量: {fake_db[key]}"


TOOLS = {
    "get_weather": {
        "fn": get_weather,
        "schema": {
            "name": "get_weather",
            "description": "查询指定城市今日的天气情况。仅支持中国主要城市。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如 '北京'、'shanghai'",
                    },
                },
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
    "calculator": {
        "fn": calculator,
        "schema": {
            "name": "calculator",
            "description": "计算算术表达式。支持 + - * / % ** 与括号，不支持函数或变量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "合法的 Python 算术表达式，如 '(3 + 4) * 2'",
                    },
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
    "get_air_quality": {
        "fn": get_air_quality,
        "schema": {
            "name": "get_air_quality",
            "description": "查询指定城市今日的空气质量指数（AQI）。评估户外运动适宜性时必用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，中文（如 '北京'）或拼音（如 'shanghai'）均可",
                    },
                },
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 2. ReAct 系统 Prompt：把工具 Schema 渲染进 Prompt，并强制输出格式
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    tool_docs = "\n\n".join(
        f"### {spec['schema']['name']}\n{spec['schema']['description']}\n"
        f"参数 JSON Schema:\n{json.dumps(spec['schema']['parameters'], ensure_ascii=False, indent=2)}"
        for spec in TOOLS.values()
    )
    return f"""你是一个严格遵循 ReAct 范式的推理助手。可以使用以下工具：

{tool_docs}

每一轮你必须且只能输出如下格式（不要输出任何其他内容）：

Thought: （你对接下来的行动的思考）
Action: （工具名，必须是上面列出的之一）
Action Input: （一个符合该工具 JSON Schema 的 JSON 对象）

当你已经掌握足够信息可以回答问题时，输出：

Thought: （总结推理）
Final Answer: （给用户的最终答案）

注意：
- Action Input 必须是合法 JSON，如 {{\"city\": \"北京\"}}。
- 每次只调用一个工具，等待 Observation 返回后再继续。
- 工具报错时，阅读 Observation 中的错误信息，调整参数重试，不要重复同样的错误。"""


# ---------------------------------------------------------------------------
# 3. 输出解析：从模型文本中提取 Action / Action Input
# ---------------------------------------------------------------------------

_ACTION_RE = re.compile(
    r"Action:\s*(?P<name>\S+)\s*\n\s*Action Input:\s*(?P<input>\{.*?\})\s*$",
    re.DOTALL,
)


def parse_action(text: str):
    """返回 (tool_name, args_dict)；解析失败返回 (None, 错误说明)。"""
    if "Final Answer:" in text:
        return "FINAL", text.split("Final Answer:", 1)[1].strip()
    m = _ACTION_RE.search(text)
    if not m:
        return None, "格式错误：未找到 'Action:' 与 'Action Input:'，请严格按 ReAct 格式输出。"
    name, raw = m.group("name"), m.group("input")
    if name not in TOOLS:
        return None, f"格式错误：未知工具 '{name}'，可用工具: {list(TOOLS)}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"格式错误：Action Input 不是合法 JSON（{e}），请重新输出合法 JSON 对象。"
    return name, args


def execute_tool(name: str, args: dict) -> str:
    """要求 4：任何异常都不中断循环，而是包装成 Observation 喂回模型自我修复。"""
    schema_props = TOOLS[name]["schema"]["parameters"]["properties"]
    # 轻量参数校验：必填项缺失 / 多余键直接报错给模型
    missing = [k for k in TOOLS[name]["schema"]["parameters"]["required"] if k not in args]
    if missing:
        return f"Error: 缺少必填参数 {missing}"
    extra = [k for k in args if k not in schema_props]
    if extra:
        return f"Error: 多余参数 {extra}，允许的参数: {list(schema_props)}"
    try:
        return str(TOOLS[name]["fn"](**args))
    except Exception as e:  # noqa: BLE001 —— 有意捕获一切异常喂回模型
        return f"Error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 4. ReAct 主循环：调用 → 解析 → 执行 → Observation 回喂 → 直到 Final Answer
# ---------------------------------------------------------------------------

MODEL = "claude-opus-5"
MAX_STEPS = 8  # 步数上限，防止死循环烧钱 —— 生产 Agent 的必备保险丝


def react_loop(question: str, client: anthropic.Anthropic | None = None) -> str:
    client = client or anthropic.Anthropic()
    system = build_system_prompt()
    # 消息历史：这是 Agent 的"短期记忆"，API 本身无状态，每轮全量重发
    messages = [{"role": "user", "content": question}]

    for step in range(1, MAX_STEPS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()

        print(f"\n===== Step {step} =====")
        print(text)

        name, payload = parse_action(text)

        if name == "FINAL":
            return payload

        if name is None:
            # 解析失败也是 Observation —— 让模型看到自己的格式错误并纠正
            observation = payload
            print(f"\n[解析失败] {observation}")
        else:
            observation = execute_tool(name, payload)
            print(f"\n>>> Observation: {observation}")

        # 关键两步：assistant 原文入史 + Observation 以 user 身份回喂
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    return f"（达到最大步数 {MAX_STEPS}，循环终止）"


if __name__ == "__main__":
    # 第三个工具加入后：只改了 TOOLS 注册表，react_loop / build_system_prompt /
    # parse_action / execute_tool 一行未动 —— Schema 自动渲染、模型自动学会调用
    answer = react_loop(
        "我想比较北京和上海今天哪个更适合户外跑步？"
        "请综合考虑天气和空气质量（AQI），并说明理由。"
    )
    print("\n" + "=" * 60)
    print("最终答案:", answer)
