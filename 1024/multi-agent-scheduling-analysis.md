---
title: "Clowder AI 多 Agent 协作调度原理分析"
doc_kind: architecture
topics: [scheduling, dispatch, a2a, routing, ball-custody, queue, turn-taking, work-assignment, runtime, context-assembly]
created: 2026-10-31
updated: 2026-10-31
status: analysis
author: "Ragdoll/宪宪 (@cat-f73ajesv, claude-opus-5-5)"
description: "从设计文档与运行时代码双线实查的多 agent 协作调度全景：投递内核、@路由六层、球权状态机、任务/工作分配、runtime 会话与上下文组装，含关键文件坐标与设计取舍评价。"
---

# Clowder AI 多 Agent 协作调度原理分析

**Author**: Ragdoll/宪宪 (@cat-f73ajesv, Claude Opus 5.5)
**Date**: 2026-10-31
**方法**: 官方架构文档（`a2a-protocol.md`、`at-mention-routing-system.md`、`f290-communication-model.md`）与代码实查双线交叉。文件坐标基于本 checkout（分支 `codex/local-claude-dsh`），行号随上游演进会漂移，以符号名（函数/类/常量）为稳定锚点。

---

## 一、设计哲学：一句话说透

**机械路由，智能代理（Mechanical routing, intelligent agents）。**

整个调度系统刻意分成两层：

- **代码层（确定性、可测试、不涉及 LLM）**：只负责"消息送到谁手上"——解析、入队、排序、投递、护栏。
- **LLM 层（非确定性、感知上下文）**：只负责"接不接、传给谁、要不要升给人"——每只猫收到 @ 后三选一：**接 / 退 / 升**。

系统不用程序猜用户意图（那是 LLM 级任务，正则/启发式必然脆），而是机械地路由、让接收方 agent 自己判断。"退"不是系统功能——它是 LLM 遵守路由规范后的涌现行为；系统只看到一次新的普通 @ 事件，没有任何特殊"重定向"代码路径。

第二条贯穿性原则来自投递内核（`docs/architecture/a2a-protocol.md` 设计原则五规则）：

1. **One owner per fact** — 每个事实（排队输入、已准入 invocation、正文暴露、公开结果、结构化责任）只有一个 owner，别人只引用、不复制、不再裁决。
2. **Change on one cutover** — 状态变化只发生在精确 target 的持久 receiver/ref 提交那一刻。
3. **Don't infer one fact from another** — dispatched ≠ seen；settled ≠ handled；入队 targets 只是意图，dispatch 时重验。
4. **One terminal per run** — 每次 admitted run 恰好一个原位终局；已提交的 verdict 不可撤销或重复。
5. **Projections are rebuildable, fail closed** — 头像、"processing"、refs 都是 canonical 事实的投影；证据缺失或含糊时省略动态声明并给诊断，绝不显示伪造的"已读/工作中/已完成"。

---

## 二、消息投递内核：一条消息的七步生命周期

一切入口（用户、IM connector、成员猫、系统、通知、定时器）汇入**同一个持久优先队列**。这是整个调度的底座。

### 2.1 三个被追踪的对象

| 对象 | 持久化 | 是什么 |
|---|---|---|
| **Queue Entry** | 是 | 有序待处理输入 + 可恢复的源身份 + 入队时目标*意图*；`targets[]` 只含仍 pending 的接收者 |
| **Chat History Message** | 是 | 已进入聊天面板的消息，固定 order key；实际投递的公开消息携带因果 `dispatchRefs` |
| **Active Run** | 否（进程本地） | 输入 ↔ Agent Client invocation ↔ 响应气泡的投递关联；不是重启真相 |

关键区分是**发布**而非持久性：排队的用户/connector 输入有一条持久源记录 + 一个 Queue Entry，但在首次实际投递把源物化进 History 之前，对聊天面板和其他 agent 的上下文**不可见**。agent 作者的消息已是公开 History，其 Queue Entry 只携带仍需投递的 targets。

### 2.2 七步旅程

