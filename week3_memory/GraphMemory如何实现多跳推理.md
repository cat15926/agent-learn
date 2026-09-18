# GraphMemory 如何实现多跳推理

> 承接 `memory_agent_走读报告.md` 第 5 站的遗留问题（图记忆注入条件太窄的修法）
> 三个层次：机制上多跳是什么 → 实现上怎么写 → 范式上两条路线

## 1. 多跳推理是什么：答案在两条边之外

当前 `GraphMemory.query()`（`memory_agent.py:144-148`）是**单跳**：给一个实体，返回它直接相连的三元组：

```python
def query(self, entity):
    return [(s, r, o) for s, r, o in self.triples if entity in s or entity in o]
```

单跳能回答："get_weather 的参数格式是什么？"（实体 → 一条边 → 答案）。

多跳问题是：**问题里的实体和答案之间隔着中间节点**。用 Week 3 真实运行沉淀的三元组构造一个：

```
(get_weather) -[支持城市]-> (beijing/shanghai/guangzhou)
(get_weather) -[city参数格式]-> (小写拼音)
(get_air_quality) -[参数格式]-> (中文或拼音均可)
```

问："**上海**该用什么格式查天气？"——问题里的实体是"上海"，但答案（小写拼音）不与"上海"直接相连，路径是：

```
上海 ──(反向:支持城市)──► get_weather ──(city参数格式)──► 小写拼音
        第1跳                        第2跳
```

这正是走读报告里那个缺陷的场景：会话 B 的问题只有"上海/广州"，不含 `get_weather` 字面，单跳查询扑空。**多跳 = 从问题实体出发，走 k 步路径到达答案**。

## 2. 实现机制：双向邻接表 + 带路径的 BFS

第一步，**建索引**（当前 `set` 只支持扫描，不支持"从节点走出去"）：

```python
def build_adjacency(triples):
    adj = {}  # node -> [(邻居, 关系, 方向), ...]
    for s, r, o in triples:
        adj.setdefault(s, []).append((o, r, "out"))
        adj.setdefault(o, []).append((s, r, "in"))   # 反向边：从"上海"能走回"get_weather"
    return adj
```

第二步，**带路径的 BFS**（关键：收集的是**路径**而不是节点，路径本身就是推理链）：

```python
def multi_hop(adj, entity, k=2, max_paths=8):
    """从 entity 出发走最多 k 步，返回所有不绕环的路径。"""
    results, queue = [], [(entity, [])]
    while queue and len(results) < max_paths:
        node, path = queue.pop(0)
        if len(path) == k:
            continue
        for neighbor, relation, direction in adj.get(node, []):
            if neighbor in [p[0] for p in path] + [entity]:  # 防环
                continue
            new_path = path + [(neighbor, relation, direction)]
            results.append(new_path)
            queue.append((neighbor, new_path))
    return results
```

对"上海"跑 `multi_hop(adj, "上海", k=2)`，其中一条路径：

```
[(get_weather, 支持城市, in), (小写拼音, city参数格式, out)]
```

读出来："上海 被 get_weather 支持（反向），get_weather 的 city 参数格式是小写拼音"——**两跳拼出了答案，且路径可解释**。这是它区别于向量检索的本质：向量给"相关度 0.44"，图给"因为 A→B→C 所以"。

三件实现层面必须处理的事：

| 问题 | 处理 |
|---|---|
| **环**（A→B→A） | 路径内去重（`if neighbor in path`） |
| **组合爆炸**（每跳 ×N 条边） | `k` 限制 2-3、`max_paths` 截断、按边的语义相关性排序 |
| **路径太杂**（多数路径是噪音） | 给关系类型加权（`参数格式`/`支持城市` > 附属性关系），或对候选路径再过一次向量检索 |

## 3. 范式分野：确定性遍历 vs LLM-over-subgraph

### 路线 A：确定性遍历（上面的 BFS）

- 查询本身表达成图模式（SPARQL/Cypher：`MATCH (c:City{name:'上海'})<-[:支持城市]-(t)-[:参数格式]->(f) RETURN f`）
- 谁执行：数据库引擎，**零 LLM 成本、结果确定**
- 适用：**模式已知**的查询——"查参数格式"、"查同款工具"这种结构固定的问题
- 局限：查询模式要人预先设计；问题千变万化时枚举不完

### 路线 B：LLM-over-subgraph（GraphRAG 的做法）

- 先用 BFS/向量检索圈出一个**局部子图**（如两跳内的 20 条三元组），序列化成文本，交给 LLM："基于这些事实回答：上海该用什么格式查天气？"
- 谁执行推理：**LLM 在子图上做**——它可以组合任意条边、处理没预料到的问法
- 适用：**模式未知**的复杂问题（"哪些工具支持拼音？哪些工具报错过？"这类没写过查询模板的问题）
- 代价：一次 LLM 调用 + 子图选取质量决定答案质量

路线 B 并没有取代遍历——**BFS 是给 LLM 圈范围的**。路线 B 就是 `MemorySystem.read()` 的升级版（把"向量 top-k"换成"向量 top-k + 两跳子图"），圈出来的东西还是注入 prompt 让 LLM 用。

### 修走读报告缺陷 ② 的完整方案

```
问题 "上海和广州哪里适合跑步"
  → 向量检索: 命中"get_weather 用拼音"经验 (0.44)     ← 现有
  → 从命中经验/问题实体出发 BFS 两跳圈子图            ← 新增, 确定性
  → 子图序列化进记忆块: (get_weather)-[city参数格式]->(小写拼音) ...
  → 注入 system prompt                                  ← 现有
```

**混合检索（hybrid）**：向量管"模糊召回"，图管"精确展开"，各干各的。

## 4. 诚实边界：图规模与多跳价值的关系

当前 `GraphMemory` 只有 2-3 条三元组，多跳的威力显示不出来（图太小，BFS 两跳就到头）。多跳推理的价值随图规模**非线性增长**——这正是知识图谱越大的公司越投 GraphRAG 的原因：

> 向量库加到 10 万条经验时，top-k 里全是"看起来像"的噪音；
> 而图的精确路径不受规模影响（"从上海走两跳"在 10 亿条边里依然只返回确定的那几条）。
> **规模是精确检索的敌人，却是图检索的无关项。**

## 实装清单（待做）

- `build_adjacency()` + `multi_hop()` 加入 `GraphMemory`
- `MemorySystem.read()` 接线：向量命中 → 实体抽取 → 两跳子图 → 序列化注入
- 纯机制演示（零 LLM 成本）："上海"两跳路径打印
