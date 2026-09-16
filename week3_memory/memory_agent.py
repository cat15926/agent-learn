# -*- coding: utf-8 -*-
"""
Week 3：记忆架构 —— 短期 / 长期 / 工作三层记忆 + Memory Read/Write
==================================================================

Week 2 结束时 Agent 的全部状态在 `AgentState` 里，运行结束即消失：
下次运行遇到同一个坑（get_weather 要传拼音），还得再踩一遍。

本周给 Agent 加上跨越运行的生命周期：

    分层记忆（对照学习计划）：
    ┌────────────────────────────────────────────────────────────┐
    │ 工作记忆  当前任务内的轨迹(trace)：本轮 Action/Observation      │ ← Week1 messages / Week2 AgentState
    │ 短期记忆  会话内跨任务：session 的对话历史，会话结束即过期        │ ← ShortTermMemory（生产: Redis, TTL）
    │ 长期记忆  跨会话沉淀的经验与事实，按相似度检索回灌               │ ← VectorMemory + GraphMemory
    └────────────────────────────────────────────────────────────┘      （生产: pgvector + Neo4j）

    记忆三操作：
    - Retrieve    读：任务开始时按与问题的相似度取 top-k，注入 system prompt
    - Consolidate 写：任务结束后用一次 LLM 调用从轨迹提取"值得记住的经验"
    - Forget      忘：重要性随时间指数衰减，低于阈值删除（对抗无限膨胀）

    本文件的 toy embedding（bigram 哈希）是词法级的：
    演示的是"向量检索的机制"（嵌入→余弦→top-k），语义匹配能力需换成真
    embedding 模型（如 Voyage / OpenAI embeddings）才能获得。

验证场景（见 __main__）：
    会话A: 问"北京 vs 上海跑步" → Step1 传中文报错 → 纠错学会拼音（踩坑）
           → consolidate 把教训写入长期记忆
    会话B: 全新会话（短期记忆为空），问"上海 vs 广州跑步"
           → retrieve 注入拼音经验 → Step1 直接传拼音（不再踩坑）
"""

import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "week1_react"))
from react_engine import MODEL, TOOLS, execute_tool

MAX_STEPS = 6          # 单任务步数上限
FORGET_THRESHOLD = 0.1  # 重要性低于此值 → 遗忘
HALF_LIFE_DAYS = 15.0   # 重要性半衰期


# ==========================================================================
# 嵌入：文本 → 定长向量。生产中换成真 embedding 模型的 API 调用。
# ==========================================================================