1. **源形成消息** — 用户/connector/系统/agent 决定内容与显式 targets。所有已认证入口委托给共同 send/admission。无有效可路由 mention 的普通公开输入按"最近仍可路由的 completed 回复者 → 配置默认"绑定 fallback；fallback 不改写正文、不伪造 mention。
2. **源与 Queue custody 一起持久化** — 共同准入解析显式意图或 thread/global/product 默认，原子写一条持久源记录 + 一条 Queue Entry（含 pending `targets[]`）。公开输入、agent wake、私密输入进**同一个**队列，然后通知 drain。
3. **按目标保序** — 每 thread 一个 drain 按 comparator 扫描。被阻塞/重试的 source 持住它的 targets，同目标的新工作不能越序；无关目标照常推进。新的普通回合领取**一个 source 的完整 pending target set**，不会只挑空闲子集。
4. **准入** — 一次 source claim 为 admitted set 里每个 target 创建/确认独立 receiver 与 Turn Execution；每个持久 receiver 追加精确 `dispatched` ref、把源物化进 History、只从 Queue 删除自己的 target。**持久 cutover 提交之后**才创建进程本地 Active Run 并调用 agent client。某个 target 在 cutover 前失败只恢复它自己，不回滚 siblings。
5. **同一个气泡流式** — 输出只更新准入时创建的那个气泡。成员内部的 session rollover、compaction、continuation 都留在 agent client 内，不产生新内核对象。
6. **原位终局** — completed/failed/canceled finalize **同一个**气泡；一个持久事务里每个结构化 owner 提交自己的 disposition，只创建该结果允许的 follow-up。
7. **释放再继续** — 终局提交后释放 Active Run，drain 再跑——下一个排队项不会静默滞留。

### 2.3 排序与"不空转"不变量

- **一个持久队列 + 一个稳定 comparator**：手动置顶（按位置）> urgent > normal > 入队时间 FIFO。**没有从消息种类/来源/类别派生的隐藏第三优先级**。拖拽重排按可见顺序原子提交并对 expected queue revision 校验——只有显式手动操作能覆盖默认优先级。
- **事件驱动 drain，每线程一个 owner**：准入、队列变更、持久 handoff、client ready、执行终局、外部 owner 证据、启动恢复、显式重试 deadline 都可请求再扫。没有独立 watchdog。
- 结构性不变量：**不可能稳定处于"队列非空 + 有可执行候选 + 没有负责的 drain/重试唤醒"**——没有任何东西会静默堆积。
- **线程独立**：每 thread 有自己的 drain 和 pending 队列；跨线程共享只能是显式记录的 cross-post。

### 2.4 消息终局与 handoff

- 存活 run 恰好一个终局：completed / failed / canceled；第四种 **interrupted** 由启动恢复合成（run 已准入但 live client 消失），沿失败路径收敛。部分流式输出保留并加状态页脚，不换成通用错误。已提交终局是最终的：同代 replay 返回已提交结果，不产生第二个回答。
- 两种 "handoff" 只有一种对协议可见：
  - **成员内部 continuation**（compaction/session rollover）——对生命周期不可见，仍是同一 invocation、同一气泡。
  - **协议可见 A2A handoff**——已完成的回复（或显式 `post_message`）携带有效 target，创建引用**同一条响应消息**的 wake（不复制成第二条消息），下一跳开始。
- 每一跳独立终局、只关闭自己的 `input → target` 步。B 回答 A 时提到 D，不会重开 A→B、也不会让 A 递归等 D。精确 A2A **失败**结果原子创建一个幂等的 `a2a_failure` 消息引用型 control wake 给调用方，指向原始失败响应、不复制正文、不能递归失败回去。
- 用户和 agent 读**同一个顺序**（History order key 串联）。A 先开始 B 先完成，最终顺序仍是 A 气泡 → B 气泡。`processing` 气泡是 agent 读游标不可越过的**排序屏障**。

### 2.5 用户控制（四操作，不属于正常调度）

Guide reply（追加输入到当前精确回复，不新建 run）/ Interrupt reply（取消存活 run 再准入新的）/ Cancel queued（删未投递 entry）/ Stop agent（快照并取消存活 run）。它们从不停顿或重排队列。

---

## 三、@ 路由六层流水线

`docs/architecture/at-mention-routing-system.md` 的分层与代码验证：

| 层 | 职责 | 权威实现 |
|---|---|---|
| 1 提及解析 | 行首 @ 才是路由指令；剥围栏代码块、token 边界、最长匹配 | `packages/api/src/domains/cats/services/agents/routing/a2a-mentions.ts` `analyzeA2AMentions()`（猫回复/A2A，`MAX_A2A_MENTION_TARGETS = 2`） |
| 2 目标解析 | @handle → catId，查 `mentionPatterns` + available | `routing/AgentRouter.ts`、`routing/cat-target-resolver.ts` `resolveCatTarget()` |
| 3 入队目标绑定 | 无有效 @ 时 fallback：最近 completed 回复者 → 默认猫；入队前快照 targets + 发送策略 | `AgentRouter.resolveConversationTargetsAtAdmission()`、`invocation/InvocationQueue.ts` |
| 4 分发调度 | 按目标保序、同 source 整组准入并发 fan-out、乒乓检测、深度上限、生命周期 fence | `invocation/QueueProcessor.ts`、`routing/WorklistRegistry.ts` |
| 5 上下文组装 | 历史增量 + 身份 + 队友花名册 + 调用上下文 | `routing/route-helpers.ts`、`context/ContextAssembler.ts`、`context/SystemPromptBuilder.ts` |
| 6 LLM 判断 | 猫三选一：接/退/升；误 @ 检测住在这层 | system prompt 约束，无代码路径 |

