# -*- coding: utf-8 -*-
"""
Week 3 补充：GraphMemory 多跳推理实装
======================================

实装《GraphMemory如何实现多跳推理.md》的三条待办：
  1. build_adjacency() + multi_hop() 加入 GraphMemory（子类扩展，不改原文件）
  2. MemorySystem.read() 接线：向量命中 → 实体抽取 → 两跳子图 → 序列化注入
  3. 纯机制演示（零 LLM 成本）："上海"两跳路径打印

混合检索的数据流（对照文档第 3 节路线 B）：

    问题 ──► 向量检索 top-k（模糊召回，可能不含工具名）
              │ 命中经验文本
              ▼
    种子实体 = 问题文本 ∪ 命中经验文本 中出现的图节点
              │
              ▼
    multi_hop BFS 两跳（确定性展开，带路径）
              │
              ▼
    路径序列化 → 与向量经验合并成记忆块 → 注入 system prompt

运行：python graph_multihop.py（不调任何 LLM，数据取自 Week 3 真实运行沉淀）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "week1_react"))

from memory_agent import GraphMemory, MemorySystem  # noqa: E402


# ==========================================================================
# 待办 1：GraphMemory 的多跳扩展（子类化——开放封闭，不改原类）
# ==========================================================================

# 玩具级实体别名：中文 ↔ 拼音。生产中这是 entity linking 一步
# （NER 抽实体 + 别名表/向量做归一到图节点，如 Neo4j 里的节点别名索引）
CITY_ALIASES = {"上海": "shanghai", "北京": "beijing", "广州": "guangzhou", "深圳": "shenzhen"}


class AugmentedGraph(GraphMemory):
    """GraphMemory + 邻接索引 + 实体归一 + 带路径的 k 跳 BFS。"""

    def build_adjacency(self) -> dict[str, list[tuple[str, str, str]]]:
        """三元组集合 → 双向邻接表。反向边让"上海"能走回"get_weather"。"""
        adj: dict[str, list[tuple[str, str, str]]] = {}
        for s, r, o in self.triples:
            adj.setdefault(s, []).append((o, r, "out"))
            adj.setdefault(o, []).append((s, r, "in"))
        return adj

    def multi_hop(self, entity: str, k: int = 2, max_paths: int = 8):
        """从 entity 出发走最多 k 步，返回所有无环路径。

        路径元素是 (节点, 经由的关系, 方向)，方向 in 表示走的是反向边。
        返回路径而非节点——路径本身就是可解释的推理链。
        """
        adj = self.build_adjacency()
        if entity not in adj:
            return []
        results, queue = [], [(entity, [])]
        while queue and len(results) < max_paths:
            node, path = queue.pop(0)
            if len(path) == k:
                continue
            for neighbor, relation, direction in adj.get(node, []):
                visited = [entity] + [p[0] for p in path]
                if neighbor in visited:          # 防环：A→B→A
                    continue
                new_path = path + [(neighbor, relation, direction)]
                results.append(new_path)
                queue.append((neighbor, new_path))
        return results[:max_paths]

    def resolve_entity(self, surface: str) -> str | None:
        """表面词 → 图节点。精确命中 → 拼音别名匹配复合节点（"上海" → beijing/shanghai/guangzhou）。"""
        adj = self.build_adjacency()
        if surface in adj:
            return surface
        cand = CITY_ALIASES.get(surface, surface.lower())
        for node in adj:
            if cand in {piece.lower() for piece in node.split("/")}:
                return node
        return None


def augment(mem: MemorySystem) -> MemorySystem:
    """把 MemorySystem 里的 GraphMemory 换成多跳版（三元组原样共享）。"""
    g = AugmentedGraph()
    g.triples = mem.graph.triples
    mem.graph = g
    return mem


# ==========================================================================
# 待办 2：read() 接线 —— 混合检索（向量召回 + 图两跳展开）
# ==========================================================================

def serialize_path(entity: str, path: list[tuple[str, str, str]]) -> str:
    """路径 → 人类可读的推理链。反向边显示为 <-[关系]-。"""
    parts = [entity]
    for node, relation, direction in path:
        arrow = f"-[{relation}]->" if direction == "out" else f"<-[{relation}]-"
        parts.append(f" {arrow} {node}")
    return "".join(parts)


def hybrid_read(mem: MemorySystem, question: str, k: int = 2, hops: int = 2) -> str:
    """MemorySystem.read() 的多跳增强版。

    差异只在图这一路：原版要求问题文本包含工具名字面（单跳、召回差）；
    本版让种子实体同时来自「问题」和「向量命中的经验文本」——
    向量管模糊召回（找到相关经验），图管精确展开（沿经验提到的工具走两跳）。
    """
    blocks = []
    hits = mem.long_term.retrieve(question, k=k)
    if hits:
        mem_lines = "\n".join(f"  - {e.text}（相关度 {s:.2f}）" for e, s in hits)
        blocks.append(f"长期记忆中与本次任务相关的经验：\n{mem_lines}")

    # ---- 种子实体抽取：图节点（含别名变体）出现在「问题 ∪ 命中经验文本」中即命中 ----
    corpus = question + " " + " ".join(e.text for e, _ in hits)
    seeds = set()
    for node in mem.graph.build_adjacency():
        # 复合节点（如 "beijing/shanghai/guangzhou"）拆开逐段匹配；每段再带上中文别名
        for piece in node.split("/"):
            variants = {piece} | {zh for zh, py in CITY_ALIASES.items() if py == piece.lower()}
            if any(len(v) >= 2 and v in corpus for v in variants):
                seeds.add(node)
                break

    # ---- 两跳展开 + 序列化 ----
    chains = []
    for seed in sorted(seeds):
        for path in mem.graph.multi_hop(seed, k=hops, max_paths=4):
            chains.append(serialize_path(seed, path))
    if chains:
        chain_lines = "\n".join(f"  - {c}" for c in chains)
        blocks.append(f"图记忆两跳推理链（{hops} 跳内的事实展开）：\n{chain_lines}")
    return "\n\n".join(blocks)


# ==========================================================================
# 待办 3：纯机制演示（零 LLM）——数据取自 Week 3 真实运行
# ==========================================================================

if __name__ == "__main__":
    # 复刻会话A consolidate 的真实产出（不调 LLM）
    mem = augment(MemorySystem())
    mem.long_term.write(
        "调用 get_weather 查询城市天气时，city 参数应使用小写拼音（如 beijing、shanghai）；"
        "传入中文城市名（如 北京）会报 ValueError 未收录错误，即使错误提示中的支持列表以中文显示",
        kind="episodic",
    )
    mem.graph.write_triples([
        ["get_weather", "city参数格式", "小写拼音"],
        ["get_weather", "支持城市", "beijing/shanghai/guangzhou"],
        ["get_air_quality", "参数格式", "中文或拼音均可"],
    ])

    print("=" * 62)
    print('【演示1】multi_hop("上海", k=2) —— 实体归一 + 两跳路径（零 LLM）')
    print("=" * 62)
    seed = mem.graph.resolve_entity("上海")
    print(f'  "上海" --实体归一--> 图节点 "{seed}"')
    for path in mem.graph.multi_hop(seed, k=2, max_paths=8):
        print(f"  {serialize_path('上海', path)}")

    print()
    print("=" * 62)
    print("【演示2】原版 read() —— 单跳，问题不含工具名 → 图记忆缺席")
    print("=" * 62)
    question = "上海和广州今天哪里更适合户外跑步？看天气就够。"
    old_block = mem.read(question)
    print(old_block if old_block else "  （无）")
    has_graph_old = "图记忆" in old_block

    print()
    print("=" * 62)
    print("【演示3】hybrid_read() —— 向量命中 → 实体抽取 → 两跳子图 → 注入")
    print("=" * 62)
    new_block = hybrid_read(mem, question)
    print(new_block)
    has_graph_new = "两跳推理链" in new_block

    print()
    print("=" * 62)
    print(f"结论: 图记忆注入 {'缺席 → 生效 ✓' if not has_graph_old and has_graph_new else '未变化'}")
    print("（种子'上海'经反向 [支持城市] 到 get_weather，再经 [city参数格式] 到'小写拼音'")
    print("  ——两跳拼出了原版单跳查不到的答案，且路径可解释）")