def embed(text: str, dim: int = 128) -> list[float]:
    """toy 版：字符 bigram 哈希进桶 → L2 归一化。词法级相似（共享词越多越近）。"""
    vec = [0.0] * dim
    for ch in text.lower().replace(" ", ""):
        vec[hash(ch) % dim] += 1.0
    for i in range(len(text) - 1):
        vec[hash(text[i : i + 2].lower()) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ==========================================================================
# 短期记忆：会话级对话历史（生产: Redis, key=session_id, EX=TTL）
# ==========================================================================

class ShortTermMemory:
    """会话内跨任务的上下文。会话结束 → 整体丢弃（或交由 consolidate 沉淀）。"""

    def __init__(self):
        self._store: dict[str, list[dict]] = {}

    def history(self, session_id: str) -> list[dict]:
        return self._store.setdefault(session_id, [])

    def append(self, session_id: str, role: str, content: str):
        self.history(session_id).append({"role": role, "content": content})

    def drop(self, session_id: str):
        self._store.pop(session_id, None)  # 生产: DEL key；TTL 到期自动发生


# ==========================================================================
# 长期记忆 ①：向量记忆（生产: pgvector / Milvus + 真 embedding）
# ==========================================================================

@dataclass
class MemoryEntry:
    text: str
    kind: str                      # episodic(经验教训) / semantic(事实)
    importance: float = 1.0
    ts: float = field(default_factory=time.time)
    vec: list[float] = field(default_factory=list)


class VectorMemory:
    def __init__(self):
        self.entries: list[MemoryEntry] = []

    def write(self, text: str, kind: str = "episodic", importance: float = 1.0):
        self.entries.append(MemoryEntry(text, kind, importance, vec=embed(text)))

    def retrieve(self, query: str, k: int = 3) -> list[tuple[MemoryEntry, float]]:
        """读：按与 query 的余弦相似度取 top-k（生产: ANN 索引，这里全量扫描）。"""
        qv = embed(query)
        scored = [(e, cosine(qv, e.vec)) for e in self.entries]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    def decay_and_forget(self, now: float | None = None) -> list[str]:
        """忘：importance *= 0.5 ** (年龄天数/半衰期)，低于阈值删除。"""
        now = now or time.time()
        forgotten = []
        for e in self.entries[:]:
            age_days = (now - e.ts) / 86400.0
            e.importance *= 0.5 ** (age_days / HALF_LIFE_DAYS)
            if e.importance < FORGET_THRESHOLD:
                self.entries.remove(e)
                forgotten.append(e.text)
        return forgotten


# ==========================================================================
# 长期记忆 ②：图记忆 —— Entity-Relation-Entity 三元组（生产: Neo4j）
# ==========================================================================

class GraphMemory:
    """结构化事实。向量记忆回答"哪条经验相关"，图记忆回答"这个实体的确定事实"。"""

    def __init__(self):
        self.triples: set[tuple[str, str, str]] = set()

    def write_triples(self, triples):
        self.triples.update(tuple(t) for t in triples)

    def query(self, entity: str) -> list[tuple[str, str, str]]:
        return [
            (s, r, o) for s, r, o in self.triples
            if entity in s or entity in o
        ]


# ==========================================================================
# 记忆系统门面：把三层记忆组装成 Agent 可用的 读/写/忘 接口
# ==========================================================================

class MemorySystem:
    def __init__(self):
        self.short_term = ShortTermMemory()
        self.long_term = VectorMemory()
        self.graph = GraphMemory()

    # ---- Read: 任务开始时调用，产出注入 system prompt 的记忆块 ----
    def read(self, question: str, k: int = 2) -> str:
        blocks = []
        hits = self.long_term.retrieve(question, k=k)
        if hits:
            mem_lines = "\n".join(f"  - {e.text}（相关度 {s:.2f}）" for e, s in hits)
            blocks.append(f"长期记忆中与本次任务相关的经验：\n{mem_lines}")
        # 图记忆：问题中出现的实体（工具名）直接查事实
        facts = []
        for tool_name in TOOLS:
            if tool_name in question:
                facts.extend(self.graph.query(tool_name))
        if facts:
            fact_lines = "\n".join(f"  - ({s}) -[{r}]-> ({o})" for s, r, o in facts)
            blocks.append(f"图记忆中的结构化事实：\n{fact_lines}")
        return "\n\n".join(blocks)

    # ---- Consolidate: 任务结束后调用，用一次 LLM 调用从轨迹提取经验 ----
    def consolidate(self, client, question: str, trace: list[tuple[str, str]]):
        system = (
            "你负责从 Agent 运行轨迹中提取值得长期记住的内容。只提取两类：\n"
            "1) memories: 工具用法教训/数据覆盖范围等经验（episodic，写成自包含的短句，"
            "包含触发场景，如'查询XX天气时应…'）；忽略仅对本次问题成立、无复用价值的细节。\n"
            "2) triples: 稳定的结构化事实，(主体, 关系, 客体) 三元组，如 (get_weather, 参数格式, 拼音)。\n"
            '只输出 JSON：{"memories": ["..."], "triples": [["s","r","o"],...]}'
        )
        trace_text = "\n".join(f"  调用 {a} → {r}" for a, r in trace)
        user = f"任务：{question}\n\n运行轨迹：\n{trace_text}"
        response = client.messages.create(
            model=MODEL, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        # 容错解析：模型可能把 JSON 包在 ```json 围栏或附带说明文字里（Week1 的老教训）
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            data = json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            print("[consolidate] 提取输出不是合法 JSON，跳过沉淀")
            return
        for m in data.get("memories", []):
            self.long_term.write(m, kind="episodic")
        self.graph.write_triples(data.get("triples", []))
        print(f"[consolidate] 沉淀 {len(data.get('memories', []))} 条经验, "
              f"{len(data.get('triples', []))} 条三元组")


# ==========================================================================
# Agent 本体：极简 JSON-action 循环（Week3 的焦点是记忆，不是编排）
# ==========================================================================

def render_tools() -> str:
    return "\n".join(
        f"- {spec['schema']['name']}: {spec['schema']['description']}\n"
        f"  参数: {json.dumps(spec['schema']['parameters'], ensure_ascii=False)}"
        for spec in TOOLS.values()
    )


def run_task(client, stm: ShortTermMemory, session_id: str,
             question: str, memory_block: str = "") -> tuple[str, list]:
    """执行一个任务。短期记忆 = 会话内共享的 messages；工作记忆 = 本次 trace。"""
    system = (
        "你是任务执行 Agent。每轮输出恰好一次工具调用，或给出最终答案。\n"
        f"可用工具：\n{render_tools()}\n\n"
        '输出 JSON：{"tool": 名字, "args": {...}} 或 {"answer": "最终答案"}。\n'
        "工具报错时阅读错误信息换一种做法。"
    )
    if memory_block:
        system += f"\n\n{memory_block}"  # ← 长期记忆的注入点：只改 system，循环无感知

    messages = stm.history(session_id)          # ← 短期记忆：同一会话的上一任务上下文在这里
    if not messages:
        messages.append({"role": "user", "content": question})
    else:
        messages.append({"role": "user", "content": f"新任务：{question}"})

    trace = []                                   # ← 工作记忆：本次任务的轨迹
    for step in range(1, MAX_STEPS + 1):
        response = client.messages.create(
            model=MODEL, max_tokens=16000, system=system, messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        try:
            action = json.loads(text)
        except json.JSONDecodeError:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "格式错误，只输出 JSON"})
            continue
        if "answer" in action:
            messages.append({"role": "assistant", "content": action["answer"]})
            return action["answer"], trace
        name, args = action.get("tool"), action.get("args", {})
        result = execute_tool(name, args) if name in TOOLS else "Error: 未知工具"
        trace.append((f"{name}({json.dumps(args, ensure_ascii=False)})",
                      result.split(chr(10))[0]))
        print(f"  Step {step}: {name}({args}) → {result if len(result) < 90 else result[:90] + '…'}")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": f"Observation: {result}"})
    return "（步数耗尽）", trace


# ==========================================================================
# 演示：踩坑会话A → 沉淀 → 遗忘演示 → 冷启动会话B
# ==========================================================================

if __name__ == "__main__":
    client = anthropic.Anthropic()
    mem = MemorySystem()

    print("=" * 62)
    print("【会话A】新 Agent，无任何长期记忆（踩坑）")
    print("=" * 62)
    ans, trace_a = run_task(client, mem.short_term, "session-A",
                            "北京和上海今天哪里更适合户外跑步？看天气就够。")
    print(f"答案: {ans[:120]}…")
    error_rounds_a = sum(1 for _, r in trace_a if r.startswith("Error"))
    print(f"\n>> 会话A 纠错轮数: {error_rounds_a}（踩坑: 天气工具要拼音）")

    print("\n" + "=" * 62)
    print("【Consolidate】从会话A轨迹提取长期记忆")
    print("=" * 62)
    mem.consolidate(client, "比较北京上海天气", trace_a)
    print("向量记忆:")
    for e in mem.long_term.entries:
        print(f"  [{e.kind}] {e.text}")
    print("图记忆三元组:")
    for t in sorted(mem.graph.triples):
        print(f"  ({t[0]}) -[{t[1]}]-> ({t[2]})")

    print("\n" + "=" * 62)
    print("【Forget】重要性衰减演示（不调 LLM，纯机制）")
    print("=" * 62)
    import datetime
    stale_ts = time.time() - 40 * 86400  # 40 天前的低价值记忆
    mem.long_term.write("某次运行耗时 3.2 秒", kind="semantic", importance=0.2)
    mem.long_term.entries[-1].ts = stale_ts
    print(f"遗忘前 {len(mem.long_term.entries)} 条 → 执行 decay_and_forget()")
    gone = mem.long_term.decay_and_forget()
    print(f"遗忘后 {len(mem.long_term.entries)} 条；被遗忘: {gone}")

    print("\n" + "=" * 62)
    print("【会话B】全新会话（短期记忆为空），换一组城市")
    print("=" * 62)
    mem.short_term.drop("session-A")  # 确保演示干净：会话B不偷看会话A的短期记忆
    block = mem.read("上海和广州今天哪里更适合户外跑步？看天气就够。")
    print("注入的记忆块:")
    print(block if block else "  （无）")
    ans, trace_b = run_task(client, mem.short_term, "session-B",
                            "上海和广州今天哪里更适合户外跑步？看天气就够。",
                            memory_block=block)
    print(f"答案: {ans[:120]}…")
    error_rounds_b = sum(1 for _, r in trace_b if r.startswith("Error"))
    print(f"\n>> 会话B 纠错轮数: {error_rounds_b}")
    print("\n" + "=" * 62)
    print(f"结论: 纠错轮数 {error_rounds_a} → {error_rounds_b}；"
          f"{'经验被成功复用 ✓' if error_rounds_b < error_rounds_a else '经验未生效 ✗'}")