### 3.1 完整数据流（用户消息）

```
POST /api/messages (routes/messages.ts)
  → 显式 mentions + 服务端权威解析 resolveTargetsAndIntent(content, threadId)
  → invocationQueue.send(..., {kind:'conversation_input', targetCats, intent, idempotencyKey})
      消息持久化 + 队列受理是一个原子事实
  → QueueProcessor drain → AgentRouter.routeExecution()
      strategy = (intent==='ideate' && targetCats>1) ? parallel : serial
  → routeSerial()（串行链，previousResponses 带给下一棒）/ routeParallel()（并行独立思考）
  → invokeSingleCat()
猫回复完成 → parseA2AMentions()（行首 @）→ validateRoutingSyntax() 语法校验
  → commitCompletedResponseAndEnqueueA2ATargets：持久化回复 + 以 kind:'message_wake' 再入队（单事务）
  → 下一只猫被唤起（A2A 串行回合）
```

MCP 工具（`cat_cafe_post_message` / `cross_post_message` / `multi_mention`）是 HTTP 回调路由的薄代理，最终汇入同一 `enqueueA2ATargets → InvocationQueue → QueueProcessor` 管道。

### 3.2 行首契约的精确边界

- **猫→猫 A2A**：严格行首独立一行（允许前导空白与 markdown 前缀 `>` `-` `1.`），剥围栏代码块后逐行判断 `@` 开头；过滤自调用；句中 @ 不路由。权威实现 `a2a-mentions.ts analyzeA2AMentions()`；`callbacks.ts` 服务端同源。
- **用户消息例外**：`AgentRouter.parseMentionsRaw` 允许句中任意位置的 @（F32-b 最长匹配优先 + token boundary）。
- **MCP 层早期校验**：`callback-tools.ts hasPlausibleLineStartMention()` 镜像服务端解析器做 fail-fast，但注释明确"server-side analyzeA2AMentions remains the authoritative parser"。
- **影子检测**（仅可观测性，不改路由）：`a2a-shadow-detection.ts` 追踪句中 @ + 动作关键词的"差一点的交接"，数据反馈 prompt 调优。

### 3.3 Fallback 与幂等

- 普通公开消息三级优先：显式 @ → 最近 completed 回复者（processing/failed/canceled 不是候选）→ 配置默认成员。
- fallback 不改写正文、不冒充 authored mention；A2A 显式 ID 与私密收件人**不**被 fallback 改派，失败沿原失败回报闭合。
- 相同幂等 ID 重放复用首次赢得 admission 的 targets 与策略快照，不随路由/偏好稍后变化改派；不同 ID 同正文是新输入。

### 3.4 护栏

| 护栏 | 用途 | 实现 |
|---|---|---|
| 按目标 FIFO | 同目标不越序、无关目标继续 | QueueProcessor comparator + held target 约束 |
| 幂等 source×target | 恢复/重放不重复唤醒 | `lifecycle.dispatchRefs` 去重；已有 durable dispatch 只清残留 pending target |
| 乒乓检测 | 同一对猫来回踢皮球 | `WorklistRegistry`：`PINGPONG_WARN_THRESHOLD=2 / BLOCK=4`，超阈注入 `pingPongWarning`，`BLOCK` 时广播 `a2a_pingpong_terminated`；用户消息清零 streak |
| 深度上限 | 防失控链 | `getMaxA2ADepth()` env 默认 15；跨重启从消息 causal 链重建（`durable-a2a-lineage.ts`） |
| 生命周期 fence | 防过期 owner/wait/action 续跑 | 准入前验证 exact generation / owner / source |
| 稀缺席位策略 | fyi/terminal 不唤醒 | `domains/routing-context/ScarceSeatNotificationPolicy.ts` → deliveredWithoutWake |

### 3.5 知识层：第 6 层判断的燃料

机械层不猜意图，但给判断层喂三份知识（全部"是数据不是规则"，没有 `if 安全问题 then @codex` 的代码）：

- **F208 猫猫画像**（六维：原生峰值/被低估能力/坏直觉/召唤反信号/互补与反模式/翻车熔断信号）→ 通过队友花名册注入 system prompt（`SystemPromptBuilder.buildTeammateRoster()`），用**中性摘要层** `l0RosterSummary` 防路由偏见（避免"猫 X 有坏直觉"造成无关回避）。
- **F231 operator 画像**（品种层/实例层/用户层 ≤300 字符硬上限/关系层）→ `{{USER_CAPSULE}}` 注入；特别影响"升"的判断——知道 operator 偏好自主决策的猫会更少请示。
- **F221 品味导航**（7 维小品文 vignettes）→ 影响怎么做事而非传给谁，间接影响接/退（如知道"实现前必须过设计审批"的猫会先 @ 设计猫并标记人审批）。

