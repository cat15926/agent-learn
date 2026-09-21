# -*- coding: utf-8 -*-
"""
Week 4：多 Agent 协作拓扑 —— Supervisor / Router + 人机协同 + 可观测性
=====================================================================

Week 3 结束时 Agent 是"一个人"：一个循环、一套工具、一份记忆。
本周把它变成"一个团队"：

    拓扑（Supervisor 模式）：

                ┌──────────────┐
        问题 ──► │  Supervisor  │ 看全局进度，决定"下一个派谁、派什么活"
                └──────┬───────┘
                       │ route（条件边）
        ┌──────────────┼──────────────────┐
        ▼              ▼                  ▼
  ┌───────────┐  ┌───────────┐    ┌─────────────┐
  │weather_agent│ │ math_agent│    │ editor_agent │  专精 Worker：只看自己的任务
  │天气+空气质量│  │ calculator │    │ publish_report│ 和自己的工具（上下文隔离）
  └─────┬─────┘  └─────┬─────┘    └──────┬──────┘
        └──────────────┴───── 结果写回 ────┘
                       ▼
              SharedState 黑板（findings）
                       │
             含关键动作时 → Human Gate（人工确认/驳回）

    三个生产主题的落点：
    1. Supervisor vs Router：Router 是"一次性分类→单 Agent"（Supervisor 的退化形式），
       Supervisor 是"循环派发→看进度→再派发"。本文件实现 Supervisor，
       验证场景的问题必须走多轮派发才能完成。
    2. Human-in-the-loop：publish_report 标记为关键动作，执行前中断等终端确认；
       驳回意见作为 Observation 喂回 → Supervisor 重新规划（Reflexion 的多 Agent 版）。
    3. 可观测性：TraceCollector 记录每次 LLM 调用的 agent/延迟/token，
       结束时输出调用轨迹 + 预算消耗表 —— LangSmith/Phoenix 的玩具对应物。

    LangGraph 概念对照（练习 2 会用真框架重写）：
    - Supervisor     ≈ add_node("supervisor") + add_conditional_edges 派发
    - Worker         ≈ add_node("weather_agent") 等各节点
    - SharedState    ≈ StateGraph(State) 的共享状态对象（黑板模式）
    - route()        ≈ conditional edge 的路由函数
    - Human Gate     ≈ interrupt_before=["editor_agent"]（框架级挂起/恢复）

运行：python supervisor_agents.py（需 ANTHROPIC_API_KEY；发布环节会在终端等人工确认）
"""

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

# 复用 Week 1 的工具层（单一事实来源的第三次受益）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "week1_react"))
from react_engine import MODEL, TOOLS, execute_tool

MAX_ROUNDS = 10        # Supervisor 派发轮数上限（保险丝，Week 2 的熔断思路）
WORKER_STEPS = 4       # 单个 Worker 内部的工具调用步数上限

# --------------------------------------------------------------------------
# 本地新增工具：发布动作。关键在 CRITICAL_TOOLS —— 人机协同的挂载点
# --------------------------------------------------------------------------

CRITICAL_TOOLS = {"publish_report"}   # 生产对应：数据库写/资金划转/邮件外发


def publish_report(title: str, body: str) -> str:
    """模拟对外发布（玩具版：只打印归档）。真实场景这是不可逆动作。"""
    return f"已发布《{title}》（{len(body)} 字）"


LOCAL_TOOLS = {
    "publish_report": {
        "func": publish_report,
        "schema": {
            "name": "publish_report",
            "description": "将最终报告对外发布。发布即生效，请确保内容完整准确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "报告标题"},
                    "body": {"type": "string", "description": "报告正文"},
                },
                "required": ["title", "body"],
            },
        },
    },
}

ALL_TOOLS = {**TOOLS, **LOCAL_TOOLS}


def render_tools(names: list[str]) -> str:
    return "\n".join(
        f"- {ALL_TOOLS[n]['schema']['name']}: {ALL_TOOLS[n]['schema']['description']}\n"
        f"  参数: {json.dumps(ALL_TOOLS[n]['schema']['parameters'], ensure_ascii=False)}"
        for n in names
    )


# --------------------------------------------------------------------------
# Worker 注册表：每个专精 Agent = 一份角色说明 + 一个工具子集
# --------------------------------------------------------------------------

WORKERS = {
    "weather_agent": {
        "role": "气象数据专家。负责查询天气与空气质量原始数据。",
        "tools": ["get_weather", "get_air_quality"],
    },
    "math_agent": {
        "role": "计算专家。负责一切数值计算。",
        "tools": ["calculator"],
    },
    "editor_agent": {
        "role": "编辑。负责把已有材料整理成报告并发布（发布需人工确认）。",
        "tools": ["publish_report"],
    },
}


# --------------------------------------------------------------------------
# 可观测性：TraceCollector —— 每次模型调用/工具执行都记账
# --------------------------------------------------------------------------

