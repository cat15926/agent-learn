# -*- coding: utf-8 -*-
"""
Week 2：从消息列表到状态机 —— Plan / Execute / Replan / Finish
================================================================

Week 1 的引擎把全部状态隐式地存在 messages 数组里，控制流是单一的
while 循环。本周把它升级为显式建模：

    1. 有限状态机 (FSM)：PLANNING → EXECUTING ⇄ REPLANNING → FINISHING → DONE
    2. Reducer：        S_{t+1} = Reducer(S_t, ΔS)     ← 状态更新与转移分离
    3. Dynamic Re-planning：单步连续失败 → 不再硬重试，而是重新规划剩余步骤
    4. 上下文治理：     已完成结果不再全量入 Prompt，超预算时压缩为摘要

与 Week 1 的关系：
    - 工具注册表 TOOLS / 安全执行 execute_tool 直接 import 复用（分层）
    - LLM 调用不再共用一个滚雪球的 messages，而是每个相位按需重建上下文
      —— 这就是"从 Message List 到 State Machine"的具体含义

状态机拓扑：

            ┌──────────┐
   问题 ──► │ PLANNING │ 生成初始任务列表
            └────┬─────┘
                 ▼
            ┌──────────┐  失败(重试<上限)   ┌──────────┐
            │EXECUTING │ ────同步骤重试───► │EXECUTING │
            └────┬─────┘                   └──────────┘
                 │ 连续失败≥上限                ▲   │
                 ▼                             │   │ 全部步骤完成
            ┌──────────┐   修订剩余计划       │   ▼
            │REPLANNING│ ────────────────────┘ ┌──────────┐
            └────┬─────┘                       │FINISHING │──► DONE
                 │ 判定不可行                   └──────────┘
                 └─────────────────────────────►┘

运行：python plan_execute_fsm.py（需 ANTHROPIC_API_KEY）
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

# 复用 Week 1 的工具层：把 week1_react 加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "week1_react"))
from react_engine import MODEL, TOOLS, execute_tool

# --------------------------------------------------------------------------
# 常量：保险丝与预算
# --------------------------------------------------------------------------
MAX_RETRIES = 2       # 单步最大重试次数，超过即触发 REPLANNING
MAX_LLM_CALLS = 24    # 全局 LLM 调用上限（防失控烧钱的硬限制）
RESULT_CHARS = 160    # 上下文预算：每个已完成结果保留的字符上限

# --------------------------------------------------------------------------
# 状态定义：Week 1 里散落在 messages 里的隐式状态，这里全部显式化
# --------------------------------------------------------------------------


@dataclass
class AgentState:
    """S_t —— Agent 的全部状态。任何相位只通过 reduce() 修改它。"""

    question: str
    phase: str = "PLANNING"
    plan: list[str] = field(default_factory=list)      # 任务列表（Plan-and-Execute）
    step_idx: int = 0                                  # 当前执行到第几步
    done_results: list[str] = field(default_factory=list)  # 已完成步骤的结果
    retries: int = 0                                   # 当前步骤的连续失败次数
    last_error: str = ""                               # 最近一次错误（喂给重试/重规划）
    final_answer: str = ""
    llm_calls: int = 0


# --------------------------------------------------------------------------
# 状态转移表：FSM 的"图"。对比 LangGraph 的 add_node / add_conditional_edges
# --------------------------------------------------------------------------

TRANSITIONS = {
    ("PLANNING", "planned"): "EXECUTING",
    ("EXECUTING", "step_done"): "EXECUTING",       # 推进到下一步
    ("EXECUTING", "step_failed"): "EXECUTING",     # 原地重试（retries 已累加）
    ("REPLANNING", "replanned"): "EXECUTING",      # 用修订后的计划继续
    ("REPLANNING", "give_up"): "FINISHING",        # 工具无法达成，带着已有结果收尾
    ("FINISHING", "answered"): "DONE",
}
# 注意表中没有 ("EXECUTING","replan_needed") 和 ("EXECUTING","all_done")：
# 这两个转移是"派生的"——取决于 step_idx / retries 的值，见 reduce() 第 3 段。


def reduce(state: AgentState, event: str, payload) -> AgentState:
    """S_{t+1} = Reducer(S_t, ΔS)。三段式：① 应用增量 ② 查表转移 ③ 派生路由。

    这是纯函数式的核心：相位逻辑只 emit 事件，不直接改状态；
    所有状态变更集中在这一个函数里 —— 排查"状态怎么变成这样的"只看这里。
    """
    # ① 应用增量 ΔS
    if event == "planned":
        state.plan = payload
    elif event == "step_done":
        state.done_results.append(payload)
        state.step_idx += 1
        state.retries = 0
    elif event == "step_failed":
        state.retries += 1
        state.last_error = payload
    elif event == "replanned":
        # 保留已完成部分，替换剩余部分 —— 计划是"动态"的
        state.plan = state.plan[: state.step_idx] + payload
        state.retries = 0
    elif event == "answered":
        state.final_answer = payload

    # ② 查表转移
    state.phase = TRANSITIONS.get((state.phase, event), state.phase)

    # ③ 派生路由（等价于 LangGraph 的 conditional edge）
    if state.phase == "EXECUTING":
        if state.step_idx >= len(state.plan):
            state.phase = "FINISHING"          # 计划走完 → 汇总
        elif state.retries >= MAX_RETRIES:
            state.phase = "REPLANNING"         # 重试耗尽 → 重新规划
    return state


# --------------------------------------------------------------------------
# 工具文档渲染（复用 Week 1 注册表 —— 单一事实来源的第二次受益）
# --------------------------------------------------------------------------


def render_tools() -> str:
    return "\n\n".join(
        f"- {spec['schema']['name']}: {spec['schema']['description']}\n"
        f"  参数: {json.dumps(spec['schema']['parameters'], ensure_ascii=False)}"
        for spec in TOOLS.values()
    )


def call_llm(client, system: str, user: str) -> str:
    """所有相位共用的最小 LLM 调用。注意：无 messages 历史 —— 上下文按相位重建。"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