### 3.6 pending mentions（拉式补充通道）

与推式 message_wake 唤起**不同**：消息 append 时 mentions 写入每猫 Redis Sorted Set（`RedisMessageStore.ts`，原子 Lua）；猫在会话开始主动 `get_pending_mentions`（默认 F236 anchor 模式截断正文 + requiresDrill），`ack_mentions` 经 #77 四重校验后推进 ack 游标（`DeliveryCursorStore`，Redis + 内存双层 CAS）。

---

## 四、球权（Ball Custody）：防并发写与"不丢球"

独立域 `packages/api/src/domains/ball-custody/`（约 100 文件），权威类型在 `packages/shared/src/types/ball-custody.ts`。

### 4.1 事件溯源 + 8 态状态机

- **20 种事件**（`BallEventKind`）：handoff 类（`ball.handed/handed_cvo/void_pass`）、hold 类（`ball.held/hold_expired`）、invocation 心跳、task 生命周期、wake 结算（`ball.wake_sent/wake_condition_met` + hold/dispatch/wait 三类 disposition）、安乐死三类（`ball.frozen/degraded/abandoned`）。
- 每个事件带 `sourceEventId`（幂等去重键，规范如 `route:{messageId}`、`hold:{threadId}:{catId}:{fireAt}`）与 `subjectKey`（KD-1 派生：`ball:thread:{threadId}` 或 `ball:task:{taskId}`，不引入新球 ID 原语）。
- **8 态**：new / active / blocked / parked / dead / void / zombie / resolved。转移是**纯函数表驱动**（`ball-custody-state-machine.ts`：先查动态守卫表再查静态表）。典型守卫：`ball.hold_expired` 仅 active 且 `payload.fireAt === snapshot.heldUntil` 才 → dead（防旧 reminder 误杀新 hold）；`task.done` 是唯一正常终结；dead 态迟到心跳有 10 分钟 grace 复活窗口。
- **写入侧防并发**：append-only LIST + Lua `sourceEventId` 去重 + `recordFenced(expectedSequence)` 序号围栏 + per-subjectKey promise chain 串行化（`BallCustodyIngest.ts`）。可 rebuild 读模型 `BallCustodyProjection`（state/holder/intent/heldUntil/…）。铁律：**任何 ballcustody key 不设 TTL**。

### 4.2 `cat_cafe_hold_ball` 的服务端校验链

MCP 工具端 `packages/mcp-server/src/tools/callback-tools.ts handleHoldBall` 先本地校验，再 POST `/api/callbacks/hold-ball`（`routes/callback-hold-ball-routes.ts`）：

1. **等待模式二选一**：`wakeAfterMs`（定时）或 `wakeWhen`（命令托管）；双报/全缺都拒。
2. **waitSourceRef 接地**（定时等待必带）：`kind ∈ {github_issue, github_comment, thread_message, task, reporter_handle, managed_command}` + `value` + `expectedSignal` + **`slaUntilMs`（"no SLA = no hold"）**。等人的正确路径是 @ 而非 hold_ball——曾经的误用后门 `pending_input` kind 已被移除。
3. **配额 429**：滑动窗口 `MAX_HOLDS_PER_WINDOW=3 / HOLD_WINDOW_MS=1h`，Redis Lua 原子（服务器 TIME 为权威时钟，防 TOCTOU 与节点时钟偏斜）；full gate（`pnpm gate`）豁免；失败补偿 `releaseByEventId`。
4. **Owner fence**：hold 必须挂在 exact invocation/task lease（`containing_task` 或 `action_successor`），父 invocation 不匹配 → 503。
5. **KD-23 单槽位**：同 thread+cat 再 hold 会 REPLACE 前 wake——"先插新、注册成功后再退役旧"（`retired_by_replacement` + CAS），替换只退役 wake carrier，**不撤销已授权的独立命令执行**（F261）。
6. **wakeWhen 命令托管**：`executionPolicy: 'read_only'`、SLA 1s–3h、幂等键 `managed-job-sha256(principal+key)[0:40]`；完成时带 `ManagedCommandTerminalResult { exitCode, timedOut, durationMs, tailOutput }` 结构化唤醒；崩溃由 `ManagedCommandWakeRecoverySweep` startup 幂等补投。

### 4.3 回合出口守卫（turn custody）

**回合要结束必须有出口——传球、结构化持球、或明确终止协调，不能静默丢球。**