class TraceCollector:
    """玩具版 LangSmith：一行一条事件，结束输出汇总（延迟/调用数/token）。"""

    def __init__(self):
        self.events: list[dict] = []
        self.llm_calls = 0

    def llm(self, agent: str, latency: float, usage, note: str = ""):
        self.llm_calls += 1
        self.events.append({
            "kind": "llm", "agent": agent, "latency": latency,
            "in": usage.input_tokens, "out": usage.output_tokens, "note": note,
        })

    def tool(self, agent: str, name: str, ok: bool, note: str = ""):
        self.events.append({
            "kind": "tool", "agent": agent, "tool": name,
            "ok": ok, "note": note,
        })

    def route(self, to: str, task: str):
        self.events.append({"kind": "route", "to": to, "task": task})

    def summary(self) -> str:
        lines = ["—" * 66, "执行轨迹（Trace 汇总）", "—" * 66]
        for ev in self.events:
            if ev["kind"] == "llm":
                lines.append(f"  [LLM ] {ev['agent']:<14} {ev['latency']:5.1f}s  "
                             f"in={ev['in']:<5} out={ev['out']:<5} {ev['note']}")
            elif ev["kind"] == "tool":
                flag = "✓" if ev["ok"] else "✗"
                lines.append(f"  [TOOL] {ev['agent']:<14} {ev['tool']} {flag} {ev['note'][:50]}")
            elif ev["kind"] == "route":
                lines.append(f"  [ROUTE] supervisor ──► {ev['to']}: {ev['task'][:50]}")
        llm_lat = sum(e["latency"] for e in self.events if e["kind"] == "llm")
        tok_in = sum(e["in"] for e in self.events if e["kind"] == "llm")
        tok_out = sum(e["out"] for e in self.events if e["kind"] == "llm")
        lines.append("—" * 66)
        lines.append(f"  LLM 调用 {self.llm_calls} 次 | 延迟合计 {llm_lat:.1f}s | "
                     f"token {tok_in}(in)/{tok_out}(out)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 共享状态：黑板模式。所有 Worker 的产出写在这里，Supervisor 据此决策
# --------------------------------------------------------------------------

@dataclass
class SharedState:
    question: str
    findings: list[str] = field(default_factory=list)   # 各 Worker 的结果流
    rounds: int = 0
    final_answer: str = ""

    def blackboard(self) -> str:
        return "\n".join(f"  {i}. {f}" for i, f in enumerate(self.findings, 1)) or "  （无）"


# --------------------------------------------------------------------------
# LLM 基础设施
# --------------------------------------------------------------------------

def call_llm(client, trace: TraceCollector, agent: str, system: str, user: str) -> str:
    t0 = time.time()
    resp = client.messages.create(
        model=MODEL, max_tokens=16000, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    trace.llm(agent, time.time() - t0, resp.usage)
    return text


# def call_llm 的 messages 是单条 user —— Supervisor/Worker 都按"任务粒度重建上下文"
# （Week 2 的做法），不维护滚雪球的对话历史。


def parse_json(text: str) -> dict | None:
    """容错解析（Week 1/3 的老教训：模型可能包围栏或夹说明文字）。"""
    import re
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m.group(0) if m else text)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# 人机协同：关键动作闸门。驳回意见变成 Observation 喂回循环
# --------------------------------------------------------------------------

def confirm_gate(name: str, args: dict) -> tuple[bool, str]:
    """interrupt_before 的手写版。返回 (是否批准, 驳回原因)。"""
    print(f"\n⚠  关键动作待确认: {name}({json.dumps(args, ensure_ascii=False)[:120]})")
    try:
        reply = input("   输入 Y 批准发布；输入其他内容 = 驳回并附原因: ").strip()
    except EOFError:  # 非交互环境（管道/CI）：默认驳回，安全侧倾斜
        print("   （非交互环境，默认驳回）")
        reply = "n 非交互环境自动驳回"
    if reply.upper() == "Y":
        return True, ""
    reason = reply[1:].strip() if reply[:1].lower() == "n" else reply
    return False, reason or "未说明原因"


def run_tool(agent: str, name: str, args: dict, trace: TraceCollector) -> str:
    """统一执行入口：关键工具先过人工闸门，再执行（本地/Week1 分发）。"""
    if name in CRITICAL_TOOLS:
        approved, reason = confirm_gate(name, args)
        if not approved:
            trace.tool(agent, name, ok=False, note=f"人工驳回: {reason}")
            return f"Error: 人工驳回——{reason}。请修改后重试或放弃发布。"
    if name in LOCAL_TOOLS:
        result = LOCAL_TOOLS[name]["func"](**args)
    else:
        result = execute_tool(name, args)
    ok = not result.startswith("Error")
    trace.tool(agent, name, ok=ok, note="")
    return result


# --------------------------------------------------------------------------
# Supervisor：看黑板 → 决定下一步派谁。纯决策，不执行
# --------------------------------------------------------------------------

def supervisor_step(client, state: SharedState, trace: TraceCollector) -> dict:
    """输出契约：
    {"action": "route", "to": worker名, "task": "具体任务"}   —— 派发
    {"action": "finish", "answer": "..."}                    —— 收工
    """
    workers_desc = "\n".join(f"- {k}: {v['role']}" for k, v in WORKERS.items())
    system = (
        "你是团队主管（Supervisor）。根据总体问题和当前进度，决定下一步：\n"
        f"可选 Worker：\n{workers_desc}\n\n"
        "规则：给 Worker 的任务必须具体、自包含（它是只看任务不看对话历史的专家）；"
        "已有材料足够回答时就 finish，不要为了用而用工具。\n"
        '只输出 JSON：{"action": "route", "to": "...", "task": "..."} '
        '或 {"action": "finish", "answer": "面向用户的最终答案"}'
    )
    user = f"总体问题：{state.question}\n\n当前已有材料（黑板）：\n{state.blackboard()}"
    action = parse_json(call_llm(client, trace, "supervisor", system, user))
    return action or {"action": "finish", "answer": "（Supervisor 输出解析失败，强制收工）"}


# --------------------------------------------------------------------------
# Worker：专精执行者。只看「自己的任务 + 自己的工具」，与全局隔离
# --------------------------------------------------------------------------

def worker_run(client, name: str, task: str, state: SharedState,
               trace: TraceCollector) -> str:
    spec = WORKERS[name]
    system = (
        f"你是{spec['role']}\n可用工具：\n{render_tools(spec['tools'])}\n\n"
        '每轮只输出一个 JSON：{"tool": 名字, "args": {...}} 或 {"answer": "任务结果"}。'
        "工具报错时阅读错误信息换一种做法。完成即给 answer，不要啰嗦。"
    )
    observations: list[str] = []
    for step in range(1, WORKER_STEPS + 1):
        user = f"你的任务：{task}"
        if observations:               # 任务 + 全部观察重建上下文（有界，步数≤WORKER_STEPS）
            user += "\n\n已有观察：\n" + "\n".join(f"  - {o}" for o in observations)
        elif step > 1:
            user += "\n\n（此前输出格式有误，请只输出 JSON）"
        text = call_llm(client, trace, name, system, user)
        action = parse_json(text)
        if action is None:
            observations = [o for o in observations if "格式" not in o]
            observations.append("上一轮输出不是合法 JSON（已忽略），请只输出 JSON")
            continue
        if "answer" in action:
            return str(action["answer"])
        tool, args = action.get("tool"), action.get("args", {})
        if tool not in spec["tools"]:
            result = f"Error: 你没有工具 {tool}，只能用 {spec['tools']}"
        else:
            result = run_tool(name, tool, args, trace)
        print(f"    [{name}] {tool}({json.dumps(args, ensure_ascii=False)[:60]}) → "
              f"{result[:70]}{'…' if len(result) > 70 else ''}")
        observations.append(f"{tool} → {result}")
    return "（Worker 步数耗尽，未产出结果）"


# --------------------------------------------------------------------------
# 主循环：事件泵。自己没有业务逻辑，只把 Supervisor 的决策翻译成调用
# --------------------------------------------------------------------------

def run(question: str, client=None) -> tuple[SharedState, TraceCollector]:
    client = client or anthropic.Anthropic()
    trace = TraceCollector()
    state = SharedState(question=question)

    while state.rounds < MAX_ROUNDS:
        state.rounds += 1
        print(f"\n◆ 第 {state.rounds} 轮派发")
        decision = supervisor_step(client, state, trace)

        if decision["action"] == "finish":
            state.final_answer = decision.get("answer", "")
            print(f"\n[Supervisor] 收工：{state.final_answer[:80]}…")
            break

        to, task = decision.get("to"), decision.get("task", "")
        if to not in WORKERS:
            print(f"[Supervisor] 未知 Worker {to}，跳过")
            continue
        trace.route(to, task)
        print(f"[Supervisor] ──► {to}: {task}")
        result = worker_run(client, to, task, state, trace)
        state.findings.append(f"[{to}] {result}")   # 写回黑板
        print(f"[{to}] 完成: {result[:80]}{'…' if len(result) > 80 else ''}")

    return state, trace


# --------------------------------------------------------------------------
# 演示：必须多轮派发 + 必经人机闸门的问题
# --------------------------------------------------------------------------

if __name__ == "__main__":
    state, trace = run(
        "对比北京和上海今天的天气与空气质量，算出两地温差多少度，"
        "最后把结论整理成一份简短的跑步建议报告并发布。"
    )

    print("\n" + "=" * 66)
    print("最终答案:", state.final_answer)
    print()
    print(trace.summary())

    # ---- 评估（Evaluation 的玩具版）：对最终产出做确定性断言 ----
    print("\n" + "=" * 66)
    print("评估（确定性检查清单）")
    checks = [
        ("流程走完（未触发轮数熔断）", state.rounds < MAX_ROUNDS),
        ("答案提及北京和上海", "北京" in state.final_answer and "上海" in state.final_answer),
        ("温差经过计算（math_agent 被派发）",
         any(f.startswith("[math_agent]") for f in state.findings)),
        ("发布动作经过人工闸门",
         any(e["kind"] == "tool" and e.get("tool") == "publish_report"
             for e in trace.events)),
    ]
    passed = sum(ok for _, ok in checks)
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    print(f"  得分: {passed}/{len(checks)}")