# --------------------------------------------------------------------------
# 上下文治理：超出预算的结果压缩为"滑动摘要"
# （确定性截断版；生产中会用一次廉价 LLM 调用做真摘要，思路相同：花小钱省大钱）
# --------------------------------------------------------------------------


def compress(text: str, limit: int = RESULT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 5, limit * 2 // 5
    return text[:head] + " …[已压缩]… " + text[-tail:]


# --------------------------------------------------------------------------
# 四个相位：每个相位 = 一个专门的 Prompt + 一次 LLM 调用 + 事件输出
# --------------------------------------------------------------------------


def phase_plan(client, s: AgentState) -> list[str]:
    """PLANNING：把问题拆解为编号任务列表（Plan-and-Execute 的 Plan）。"""
    system = (
        "你是任务规划器。把用户问题拆解为最多 5 个编号步骤，"
        "每步必须是\"一次工具调用即可完成\"的具体任务，按依赖顺序排列。\n"
        f"可用工具：\n{render_tools()}\n\n只输出编号列表，不要其他内容。"
    )
    user = f"问题：{s.question}"
    text = call_llm(client, system, user)
    s.llm_calls += 1
    tasks = [
        line.strip().split(".", 1)[-1].strip()
        for line in text.splitlines()
        if line.strip()[:1].isdigit()
    ]
    print(f"\n[PLANNING] 初始计划（{len(tasks)} 步）:")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t}")
    return tasks


def phase_execute(client, s: AgentState):
    """EXECUTING：执行当前步骤 —— 单步单工具调用。

    与 Week 1 的关键差异：上下文不是滚雪球的 messages，而是
    「问题 + 已完成结果的压缩摘要 + 当前任务」按需重建 → 天然有界。
    """
    system = (
        "你是任务执行器。根据给定任务输出恰好一次工具调用。\n"
        "必须严格完成给定任务本身——不得替换任务中的城市/对象来\"曲线达成\"。\n"
        f"可用工具：\n{render_tools()}\n\n"
        '只输出一个 JSON 对象：{"tool": 工具名, "args": {参数}}，不要输出其他内容。'
    )
    done_part = "\n".join(
        f"  {i}. {compress(r)}" for i, r in enumerate(s.done_results, 1)
    ) or "  （无）"
    retry_part = (
        f"\n注意：该任务已失败 {s.retries} 次，最近错误：{s.last_error}\n"
        "请换一种可行的做法，不要重复同样失败的调用。"
        if s.retries
        else ""
    )
    user = (
        f"总体问题：{s.question}\n\n已完成步骤的结果：\n{done_part}\n\n"
        f"当前任务（第 {s.step_idx + 1} 步）：{s.plan[s.step_idx]}{retry_part}"
    )
    text = call_llm(client, system, user)
    s.llm_calls += 1

    # 解析 + 执行（错误也走 Observation 语义：失败信息留给下一次重试）
    try:
        action = json.loads(text)
        name, args = action["tool"], action["args"]
        if name not in TOOLS:
            return "step_failed", f"未知工具 {name}"
    except (json.JSONDecodeError, KeyError) as e:
        return "step_failed", f"输出不是合法的 Action JSON（{e}）: {text[:120]}"

    result = execute_tool(name, args)
    ok = not result.startswith("Error")
    print(
        f"\n[EXECUTING] 步骤 {s.step_idx + 1}/{len(s.plan)}: {s.plan[s.step_idx]}\n"
        f"  调用 {name}({args}) → {'✓' if ok else '✗ 重试 ' + str(s.retries + 1) + '/' + str(MAX_RETRIES)}\n"
        f"  结果: {result}"
    )
    return ("step_done", result) if ok else ("step_failed", result)