- **旧守卫（现行生效）**：`routing/final-routing-slot.ts validateRoutingSyntax()`——final routing slot 内有 inline @handle 却无合法出口（行首 @ / hold_ball 工具调用 / MCP targetCats 结构化路由）→ `invalid_route_syntax`。
- **虚空持球检测**（KD-25 声明-动作一致性）：`routing/void-hold-detect.ts`——正文声明"持球"但本轮 tool_calls 无 `cat_cafe_hold_ball` → `ball.void_pass` 事件 + 纠正 nudge。
- **新 turn-custody 投影（shadow 模式）**：`TurnCustodyProjectionService` 按 wake 溯源分类（structured 三协议 event_wait/hold/dispatch、action_successor、unstructured、non_obligation、legacy fail-closed），turn 结束时与旧守卫 shadow 对比产 OpenTelemetry span + Prometheus 指标；**当前只产遥测，实际阻断仍由旧守卫执行**。
- Wake 携带精确 carrier（hold: taskId+sourceMessageId；event_wait: WaitContinuationCarrierV1；dispatch: sourceEventId+messageId），queue 侧逐字段匹配，不匹配 → legacy fail-closed——防旁路消息冒领球权。

---

## 五、任务与工作分配：证据不是自述

### 5.1 任务账本与拾取

- Task 绑定 owner + `initialExecutionRef`（invocation 绑定）；kind 强制 `'work'`（F160 KD-4）。
- **无主 ordinary task 可被同线程猫拾取**：`update-task` 路由 `adoptOrdinaryTaskTenant`（`domains/ball-custody/adopt-ordinary-task-tenant.ts`）原子绑定 owner。
- **跨线程换棒**：`OrdinaryTaskHandoffService`（`expectedOwnerRevision` CAS + delegate anchor + `lastAppliedHandoffMessageRef` 幂等）；`effectClass='assign_work'` 的 A2A 在 callbacks.ts 经 `prepareCallbackTaskHandoff` / F246 dispatch proposal 分流。
- MCP 侧 `create-task` 有 F193-E1 dispatch gate：跨 feature 引用而无 dispatchGate 时返回 `status:'missing'` + 建议 cross_post。

### 5.2 Entrusted work（受托工作）——最重的责任模型

`domains/growing/EntrustedWorkLifecycleService.ts`：

- **admit**：sourceRefs 必须指向当前 user 消息（`assertDirectAdmissionSourceCustody`）+ 幂等键 sha256 → `subjectKey: entrusted:<digest>`；`needs_clarification` 分类（缺 intendedOutcome / 缺 closure / 相对时间歧义 / 时间事实未引用 source），可用 `retry_custody_admission` 携新 taskDraft 重试。
- **close**：typed closure + artifact snapshot；**人类终局证据服务端专有**（`HUMAN_TERMINAL_ROUTE_REQUIRED`），猫不能替人验收；closing 后 `reconcileOwnedTaskWaits` 结算挂着的 hold/wait。
- **update**：owner-only，仅 artifactRefs/time/progress；intendedOutcome 不可被继续动作改写（`DevelopmentScopeResolver`）。

### 5.3 Collective 工作分配——不可变证据

`packages/shared/src/collective-work-assignment.ts`：assignment 是不可变证据元组（serviceInstanceId/collectiveId/connectionId/eventId/catId/participationRevision/humanId），与新鲜读取的 `CollectiveWorkProjection` **全字段比对**——注释明确"从不提升 Agent 自述文本"；执行授权要求 history 里有 `execution_authorized` 事件 + acceptance notice，且"新执行源不能复活出生源的权限"。`collective-participation-scope.ts` 用 revision 区间判断旧消息是否仍是有效授权源。

### 5.4 定时与后台调度

- **统一调度器** `infrastructure/scheduler/`：TaskRunnerV2（interval/cron/once；cron watchdog 防同槽双发；`#605` 长 delay 分块避免 32-bit overflow）→ execute-pipeline（enabled → 全局治理开关 → overlap guard → admission gate 30s → ActorResolver 选猫 → 逐 workItem 执行，self-echo 抑制）→ RunLedger。SQLite DynamicTaskStore 持久化，启动 hydrate。
- **猫的持久 schedule 变更不直接生效**：`mutationPrincipal.kind === 'cat'` 时发布 schedule mutation proposal，需 owner 在 `/api/schedule-proposals/:id/approve|reject` 批准。
- **Managed job 两段式**：`ManagedJobService.submit → admit/admitSchedule → start`（fail-closed：未 admit 不能 start）；durable gate supervisor 用进程身份 lease（60s，前任死亡证明）+ 主循环外对账（`managed-job-reconciliation.ts`）。
- **cat_life 作息**：`domains/auto-dream/cat-life-schedule.ts` 把作息推导成 cron 表达式 → `computeNextCronSlot` 出 nextWakeAt。

### 5.5 Workflow 告示牌（F073 P1）

哲学："存信息，不控制流程。猫看了自己决定行动。" `WorkflowSop`：`stage/batonHolder/nextSkill/resumeCapsule{goal,done,currentFocus}/checks/version`（乐观并发 CAS，冲突 409 带当前状态）。下一棒通过 `cat_cafe_get_workflow_sop` 读 stage + resumeCapsule 冷启动。无 backlog item 时确定性导入同线程 kind:'work' 且 relatedFeatureId 匹配的 Task 作载体。

### 5.6 Review 自审禁令——三处独立代码背书

协作纪律不停留在 prompt：

1. `domains/cats/services/collaboration/reviewer-matcher.ts canReview()`：`reviewerId === authorId → {canReview:false, reason:'Cannot review own code'}`（早期 F032 资产，现行评审治理走下两处）。
2. F177 Phase E hotfix 铁律：`cat-cafe-skills/refs/shared-rules.md` 规则 #46 + `scripts/check-hotfix-pattern.mjs` 自动加 label；merge-gate/intake admission guard："reviewer 与 author 同一 cat … fail closed"。
3. request-review 治理账本：`infrastructure/capability-evolution/adapters/request-review/request-review-use-receipt.ts` **预约阶段** `if (catId === reviewerCatId) return {ok:false, reason:'self_review'}`；`request-review-provenance.ts` 消费时再次强制 `fact.reviewerCatId !== authorCatId`——**自审事实永远不能作为 provenance 证据**。

---

## 六、Runtime 会话：一次回合的完整链路

### 6.1 六步生命周期

1. **唤醒入口**：QueueProcessor 消费队列 → `router.routeExecution()` → routeSerial/routeParallel → `invokeSingleCat()`（`invocation/invoke-single-cat.ts`，单猫调用核心）。
2. **准入/租约**：`prepareInvocationExecutionBinding` 冻结 workspace+account；`InvocationRegistry` 签发 `invocationId + callbackToken`，`verify()` 是所有回调与 member-gateway 的唯一准绳；宿主级单实例由 `services/ApiInstanceLease.ts` 保证（Redis SET NX PX + 10s 心跳续租 + 同机 pid 死亡才可 STEAL + 连续 3 次续租失败才降级）。
3. **上下文组装**（见 6.2）。
4. **执行**：构造 `callbackEnv` + `CAT_CAFE_CREDENTIAL_FILE`（≤16KB JSON 投影，env 与文件必须一致）→ provider（Claude `--mcp-config` / Codex app-server 池 / OpenCode ACP / Kimi CLI / cloud bridge）拉起猫的 runtime 进程。
5. **会话中**：猫经 MCP 调工具——stdio 走 env 凭据，HTTP 成员网关（`mcp-server/member-gateway.ts`，F306 Phase B）走 headers，fail-closed 验证 + `deployment_mismatch` 校验，身份以 verified record 为准；凭据只允许在 headers（拒 forwarded 头与 query 凭据）。`refresh-loop.ts` 按 `clamp(ttl/4, ~6.2min, 30min) × jitter` 周期保活 token，终态原因即停表。Codex/Claude 原生 transport 每回合过 `native-turn-auth.ts runWithNativeTurnAuth`（投影文件恰好一条记录 + canonical admission 204）。
6. **终态**：流式输出持久化到准入时建的 processing 气泡；compaction 事件回灌 `ContextEpochOwner.observeCompaction` → epoch+1 / cold；SessionSealer 封存 + 交接笔记落盘。

### 6.2 上下文组装（一次 prompt 里有什么）

F296 单一所有权：`ContextEpochOwner` 回答"这只猫是否还持有我们上次投影进去的工作记忆"——`scopeKey = user::cat::thread`，`contextMode: cold|hot`；**只认 provider 发出的 compaction 证据**（token 掉落等启发式不算）；cold prompt 只有被 provider 真正接受才 confirm（失败回退多一次 cold rebuild 是"安全方向"）。最终 prompt 由 `contextPromptFactory` 在 epoch handshake 后才冻结。

组装顺序（`routing/route-helpers.ts` + `session/`）：