def phase_replan(client, s: AgentState):
    """REPLANNING：Dynamic Re-planning。

    触发条件：单步重试耗尽。做法：把「原问题 + 已完成结果 + 失败历史」
    交给规划器，修订"剩余"计划 —— 而不是从头再来（已完成的工作不浪费）。
    """
    system = (
        "你是任务规划器。此前的计划某一步用可用工具反复失败。请基于已有信息"
        "修订【剩余】步骤：可以绕过、降级或舍弃无法完成的部分。\n"
        f"可用工具：\n{render_tools()}\n\n"
        "只输出修订后的编号任务列表（仅剩余步骤）；若问题已无法用现有工具推进，"
        "只输出 IMPOSSIBLE。"
    )
    done_part = "\n".join(
        f"  {i}. {compress(r)}" for i, r in enumerate(s.done_results, 1)
    ) or "  （无）"
    remain_part = "\n".join(
        f"  {i}. {t}" for i, t in enumerate(s.plan[s.step_idx:], s.step_idx + 1)
    )
    user = (
        f"总体问题：{s.question}\n\n已完成：\n{done_part}\n\n"
        f"失败步骤：「{s.plan[s.step_idx]}」已连续失败 {s.retries} 次，"
        f"最近错误：{s.last_error}\n\n剩余旧计划：\n{remain_part}"
    )
    text = call_llm(client, system, user)
    s.llm_calls += 1

    if "IMPOSSIBLE" in text:
        print(f"\n[REPLANNING] 判定不可行，带已有结果收尾")
        return "give_up", None
    tasks = [
        line.strip().split(".", 1)[-1].strip()
        for line in text.splitlines()
        if line.strip()[:1].isdigit()
    ]
    print(f"\n[REPLANNING] 修订剩余计划（{len(tasks)} 步）:")
    for i, t in enumerate(tasks, s.step_idx + 1):
        print(f"  {i}. {t}")
    return "replanned", tasks


def phase_finish(client, s: AgentState) -> str:
    """FINISHING：汇总所有结果，生成面向用户的最终答案。"""
    system = "你是总结者。基于已有结果回答用户问题；若部分内容因工具限制缺失，如实说明。"
    done_part = "\n".join(f"  {i}. {r}" for i, r in enumerate(s.done_results, 1))
    user = f"问题：{s.question}\n\n收集到的结果：\n{done_part}"
    s.llm_calls += 1
    return call_llm(client, system, user)


# --------------------------------------------------------------------------
# 主循环：驱动 FSM。它自己没有任何业务逻辑 —— 只是"事件泵"
# --------------------------------------------------------------------------


def run(question: str, client: anthropic.Anthropic | None = None) -> AgentState:
    client = client or anthropic.Anthropic()
    s = AgentState(question=question)

    while s.phase != "DONE":
        if s.llm_calls >= MAX_LLM_CALLS:
            print(f"\n[熔断] 达到 LLM 调用上限 {MAX_LLM_CALLS}")
            break

        if s.phase == "PLANNING":
            s = reduce(s, "planned", phase_plan(client, s))
        elif s.phase == "EXECUTING":
            event, payload = phase_execute(client, s)
            s = reduce(s, event, payload)
        elif s.phase == "REPLANNING":
            event, payload = phase_replan(client, s)
            s = reduce(s, event, payload)
        elif s.phase == "FINISHING":
            s = reduce(s, "answered", phase_finish(client, s))

    return s


if __name__ == "__main__":
    # 深圳不在任何工具的数据库里 → 天然触发 step_failed → 重试耗尽 → REPLANNING
    # 观察点：重规划如何"绕过"缺失数据，而不是死磕
    s = run(
        "帮我评估在北京、上海、深圳三地中，哪里今天最适合户外跑步？"
        "综合考虑天气和空气质量；如果信息拿不全，基于可得信息给出结论并说明缺了什么。"
    )
    print("\n" + "=" * 60)
    print("最终答案:", s.final_answer)