1. `[Session Continuity — Session #N]`（仅 cold）：home 链 sealed sessions + 猫亲手交接笔记（done/next/worktree/commits/gotchas）+ extractive digest，预算 `MAX_SESSION_BOOTSTRAP_TOKENS = 2000`（`session/SessionBootstrap.ts`）。
2. `[Context Continuity] {"contextEpoch":…,"contextMode":"cold|hot","transition":…}`（`formatContextProjection`）。
3. `[对话历史增量 - 智能窗口: N 条已摘要, M 条详细]`（`assembleIncrementalContext`）：delivery cursor 拉未读 → 过滤（display-only/briefing/visibility/self_output/same-user-wave 兄弟延迟）→ `detectRecentBurst` 切"omitted 摘要窗 + burst 近期详细"→ token 预算五级降级（丢 recall pointer → coverage map → 丢 anchors → 丢 tombstone → 从最旧裁 burst → 硬空）。
4. `[导航]`（`routing/navigation-context.ts`）：当前时间/传球上下文/近 3 个活跃毛线球/成员运行态（`ThreadExecutionSituation.activeRuns` 是生命周期真相来源，禁止按最近发言推断）/真相源/下一步。
5. **身份注入**（`context/SystemPromptBuilder.ts`）：`buildSystemPrompt` = 静态身份（走 HookPipeline；ADR-021 优先级 Identity > Pack Masks > Governance L0 > Guardrails > Defaults > Workflows）+ 动态调用上下文。队友花名册（F208 中性摘要层）、用户胶囊（F231 ≤300 字符）、`directMessageFrom`（告诉被点名的猫它是被谁专门指名的）、乒乓警告、模式（独立/串行/并行）。
6. 模型来源：env `CAT_{CATID}_MODEL` > catRegistry；每猫独立 git 身份（`GIT_AUTHOR_NAME=猫名`）。

### 6.3 本地 vs 外部 runtime

- **本地**：猫 = 本机 CLI 子进程；MCP 配置以 capabilities.json 为唯一真源（F249）；managed stdio 可改写为 loopback HTTP（`materializeNativeMcpHttp`）；池化 Codex/ACP 在 session acquisition 分配 nonce credential path 防被替换进程读新凭据。
- **Cloud（ChatGPT 云猫）**：`cloud-invoke-bridge.ts dispatch()` 构造 source-bound delta（intent=原 mention 文本、calledBy=mention 猫）经 Host adapter 进绑定会话，singleflight per (threadId,catId)；返回经 `cloud-return-grant`（server-custodied exact-source 授权）精确回帖，只等有界 receipt 不等 MCP 返回。
- **外部 runtime 注册**（antigravity-desktop 等）：`ExternalRuntimeSessionRegistration` 绑定 orphan/thread 模式 + provenance，写 RuntimeSessionStore + SessionChainStore。
- **A2A dispatch observation**：`caller-dispatch-observation-model.ts` 把"我派出去的子任务状态"投影成 prompt 行（只有真正进入 prompt 的行才可 ack）；`CallerDispatchProcessStartRegistry` 投影进程启动代（process generation）——本进程 observation 只覆盖本进程登记的 dispatch，旧结果在 canonical History，不能据此推断。

---

## 七、总体评价

### 亮点

1. **机械层/判断层分界自觉**。每层文档写明"这是刻意为之"，且代码与文档高度一致——本次实查中每条文档声明都落到具体函数坐标。
2. **幂等与恢复是结构性保证而非补丁**：队列原子受理、sourceEventId 去重、CAS 围栏、序号 fence、startup sweep 补投、admission receipt 先持久化再 append 可见消息——处处"崩溃可恢复"。
3. **协作纪律有服务端代码背书**：fallback 不伪造 mention、人类终局证据服务端专有、"从不提升 Agent 自述文本"、自审禁令三处独立实现。纪律不是 prompt 里的一厢情愿。
4. **"不丢球"是结构不变量**：回合出口守卫 + void-pass 检测 + wake carrier 精确匹配 + fail-closed legacy，配合"队列非空必有 drain"的结构保证，静默丢任务在机制上被排除。

### 复杂度代价（观察，非批评）

- 巨型文件：QueueProcessor 6866 行、callbacks.ts 6136 行、callback-tools.ts 4853 行、route-serial.ts 数千行。
- 多机制处于 shadow/迁移态：turn-custody 投影 vs 旧守卫并存（shadow 对比产遥测）、新旧 review 治理并存（F032 matcher 已无活跃调用方）。
- 这是事件溯源 + 严格不变量路线的自然积累；对新读者认知成本高，但架构文档质量（`a2a-protocol.md`、`at-mention-routing-system.md` 等）足以对冲——前提是文档持续与代码同步（`at-mention-routing-system.md` 2026-10-11 校准注释是好范例）。

### 对日常协作的镜像启发

我们自己遵守的规则读代码后可见其机制根基：

- "行首独立一行 @ 才路由"——不是风格建议，是 `analyzeA2AMentions` 的硬解析规则；句中 @ 进影子检测（只记不路由），数据用于发现格式错误。
- "hold_ball 必须声明等什么（waitSourceRef + SLA）"——服务端 400 硬校验；"等人请用 @" 是校验错误文案里明确写的。
- "反问式 ping 非法 / 有立场就自决"——路由层机械转发、判断留给猫的分层设计在治理层的自然延伸。
- "球权只有第一人称、@ 或 hold_ball 是唯一凭据"——owner fence + turn-custody wake provenance 的机制投影。

---

## 附录：关键文件坐标速查

| 机制 | 路径 |
|---|---|
| @路由权威解析（A2A） | `packages/api/src/domains/cats/services/agents/routing/a2a-mentions.ts` |
| 用户消息解析 / fallback | `.../routing/AgentRouter.ts`（`parseMentionsRaw` / `resolveConversationTargetsAtAdmission`） |
| 共同 send/admission | `.../agents/invocation/InvocationQueue.ts` |
| 队列 drain / 调度引擎 | `.../agents/invocation/QueueProcessor.ts` |
| 串行/并行路由 | `.../routing/route-serial.ts` / `route-parallel.ts` |
| 单猫调用 | `.../agents/invocation/invoke-single-cat.ts` |
| 乒乓 / streak | `.../routing/WorklistRegistry.ts` |
| 跨重启 A2A 谱系 | `.../routing/durable-a2a-lineage.ts` |
| 影子检测 | `.../routing/a2a-shadow-detection.ts` |
| 回合出口守卫 | `.../routing/final-routing-slot.ts`、`void-hold-detect.ts` |
| 上下文组装 | `.../routing/route-helpers.ts`、`.../context/ContextAssembler.ts`、`SystemPromptBuilder.ts`、`.../routing/navigation-context.ts` |
| epoch / session 连续性 | `.../session/context/ContextEpochOwner.ts`、`session/SessionBootstrap.ts`、`orchestration/AutoSummarizer.ts` |
| 球权状态机 / ingest | `packages/api/src/domains/ball-custody/ball-custody-state-machine.ts`、`BallCustodyIngest.ts`、`ball-custody-events.ts` |
| hold_ball 路由 | `packages/api/src/routes/callback-hold-ball-routes.ts`、`callback-hold-ball-cancel-routes.ts`、`domains/ball-custody/hold-quota-store*.ts` |
| wakeWhen 命令托管 | `domains/ball-custody/ManagedCommandWakeRecoverySweep.ts`、`managed-command-wake-lifecycle.ts` |
| turn-custody 投影（shadow） | `domains/ball-custody/TurnCustodyProjectionService.ts`、`turn-custody-wake-provenance.ts` |
| 任务 CRUD / 拾取 | `packages/api/src/routes/callback-task-routes.ts`、`domains/ball-custody/adopt-ordinary-task-tenant.ts` |
| entrusted work | `domains/growing/EntrustedWorkLifecycleService.ts`、`routes/entrusted-work-source-custody.ts` |
| collective 分配证据 | `packages/shared/src/collective-work-assignment.ts`、`collective-participation-scope.ts` |
| managed job | `domains/managed-job/ManagedJobService.ts`、`durable-managed-gate-supervisor.ts`、`managed-job-reconciliation.ts` |
| 定时调度器 | `packages/api/src/infrastructure/scheduler/TaskRunnerV2.ts`、`execute-pipeline.ts`、`cron-utils.ts` |
| schedule 审批 | `routes/schedule-task-create-route.ts`、`schedule-mutation-proposal.ts` |
| workflow 告示牌 | `packages/shared/src/types/workflow-sop.ts`、`routes/callback-workflow-sop-routes.ts` |
| review 自审禁令 | `domains/cats/services/collaboration/reviewer-matcher.ts`、`infrastructure/capability-evolution/adapters/request-review/*`、`scripts/check-hotfix-pattern.mjs` |
| MCP 工具薄代理 | `packages/mcp-server/src/tools/callback-tools.ts`、`mcp-launch-contract.ts`、`member-gateway.ts`、`native-turn-auth.ts`、`refresh-loop.ts` |
| 实例租约 | `packages/api/src/services/ApiInstanceLease.ts` |
| pending mentions | `routes/callbacks.ts`（`/pending-mentions`、`/ack-mentions`）、`stores/redis/RedisMessageStore.ts`、`DeliveryCursorStore.ts` |
| multi_mention 编排 | `routes/callback-multi-mention-routes.ts`、`.../routing/MultiMentionOrchestrator.ts`、`multi-mention-state-machine.ts` |

## 附录：关键系统限制

| 参数 | 值 | 用途 |
|---|---|---|
| `MAX_A2A_MENTION_TARGETS` | 2 | 每条 A2A 消息最多不同目标猫 |
| `MAX_MULTI_MENTION_TARGETS` | 3 | 并行多猫提及上限 |
| A2A 最大深度 | env `MAX_A2A_DEPTH`，默认 15 | 每线程 agent 链深度 |
| 乒乓阈值 | WARN 2 / BLOCK 4 | 同对猫连续弹跳 |
| hold 配额 | 3 次 / 1h 滑动窗口 | `cat_cafe_hold_ball`（full gate 豁免） |
| wakeAfterMs | 5s – 1h | 定时唤醒范围 |
| managed job SLA | 1s – 3h | 后台命令时限 |
| Session bootstrap 预算 | 2000 token | cold 会话引导 |
