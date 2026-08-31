# 学习 Claude Code · s12 Cron Scheduler 答疑对话

> **归档时间**: 2026-08-28
> **主题**: s12_cron_scheduler 源码解读
> **参与方**: cxhui × AI 助手
> **来源项目**: `/Users/cxhui/Documents/学习文档/ai/learn-claude-code`

---

## 目录

1. [`start_runtime_threads` 完整解析](#1-start_runtime_threads-完整解析)
2. [`stop_event.wait(1.0)` 语义详解](#2-stop_eventwait10-语义详解)
3. [`pending_delivery` 的完整含义（cron 任务的 ACK 状态位）](#3-pending_delivery-的完整含义cron-任务的-ack-状态位)
4. [一次性任务 vs 周期性任务：如何区分及对应代码](#4-一次性任务-vs-周期性任务如何区分及对应代码)
5. [s12 是否修改系统 crontab（进程内 cron vs OS 级 cron）](#5-s12-是否修改系统-crontab进程内-cron-vs-os-级-cron)
6. [创建 cron 的工具：`schedule_cron`（及 list_crons / cancel_cron）](#6-创建-cron-的工具schedule_cron及-list_crons--cancel_cron)
7. [s13 README "Harness 需要回答一组相互关联的问题" 深度解读](#7-s13-readme-harness-需要回答一组相互关联的问题-深度解读)
8. [s13 `wait_for_cli_event` 双事件源阻塞器](#8-s13-wait_for_cli_event-双事件源阻塞器)
9. [s13 Task `owner` 字段的枚举取值](#9-s13-task-owner-字段的枚举取值)
10. [s13 `MessageBus` 完整解析：文件收件箱通信基础设施](#10-s13-messagebus-完整解析文件收件箱通信基础设施)
11. [s13 `wait_for_messages` 的动态超时重算逻辑](#11-s13-wait_for_messages-的动态超时重算逻辑)
12. [s13 `_read_unlocked` 设计解析：破坏性读的最底层原语](#12-s13-_read_unlocked-设计解析破坏性读的最底层原语)
13. [s16 Workflow Runtime 解决什么问题（举例讲透）](#13-s16-workflow-runtime-解决什么问题举例讲透)
14. [s15 → s16 演进 HTML 可视化图解](#14-s15--s16-演进-html-可视化图解)
15. [s17 Goal Loop 讲什么：独立判断器决定 agent 是否真的完成](#15-s17-goal-loop-讲什么独立判断器决定-agent-是否真的完成)

---

## 1. `start_runtime_threads` 完整解析

### 用户

> 解释下这个函数是做什么的？（选中 s12_cron_scheduler/code.py 第 718 行 `def start_runtime_threads():`）

### 助手

#### 一句话定位

s12 定时任务系统的**总开关**——在 agent 启动时**幂等地**拉起两个守护后台线程（cron 调度器 + 队列处理器），让 s12 从"用户问一句才响应一句"的同步 agent 升级为"你不问它也会自己动"的常驻定时任务系统。它是 s11 → s12 最关键的架构跃迁点。

#### 完整代码

```python
def start_runtime_threads():
    global runtime_started
    with runtime_lock:
        if runtime_started:
            return
        load_durable_jobs()
        RUNTIME_STOP.clear()
        runtime_threads.extend([
            threading.Thread(
                target=cron_scheduler_loop,
                name="cron-scheduler",
                daemon=True,
            ),
            threading.Thread(
                target=queue_processor_loop,
                name="cron-queue-processor",
                daemon=True,
            ),
        ])
        for thread in runtime_threads:
            thread.start()
        runtime_started = True
```

#### 逐段拆解

**第 1 段：幂等保护**

```python
global runtime_started
with runtime_lock:
    if runtime_started:
        return
```

模块级 `runtime_lock` + `runtime_started` 标志保证多次调用只启动一次。经典懒初始化 + 单例模式。防止某些异常路径重复调用导致启动 4 个线程。

**第 2 段：从磁盘恢复持久化任务**

```python
load_durable_jobs()
```

从 [.tasks/](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/.tasks) 之类目录读取上次运行时创建的持久化 cron 任务。**必须在启动 scheduler 线程之前调**——否则 scheduler 一启动可能看到空任务列表（虽然不致命但不干净）。用户昨天创建的"每天 9 点提醒"必须能在今天 agent 重启后自动恢复。

**第 3 段：清空停止信号**

```python
RUNTIME_STOP.clear()
```

`RUNTIME_STOP` 是 `threading.Event` 类型的**协作式关停信号**。两个后台线程用 `while not stop_event.wait(...)` 循环。如果之前 `stop_runtime_threads()` 调过 `.set()`，这里必须 `clear()`，否则新线程一进循环就退出。**支持 stop→start 反复循环**。

**第 4 段：创建两个守护线程**

**线程 A: `cron-scheduler`** (第 624-626 行)：

```python
def cron_scheduler_loop(stop_event: threading.Event = RUNTIME_STOP):
    while not stop_event.wait(1.0):
        poll_due_jobs(datetime.now())
```

- 每 1 秒醒一次（用 `stop_event.wait(1.0)` 而非 `time.sleep(1.0)`——前者可被 `set` 立即打断，退出响应更快）
- 只负责"发现该跑了"，把到期任务塞进 cron 队列

**线程 B: `cron-queue-processor`** (第 707-716 行)：

```python
def queue_processor_loop(stop_event: threading.Event = RUNTIME_STOP):
    while not stop_event.wait(0.2):
        if not has_cron_queue() or not agent_lock.acquire(blocking=False):
            continue
        try:
            if has_cron_queue():
                run_agent_turn_locked()
        finally:
            agent_lock.release()
```

- 每 0.2 秒醒一次（比 scheduler 频率高，让队列消费更及时）
- 关键：**`agent_lock` 用 `blocking=False` 非阻塞抢**——抢到就跑，抢不到就让位（用户正在跟 agent 对话优先），下次 tick 再试
- 与主线程通过 `agent_lock` 互斥执行 agent turn，防止两个 turn 交错污染 `session_history`

**`daemon=True` 的深意**：主进程退出时被 Python 解释器强制杀掉。如果不是 daemon，用户按 `q` 后主线程结束但后台 `while` 循环还在跑，`stop_event` 从未 set 就永远不退，主进程僵死。配合 `stop_runtime_threads` 的 `RUNTIME_STOP.set()` + `join(timeout=1)` 优雅关停，daemon 只是最终兜底。

**第 5 段：启动线程 + 标记已启动**

```python
for thread in runtime_threads:
    thread.start()
runtime_started = True
```

- `thread.start()` 只能调一次，第二次调抛 `RuntimeError`——这就是第 1 段幂等保护存在的原因
- `runtime_started = True` **放最后**：符合"数据先备齐、信号最后发布"惯例，避免中间态误判

#### 完整启动流程图

```
主入口 __main__
    ↓
start_runtime_threads()
    ↓
加锁 runtime_lock
    ↓
runtime_started?
  否 ↓
load_durable_jobs()             ← 从磁盘恢复任务
    ↓
RUNTIME_STOP.clear()            ← 重置停止信号
    ↓
创建 Thread A: cron-scheduler
创建 Thread B: cron-queue-processor
    ↓
thread.start() × 2
    ↓
runtime_started = True
释放锁
    ↓
主循环 input() 等用户输入
    ↓
   ┌─── 线程 A（每 1 秒）:
   │      poll_due_jobs(now)
   │      时间到 → 入 cron 队列
   │
   └─── 线程 B（每 0.2 秒）:
          has_cron_queue()?
          agent_lock 空闲?
          是 → run_agent_turn_locked()  ← 与主线程互斥
    ↓
用户按 q
    ↓
stop_runtime_threads()
    RUNTIME_STOP.set()
    join(timeout=1)
    runtime_started = False
    ↓
主进程退出（daemon 兜底强杀残留线程）
```

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | `with runtime_lock` + `runtime_started` 检查 | 幂等启动 |
| 2 | `load_durable_jobs()` 先于线程启动 | scheduler 启动就看到完整任务表，重启后任务不丢 |
| 3 | `RUNTIME_STOP.clear()` | 支持 stop→start 循环 |
| 4 | `daemon=True` | 主进程退出时线程被强杀避免僵尸 |
| 5 | 拆成两个线程 | 生产者/消费者分离，各司其职、频率独立 |
| 6 | `runtime_started = True` 放最后 | 状态发布晚于线程真正启动 |

#### 双线程模型的价值

一个线程既扫描又执行会有问题：一次 agent turn 可能跑几十秒，期间 scheduler 无法扫描新到期任务，错过精度要求高的定时。拆成两个线程后：

- **scheduler 保持 1 秒精度**——不管 agent 在忙什么扫描永不停
- **processor 通过 `agent_lock` 互斥**——不跟用户对话打架
- **队列作为缓冲**——即便 processor 抢不到 lock，任务也不会丢

**典型的生产者-消费者-缓冲区模式**。

#### 与 s11 的对比

| 维度 | s11 后台任务 | s12 cron scheduler |
|---|---|---|
| 触发源 | 用户 query 内 tool_use 显式发起 | **agent 自主定时触发**，无需用户输入 |
| 生命周期 | 一次 query 内启动，结果跨轮次采集 | **进程级常驻**，全程运行 |
| 状态 | 内存 dict，进程死则丢 | **磁盘持久化**（`load_durable_jobs`） |
| 后台执行体 | 每任务一个短命线程 | **两个长期线程**（scheduler + processor） |
| 结果注入 | `inject_background_results` | 从 `cron_queue` 消费，注入 `[Scheduled] xxx` |

s11 是"用户主动委托的异步任务"（外部驱动），s12 是"agent 自主的定时任务"（内部时钟驱动）。`start_runtime_threads` 就是这个内部时钟的启动器。

### 一句话总结

`start_runtime_threads` 是 s12 定时任务系统的**总开关和引导器**：**加锁保证幂等 → 从磁盘恢复持久化任务 → 清空停止信号 → 起两个守护线程（`cron-scheduler` 每秒扫描到期任务入队 + `cron-queue-processor` 每 0.2 秒消费队列并借 `agent_lock` 与主线程互斥地跑 agent turn）→ 标记已启动**。把 s12 从"用户驱动的同步 agent"升级为"内部时钟驱动的常驻定时系统"，是 s12 相对 s11 最大的架构跃迁点。

---

## 2. `stop_event.wait(1.0)` 语义详解

### 用户

> wait(1.0) 是什么意思？（选中 s12_cron_scheduler/code.py 第 625 行 `while not stop_event.wait(1.0):`）

### 助手

`wait(1.0)` 是 `threading.Event` 类的方法，**核心作用**：**"最多等待 1.0 秒，或直到 Event 被 `set()` 时立即返回"**。它不是简单的 `sleep(1.0)`——是**"可被打断的睡眠"**。

#### `Event.wait(timeout)` 返回值语义

```python
Event.wait(timeout=None) -> bool
```

| 场景 | 何时返回 | 返回值 |
|---|---|---|
| Event 已 `set()` | 立即返回 | `True` |
| 未 set，等到 timeout 到期 | 等满 timeout 秒 | `False` |
| 未 set，等待期间被别的线程 `set()` | 立刻醒来 | `True` |
| `timeout=None` | 永远等到被 set | `True` |

**关键**：返回值告诉你是"超时正常醒来"还是"被信号打断醒来"。

#### `while not stop_event.wait(1.0):` 惯用法解读

- **未收到停止信号** → wait 返回 `False` → `not False = True` → 继续循环
- **收到停止信号** → wait 返回 `True` → `not True = False` → 退出循环

**中文翻译**：**每隔 1 秒扫一次到期任务，直到有人叫停才退出**。

#### 对比 `time.sleep(1.0)`

| 特性 | `time.sleep(1.0)` | `stop_event.wait(1.0)` |
|---|---|---|
| 阻塞 1 秒 | ✅ | ✅ |
| **可被外部信号打断** | ❌ 必须睡满 | ✅ 立即醒来 |
| 返回值携带信息 | 无 | `True`/`False` 告知原因 |
| 主进程退出响应速度 | **最多 1 秒** | **几毫秒** |

用 `time.sleep` 的糟糕写法必须每次醒来手动检查 `is_set()`，且中间被 set 也要睡满整个周期。

#### 实战验证 s12 的优雅关停

看 `stop_runtime_threads`：

```python
RUNTIME_STOP.set()              # ① 拉响停止信号
for thread in runtime_threads:
    thread.join(timeout=1)      # ② 最多等 1 秒
```

发生的事：

1. `RUNTIME_STOP.set()` 让所有 `stop_event.wait(1.0)` 阻塞的线程**立刻醒来**
2. 醒来后 wait 返回 `True`，两个线程的 while 循环立即退出
3. `join(timeout=1)` 只需几毫秒就完成
4. 用户按 q → 干净退出无卡顿

如果换成 `time.sleep(1.0)`，最坏情况要等**接近 1 秒**才能退出。

#### `threading.Event` 完整方法

| 方法 | 作用 |
|---|---|
| `set()` | 标志置 True，所有正在 wait 的线程立刻醒来 |
| `clear()` | 标志置回 False |
| `is_set()` | 查询当前状态（不阻塞） |
| `wait(timeout)` | 等到标志变 True 或超时 |

**一个 Event 可同时打断任意数量的 wait 线程**——s12 两个后台线程共享同一个 `RUNTIME_STOP`，一次 `set()` 同时唤醒两个。

#### 时间轴对比

```
场景A: time.sleep 版本
──────────────────────────
主线程    ─┐              按q  set()
          ▼               │
后台线程  时刻0 ── sleep 1s ─╳ 还在睡不响应
                             ↑
                        响应延迟接近 1 秒


场景B: stop_event.wait 版本
──────────────────────────
主线程    ─┐              按q  set()
          ▼               │
后台线程  时刻0 ── wait(1s)⚡ 立即被打断
                            ↑
                       响应延迟 < 10 毫秒
```

#### 日常类比

想象等公交车：

- **`time.sleep(60)`** = 戴降噪耳机闭眼睡 60 秒（车来了也不知道）
- **`event.wait(60)`** = 戴开放式耳机眯 60 秒（车来时售票员喊一嗓子立刻醒过来）

### 一句话总结

`stop_event.wait(1.0)` = **"最多等待 1.0 秒，但如果期间被别的线程 `set()` 就立刻醒来"**。返回 `True` 表示被 set 唤醒（该退出了），`False` 表示超时正常醒来（继续干活）。比 `time.sleep(1.0)` 优越的地方是**可被外部信号立即打断**，让 s12 用户按 q 退出时能几毫秒响应而非最多等 1 秒。这是 Python 多线程"优雅关停"模式的**标准写法**。

---

## 3. `pending_delivery` 的完整含义（cron 任务的 ACK 状态位）

### 用户

> pending_delivery 是什么含义（选中 s12_cron_scheduler/code.py 第 477-489 行 `poll_due_jobs`）

### 助手

#### 一句话定义

**`pending_delivery` = CronJob 的"消息队列 ACK 状态位"**——`True` 表示"已被 scheduler 发现到期、放进 `cron_queue` 等待 agent 消费和 ACK"，`False` 表示"空闲或已成功交付"。核心作用是**防重复触发**。

#### 字段定义

```python
@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False    # ← 就是它
    last_fired: str | None = None
```

#### 三种状态

| pending_delivery | 语义 |
|---|---|
| `False` | **空闲**——未到期或已成功交付，等待下一次触发 |
| `True` | **"投递中"**——已入队 `cron_queue`，等 agent 处理并 ACK |
| False + 从 scheduled_jobs 移除（一次性任务） | **"已 ACK"**——处理成功 |

#### 完整生命周期（5 处代码 + 时间顺序）

**① 创建：pending_delivery = False**

**② `poll_due_jobs` 双重跳过防重复**（用户选中的代码）：

```python
if job.pending_delivery or job.last_fired == minute_marker:
    continue
```

两道防重复守卫：

1. `pending_delivery=True` → 已入队没被 ACK → 跳过
2. `last_fired == minute_marker` → 本分钟已触发过 → 跳过

**为什么两个都需要**：scheduler 每秒扫一次，1 分钟内会扫 60 次。没有这两个守卫，一个每分钟触发的任务会被重复入队 60 次。

**③ `_enqueue_due_job` 置 True + 持久化 + 入队**

```python
def _enqueue_due_job(job: CronJob, minute_marker: str | None = None):
    old_pending = job.pending_delivery
    old_last_fired = job.last_fired
    job.pending_delivery = True                # ← 标记
    if minute_marker is not None:
        job.last_fired = minute_marker
    try:
        if job.durable:
            save_durable_jobs()                # ← 持久化
    except Exception:
        job.pending_delivery = old_pending     # ← 回滚
        job.last_fired = old_last_fired
        raise
    cron_queue.append(job)
```

**事务性**：先标记 → 持久化 → 入队。任何一步失败都完整回滚。

**④ `consume_cron_queue` 只出队不改 pending**

```python
def consume_cron_queue() -> list[CronJob]:
    with cron_lock:
        jobs = list(cron_queue)
        cron_queue.clear()          # ← 只清空队列
    return jobs
```

**为什么不改 pending**：LLM 调用可能失败，如果消费时就清 pending，失败后任务丢了。必须等 LLM 真的成功再 ACK。

**⑤ 成功路径：`acknowledge_cron_jobs`**

```python
if current.recurring:
    current.pending_delivery = False       # ← 周期任务清标记
else:
    scheduled_jobs.pop(current.id)         # ← 一次性任务直接删除
```

带完整 try/except 回滚：磁盘写失败时**内存状态完全恢复**（删的加回来、pending 复原、队列补回来）。

**⑥ 失败路径：`restore_cron_jobs`**

```python
def restore_cron_jobs(jobs: list[CronJob]):
    with cron_lock:
        for delivered in jobs:
            current = scheduled_jobs.get(delivered.id)
            if current is None:
                continue
            current.pending_delivery = True    # ← 保持 pending
```

**保留 pending=True** 防止 poll_due_jobs 重新入队（任务已经在 cron_queue 里等下轮 agent 消费）。

#### 状态转移图

```
       ┌───────────────┐
       │   任务创建     │
       │ pending=False │
       └───────┬───────┘
               │
        scheduler 每秒扫
               │
       poll_due_jobs 检查:
       - pending? 是 → 跳过
       - 本分钟已 fired? 是 → 跳过
       - cron_matches? 否 → 跳过
               │ 全部通过
               ▼
       _enqueue_due_job:
       ┌───────────────┐
       │pending = True │
       │last_fired = X │
       │durable ? save │
       │queue.append  │
       └───────┬───────┘
               │
        queue_processor 拉起 agent turn
               │
       consume_cron_queue() 只出队,pending 仍为 True
               │
       LLM.messages.create()
               │
       ┌───────┴───────┐
     成功            失败
       │              │
    ack:          restore:
    recurring?     pending 仍 True
      是: pending=False   (等下次 agent turn)
      否: 从 dict 删
    durable ? save
```

#### 类比 MQ 的 ACK 机制

`pending_delivery` 和 RabbitMQ/SQS 的 **unacknowledged message** 完全同构：

| MQ 概念 | s12 对应 |
|---|---|
| Message published | `cron_queue.append(job)` |
| Message delivered | `consume_cron_queue()` |
| **Unacknowledged** | `pending_delivery=True` |
| Consumer ACK | `acknowledge_cron_jobs()` |
| Consumer NACK | `restore_cron_jobs()` |
| Redelivery on failure | 保留 `pending_delivery=True` |

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | 三态刻画 | 精确表示生产/消费/ACK 三阶段 |
| 2 | poll 双重跳过（pending + last_fired） | 防秒级扫描导致的分钟内重复 |
| 3 | 先置 pending 再持久化再入队 + 回滚 | 事务性:磁盘/内存/队列三方一致 |
| 4 | consume 不清 pending | 等 LLM 成功才 ACK,防止丢任务 |
| 5 | ack 区分 recurring/一次性 | 周期保留、一次性删除 |
| 6 | ack 完整回滚 | 磁盘失败时内存复原 |
| 7 | restore 保持 pending=True | 失败重试时防重新入队 |

### 一句话总结

`pending_delivery` 是 CronJob 的**"消息队列 ACK 状态位"**：`True`=已入队等 ACK，`False`=空闲。核心作用是**防重复触发**——scheduler 每秒扫一次，没有这个标记一分钟内会被重复入队 60 次。整个 `_enqueue → consume → ack/restore` 是一个**微型 MQ ACK 协议**（类似 RabbitMQ），配合 `durable` + `save_durable_jobs` 提供了 s12 的"至少一次"语义（at-least-once delivery）——任务宁可被重复投递也不能丢失。

---

## 4. 一次性任务 vs 周期性任务：如何区分及对应代码

### 用户

> 怎么区分一次性任务和周期性任务，对应哪里的代码（选中 README.zh.md 第 105 行"一次性任务会被删除，周期任务则清除 `pending_delivery`，等待下一次匹配"）

### 助手

#### 核心区分字段：`CronJob.recurring: bool`（第 254 行）

```python
@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool          # ← 唯一区分标记
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None
```

| `recurring=True` | `recurring=False` |
|---|---|
| 周期任务（"每分钟提醒") | 一次性任务（"10 分钟后提醒") |
| ACK 后保留在 `scheduled_jobs` | ACK 后从 `scheduled_jobs` 移除 |

#### 6 处代码路径（从上游到下游）

**① Tool schema（第 578-586 行）**：LLM 显式传入 `recurring`

```python
{"name": "schedule_cron", ...
 "input_schema": {
     "properties": {
         "cron": {"type": "string"},
         "prompt": {"type": "string"},
         "recurring": {"type": "boolean"},    # ← 非必填
         "durable": {"type": "boolean"}},
     "required": ["cron", "prompt"]}}         # ← recurring 不在 required
```

**② `run_schedule_cron` 工具入口（第 546 行）**：默认 `recurring=True`

```python
def run_schedule_cron(cron: str, prompt: str, recurring: bool = True,
                      durable: bool = True) -> str:
```

保守策略：不确定就当周期性。

**③ `schedule_job` 存字段（第 414-436 行）**

```python
job = CronJob(
    id=new_cron_id(),
    cron=cron, prompt=prompt,
    recurring=recurring,          # ← 存入永久属性
    durable=durable,
)
scheduled_jobs[job.id] = job
```

**④ 触发路径完全统一（`poll_due_jobs` / `_enqueue_due_job`）**

Scheduler **不区分两类任务**，只干"到时间就入队"这一件事。分类决策推迟到 ACK 阶段。

**⑤ ★ ACK 分岔点 —— 用户选中原文对应的核心代码（第 505-510 行）**

```python
def acknowledge_cron_jobs(jobs: list[CronJob]):
    ...
    for delivered in jobs:
        current = scheduled_jobs.get(delivered.id)
        if current is None:
            continue
        changed.append((current, current.pending_delivery))
        if current.recurring:                           # ★ 分岔点
            current.pending_delivery = False            # 周期:清 pending
        else:
            removed.append(current)
            scheduled_jobs.pop(current.id)              # 一次性:从 dict 删
```

**两条分支的语义**：

| `current.recurring` | 操作 | 结果 |
|---|---|---|
| `True`（周期） | `pending_delivery = False`（保留在 dict） | 下次 cron 匹配再次触发 |
| `False`（一次性） | `scheduled_jobs.pop(current.id)` | 从此消失，不再触发 |

一次性任务的"删除"是**从 `scheduled_jobs` 整个移除对象**——不只是清 pending，而是让 `poll_due_jobs` 下次遍历时**根本看不到它**。

**⑥ 展示层区分（`run_list_crons` 第 561-563 行）**

```python
for job in jobs:
    frequency = "recurring" if job.recurring else "one-shot"
    storage = "durable" if job.durable else "session"
    lines.append(f"{job.id}: {job.cron} -> {job.prompt[:60]} [{frequency}, {storage}]")
```

用户能看到：

```
cron_0001: 0 9 * * * -> 查看邮件 [recurring, durable]
cron_0002: 20 14 * * * -> 该吃药了 [one-shot, durable]
```

#### 完整流程对照图

```
LLM 调用 schedule_cron(cron, prompt, recurring=?, durable=?)
                        ↓
              run_schedule_cron (默认 recurring=True)
                        ↓
              schedule_job → CronJob(recurring=X, ...)
                        ↓
              scheduled_jobs[id] = job + 可选持久化
                        ↓
   scheduler 每秒扫描  ── 两类任务处理完全相同
              poll_due_jobs → _enqueue_due_job
                        ↓
              cron_queue.append(job), pending=True
                        ↓
   queue_processor 拉起 agent_loop → LLM 处理
                        ↓
              ┌─────────┴─────────┐
            成功              失败
              ↓                   ↓
    acknowledge_cron_jobs    restore_cron_jobs
              ↓
     ┌────────┴────────┐
    True             False
     ↓                ↓
  pending=False   pop(id)
  保留在 dict     从 dict 删除
   ↓                ↓
  等下次触发    永久消失
```

#### 双维度分类（recurring × durable）

`recurring` 只是一个维度，s12 还有另一个正交维度 `durable`。两两组合共 4 种任务类型：

| recurring | durable | 类型 | 典型场景 |
|---|---|---|---|
| ✅ | ✅ | **持久周期** | 每天 9 点提醒，重启后自动恢复 |
| ✅ | ❌ | **临时周期** | 本次会话内每分钟检查，退出即失 |
| ❌ | ✅ | **持久一次** | 明天 10 点提醒交报告，跨重启生效 |
| ❌ | ❌ | **临时一次** | 30 秒后 ping 一下，会话内有效 |

两字段完全解耦——`recurring` 决定"跑多少次"，`durable` 决定"存不存磁盘"。

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | `recurring` 是 CronJob 的显式布尔字段 | 一个字段决定语义 |
| 2 | tool schema 里 recurring 非必填 | 减少 LLM 心智负担 |
| 3 | Python 默认 recurring=True | 保守策略：不确定就当周期 |
| 4 | scheduler 触发路径完全统一 | 分类决策推迟到 ACK，scheduler 保持简单 |
| 5 | 唯一分岔在 acknowledge_cron_jobs | **单一决策点**，逻辑集中易理解 |
| 6 | 一次性任务 `pop` 从 dict 彻底移除 | 简单粗暴：不存在的 job 不会被扫到 |
| 7 | 与 durable 正交 | 双维度组合出 4 种任务类型 |

### 一句话总结

**区分一次性和周期性任务全靠 `CronJob.recurring: bool` 一个字段**。用户通过 tool schema 的 `recurring` 参数显式声明（默认 True），`schedule_job` 存入对象，scheduler 触发路径两类任务完全一样——**唯一分岔点在 `acknowledge_cron_jobs` 第 507 行 `if current.recurring:`：周期任务只把 `pending_delivery` 清成 False 等下次匹配，一次性任务直接从 `scheduled_jobs.pop(current.id)` 从字典删除**。**一个字段、一个 if 分支就实现了两类语义**。

---

## 5. s12 是否修改系统 crontab（进程内 cron vs OS 级 cron）

### 用户

> 这里的代码有修改系统 crontab 的逻辑吗

### 助手

**没有**。s12 整套 cron scheduler **完全在 Python 进程内自实现**，**不修改系统 crontab**（`/etc/crontab`、`crontab -e` 用户 crontab、`launchd`、`systemd timer` 一个都不动）。

#### 证据 —— 全仓 grep 结果

搜索 `crontab|launchctl|systemctl|/etc/cron|/var/spool/cron|schtasks` 等所有可能操作系统调度器的关键字：

- 6 个匹配全部出现在**文档 / README / SVG 图**里
- **代码文件 `code.py` 中 0 个匹配**

文档甚至专门用红字警告：

> ⚠ **进程关闭 = 调度停止（不是 OS 级 crontab）** —— [cron-scheduler-overview.svg](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s12_cron_scheduler/images/cron-scheduler-overview.svg) 第 100 行

[README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s12_cron_scheduler/README.zh.md) 第 126 行明确说：

> "需要在 Agent 关闭时仍按时执行任务，应使用系统的 crontab、systemd timer 或其他外部调度服务。"

这句话反过来正说明 s12 自己不会碰系统 crontab。

#### s12 到底"cron"了什么？

它只**借用了 cron 表达式的语法**（5 字段 `分 时 日 月 周`），完全在 Python 进程内自己解析和触发：

**① 自实现 cron 表达式解析**（第 263-288 行）：

```python
def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*": return True
    if field.startswith("*/"): return value % int(field[2:]) == 0
    ...

def cron_matches(cron_expr: str, moment: datetime) -> bool:
    fields = cron_expr.strip().split()
    ...
```

**② 自实现调度线程**（第 624-626 行）：

```python
def cron_scheduler_loop(stop_event: threading.Event = RUNTIME_STOP):
    while not stop_event.wait(1.0):
        poll_due_jobs(datetime.now())
```

**③ 自实现持久化** —— 存到项目本地 `.tasks/` 目录的 JSON 文件，**不写 `/var/spool/cron/`**。

#### 差异对照表

| 维度 | s12 内置 cron | 真正的 OS 级 crontab |
|---|---|---|
| 表达式语法 | 5 字段（借用 cron 语法） | 5 字段 |
| 触发引擎 | Python threading.Thread + while 循环 | `crond` / `launchd` / `systemd` |
| **持久化位置** | `.tasks/` 项目本地 JSON | `/var/spool/cron/crontabs/<user>` |
| **执行主体** | 当前 Python agent 进程 | 系统级独立进程 |
| **进程退出后** | ❌ 停止 | ✅ 继续跑 |
| **需要 root/sudo** | ❌ 不需要 | 依赖系统 cron 服务 |
| **副作用** | 只影响当前 agent 进程 | **修改系统调度器**，跨进程生效 |

#### 关键差异

**s12 是"应用层模拟的 cron"**：

- ✅ **好处**：无侵入、无权限要求、跨平台、任务和 agent 绑定
- ❌ **代价**：Python 进程一退出，所有任务停止触发（`durable=True` 只是把任务定义存盘，下次启动才恢复；期间到期时刻会被错过）

### 一句话总结

s12 **没有修改系统 crontab 的逻辑**。它是**"应用层模拟的 cron"**——只借用 cron 表达式语法，实际调度由 Python 后台线程 `cron_scheduler_loop` 在**同一个进程内**完成，任务存储在项目本地 `.tasks/`，**不碰 `crontab -e`、不碰 `/var/spool/cron/`、不碰 `launchctl`、不碰 `systemctl`**。想要 agent 关闭后任务仍按时触发，README 明确建议用系统 crontab 或 systemd timer 从外部调度 agent——这句建议本身就是 s12 不做系统 crontab 集成的明证。

---

## 6. 创建 cron 的工具：`schedule_cron`（及 list_crons / cancel_cron）

### 用户

> 有创建 cron 的工具么？叫什么名字？

### 助手

**有的 —— 叫 `schedule_cron`**。s12 给 LLM 暴露了 **3 个 cron 相关工具**。

#### 3 个工具速览（[code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s12_cron_scheduler/code.py) 第 573-591 行）

| 工具名 | 作用 | 必填参数 | 可选参数 |
|---|---|---|---|
| **`schedule_cron`** | 创建 cron 任务 | `cron`、`prompt` | `recurring`（默认 True）、`durable`（默认 True） |
| `list_crons` | 列出所有已注册任务 | 无 | 无 |
| `cancel_cron` | 按 ID 取消任务 | `job_id` | 无 |

#### `schedule_cron` 的完整定义

```python
{"name": "schedule_cron",
 "description": "Schedule a prompt with a 5-field cron expression.",
 "input_schema": {"type": "object",
                  "properties": {
                      "cron":      {"type": "string"},
                      "prompt":    {"type": "string"},
                      "recurring": {"type": "boolean"},
                      "durable":   {"type": "boolean"}},
                  "required": ["cron", "prompt"]}}
```

#### 参数解读

| 参数 | 类型 | 含义 | 默认 |
|---|---|---|---|
| `cron` | string | 5 字段 cron 表达式：`分 时 日 月 周` | 必填 |
| `prompt` | string | 到时间后投递给 agent 的用户消息（以 `[Scheduled] xxx` 形式追加到 messages） | 必填 |
| `recurring` | boolean | true=周期任务，false=一次性 | True |
| `durable` | boolean | true=持久化到磁盘，false=仅内存 | True |

#### 使用示例

**周期任务**：

```
用户:每分钟提醒我看下时间
→ schedule_cron(cron="* * * * *", prompt="现在几点了?", recurring=true, durable=true)
```

**一次性任务**：

```
用户:明天 9 点 30 分提醒我开晨会
→ schedule_cron(cron="30 9 * * *", prompt="开晨会时间到", recurring=false, durable=true)
```

**仅本次会话有效**：

```
用户:每 5 分钟检查一次进程,但不要持久化
→ schedule_cron(cron="*/5 * * * *", prompt="检查进程", recurring=true, durable=false)
```

#### 调用链路（第 592-596 行）

```python
TOOL_HANDLERS.update({
    "schedule_cron": run_schedule_cron,
    "list_crons":    run_list_crons,
    "cancel_cron":   run_cancel_cron,
})
```

```
LLM: schedule_cron(...)
        ↓
run_schedule_cron  (默认 recurring=True, durable=True)
        ↓
schedule_job
        ├─ validate_cron() 校验表达式
        ├─ 创建 CronJob 对象
        ├─ scheduled_jobs[job.id] = job
        └─ 若 durable, save_durable_jobs() 落盘
        ↓
返回 "Scheduled cron_0001: * * * * * -> ..." 给 LLM
```

#### 另外两个工具

**`list_crons`**（第 553-566 行）—— 查看所有任务：

```
返回:
cron_0001: * * * * * -> 现在几点了? [recurring, durable]
cron_0002: 30 9 * * * -> 开晨会时间到 [one-shot, durable]
```

**`cancel_cron`**（第 568-569 行）—— 按 ID 取消：

```
LLM: cancel_cron(job_id="cron_0001")
返回: "Cancelled cron_0001"
```

### 一句话总结

**创建 cron 的工具叫 `schedule_cron`**，参数是 `cron`（5 字段表达式）+ `prompt`（到点投递给 agent 的提示词）+ 可选的 `recurring`（周期/一次性）+ `durable`（持久化/仅内存）。s12 一共暴露三个 cron 工具：**`schedule_cron` 创建 / `list_crons` 查询 / `cancel_cron` 取消**，构成完整的 CRUD 中的 C、R、D（Update 需要先 cancel 再 schedule）。

---

## 7. s13 README "Harness 需要回答一组相互关联的问题" 深度解读

### 用户

> 解释下这句话本身。Harness 被赋予了什么样的词体（主语？）Harness 是什么，为什么在这里做主语。这句话要表达什么意思？
>
> Harness 是一个名词：agent 中，除了 llm 以外，所有的都是 harness。这个名词在这里做主语表示什么含义？
>
> （选中 s13_agent_teams/README.zh.md 第 24 行 "Harness 需要回答一组相互关联的问题："）

### 助手

**这是一句拟人化修辞的技术句子**。

#### 上下文定位

句子出现在 s13 README 第 24 行，紧跟在用户抛出模糊需求之后：

> "重构这个示例后端。清理配置加载、认证和测试..."

作者要引出"要把这句话变成可执行的多 Agent 工作，需要解决哪些工程问题"。

#### 问题 1：Harness 是什么？为什么做主语？

用户的理解**完全正确**：**Harness = agent 中除了 LLM 之外的所有代码**——Agent Loop、工具注册、权限系统、Hook、上下文压缩、记忆管理、后台任务、定时任务、s13 的多 Agent 团队运行时。

看第 8 行关键定义句，作者刻意点破整章定位：

> **Harness 层**：Team（团队）— 多个 Agent 如何分工、共享状态，同时接受 Lead 控制。

作者在**明确划分职责**：这一章讲的不是"LLM 能不能想到要分工"（LLM 的能力问题），而是"当 LLM 想分工时，harness 该怎么支持它"。**主语选择透露了作者的立场**：这些问题不该由 LLM 回答，而应该由 harness 通过基础设施来回答。

#### 问题 2："回答问题" 是什么意思？

这是**拟人化修辞**。字面上 harness 是 Python 代码，代码不会"回答问题"。真实的意思是：

> **"设计和实现 harness 的工程师，必须在写代码之前想清楚这些设计问题；harness 一旦写出来，其代码结构本身就是这些问题的答案。"**

直白版：**"要实现 s13 的多 Agent 团队功能，harness 必须在设计上解决以下 6 个问题"**。

看紧接着列出的 6 个问题，每个都对应 harness 的一个具体模块：

| 问题 | 需要 harness 提供什么机制 |
|---|---|
| 1. 谁判断并行是否有用？新 Agent 由谁确认？ | **计划闸门**、Lead-用户审批协议 |
| 2. 每个队友如何跨任务保留身份和上下文？ | **持久 Agent**（不 spawn 新的） |
| 3. 结果如何自动返回 Lead？ | **MessageBus**（收件箱+运行时投递） |
| 4. 空闲队友能否直接接手 ready task？ | **共享任务板**（不用 Lead 逐项派发） |
| 5. 并行修改冲突时用哪个工作目录？ | **可选 worktree**（任务绑定目录） |
| 6. 关机和计划审批如何成为可追踪协议？ | **类型化协议** |

对照第 33-42 行"解决方案"部分的架构组件，**每个问题都对应一个 harness 模块**。所以这句话真正的含义是：

> **"s13 的 harness 设计必须提供以下机制来解决以下问题。"**

#### 问题 3：作者选 Harness 做主语的深层意图

三层用意：

**（1）文体上：把"设计题"包装成"harness 的问题"** —— 避免掉进"LLM 应该更聪明就好"的陷阱。让 LLM 自主完成分工、协调、恢复上下文——理论上可能，但**极不稳定、极耗 token、极难调试**。作者的态度是**把这些问题工程化、协议化，让 harness 承担确定性，让 LLM 只负责语义决策**。

**（2）架构上：明确"分层"的责任边界** —— 这是整个 learn-claude-code 系列贯穿的核心哲学：

```
┌─────────────────────────────────┐
│  LLM 层  ← 负责"该做什么"(语义) │
├─────────────────────────────────┤
│  Harness 层 ← 负责"怎么做"(工程) │
├─────────────────────────────────┤
│  OS 层   ← 负责"跑得动"(系统)   │
└─────────────────────────────────┘
```

用户抛给 LLM 一句模糊的"重构后端"，LLM 顶多能规划任务图。但**任务如何在多个 Agent 间分发、身份如何维持、消息如何路由、冲突如何避免——这些不能靠 LLM，只能靠 harness**。

**（3）教学上：问题驱动的表达方式** —— 先抛 6 个问题，再给方案。第 24 行就是"提问导览"，读到第 33 行"解决方案"清单时能一一对号入座。

#### 类比

这句话与以下表达是同一种修辞手法：

| 拟人化说法 | 直白说法 |
|---|---|
| **"Harness 需要回答一组相互关联的问题"** | **"我们设计 harness 时必须解决这些问题"** |
| "架构必须承担这些责任" | "架构师必须在架构里实现这些责任" |
| "数据库需要保证 ACID" | "数据库的实现代码必须保证 ACID 特性" |
| "编译器要处理循环引用" | "编译器工程师必须写代码处理循环引用" |

**工程写作中常见的拟人化**——把设计问题归到系统本身，让读者聚焦"系统需要什么能力"，而不是"谁去实现"。

### 一句话总结

**Harness 在这里 = "agent 中除了 LLM 之外的所有工程代码"**（用户理解完全正确）。作为主语用了**拟人化修辞**，字面意思是"harness 需要回答这些问题"，真实含义是"**要实现 s13 的多 Agent 团队功能，我们必须在 harness 层设计出机制来解决这 6 个问题**"。作者刻意选 harness 做主语而不是"LLM"或"我们"，是要**划清责任边界**：这些问题不能靠 LLM 的聪明来解决，只能由 harness 通过工程化的基础设施（MessageBus、共享任务板、worktree、协议闸门等）来兜底。这是 learn-claude-code 系列贯穿的核心哲学——**LLM 管语义决策，Harness 管确定性执行，各司其职**。

---

## 8. s13 `wait_for_cli_event` 双事件源阻塞器

### 用户

> `wait_for_cli_event` 这个是做什么的？（选中 s13_agent_teams/code.py 第 1817-1833 行）

### 助手

#### 一句话定位

s13 主线程的**"双事件源阻塞器"**——替代普通 `input()`，让主线程能**同时等待两件事的任意一件先发生**：

1. 用户敲键盘输入一行
2. 后台某个 teammate 往 Lead 的收件箱扔了消息

哪个先到就返回哪个。这是 s13 从"用户驱动的对话 agent"升级为"多 Agent 团队 lead"的**关键 IO 抽象**。

#### 完整代码

```python
def wait_for_cli_event() -> tuple[str, str | None]:
    prompt_visible = False
    while True:
        if BUS.peek("lead"):                           # ① 收件箱有货?
            if prompt_visible:
                print()
            return "wake", None                        #    → wake 事件
        if not prompt_visible:
            print("s13 >> ", end="", flush=True)       # ② 打印提示符
            prompt_visible = True
        readable, _, _ = select.select([sys.stdin], [], [], 0.25)   # ③ 等 stdin 或 250ms
        if readable:
            line = sys.stdin.readline()                # ④ 读一行
            if line == "":
                return "quit", None                    #    → EOF
            return "user", line.rstrip("\n")           #    → 用户输入
```

#### 返回值语义（三种事件）

| kind | payload | 触发条件 | 主循环处理 |
|---|---|---|---|
| `"user"` | 输入字符串 | 用户敲了一行按了回车 | 追加到 history，跑 agent_loop |
| `"wake"` | None | Lead 收件箱有消息 | 消费收件箱，注入到下一轮 |
| `"quit"` | None | stdin 收到 EOF（Ctrl+D） | 退出主循环 |

#### 逐段拆解

**① 事件源优先级：先检查收件箱**

```python
if BUS.peek("lead"):
    return "wake", None
```

`BUS.peek("lead")` 是**非阻塞观察**（第 884 行）：只看收件箱文件是否存在且非空，不读、不删、不移动指针。

**优先级设计**：wake 高于 user——teammate 完成任务或需审批的消息，通常是主循环之前的 agent turn 触发的，有"我发起的东西回来了"的语义连续性。

**② 打印提示符只打一次**

`prompt_visible` 状态位保证 `s13 >> ` 只在整个 while 循环里打印一次（select 每 250ms 醒一次，不加保护会刷屏）。`flush=True` 是必须的，否则 `end=""` 时提示符不会立即显示。

**③ 核心：`select` 非阻塞轮询 stdin（250ms 超时）**

```python
readable, _, _ = select.select([sys.stdin], [], [], 0.25)
```

`select.select(rlist, wlist, xlist, timeout)` 语义：

- 监听 rlist 里的 fd 是否有数据可读
- 最多等 timeout 秒
- 三种返回：stdin 有数据 / 250ms 到期 / Ctrl+C 打断（外层兜底）

**为什么用 select 不用 input()**：`input()` 永久阻塞——主线程永远无法主动检查收件箱。teammate 完成任务后，用户不敲键盘就永远不会被处理。这是**两个事件源都可能先到**场景的经典难题。

**为什么是 250ms**：响应性与 CPU 占用的平衡点。太短 CPU 空转，太长 wake 延迟明显。250ms 用户几乎感知不到延迟，CPU 一秒醒 4 次几乎无负担。

**④ 读取或退出**

```python
if readable:
    line = sys.stdin.readline()
    if line == "":
        return "quit", None      # EOF (Ctrl+D)
    return "user", line.rstrip("\n")
```

`line == ""` 是 Unix EOF 约定——正常输入至少含 `\n`，空串表示流关闭。用 `rstrip("\n")` 而不是 `strip()` 精准去掉行尾换行，保留用户可能有意义的两端空格。

#### 主循环里的完整使用（第 1842-1867 行）

```python
while True:
    kind, payload = wait_for_cli_event()
    if kind == "quit":
        break
    if kind == "user":
        if payload is None or payload.strip().lower() in {"q", "exit", ""}:
            break
        trigger_hooks("UserPromptSubmit", payload)
        history.append({"role": "user", "content": payload})
    else:                                      # wake
        inbox = consume_lead_inbox()
        if not inbox:
            continue
        history.append({
            "role": "user",
            "content": format_team_events(inbox),
        })
        print(f"[wake: {len(inbox)} team event(s) -> new turn]")

    agent_loop(history)
    ...
```

**关键设计**：两种事件源殊途同归——键盘 or 收件箱最终都被塑造成一条 user message 追加 history，统一走 agent_loop。Lead LLM **不需要知道触发它的是键盘还是收件箱**，只需要看 history 最后一条消息做决策。

#### 与 s11/s12 主循环等待机制对比

| 章节 | 等待函数 | 事件源 | 阻塞方式 |
|---|---|---|---|
| s11 | `input()` + `inject_background_results` | 用户键盘 + 后台结果（被动等下次输入才注入） | `input()` 完全阻塞 |
| s12 | `input()` + 后台线程 `run_agent_turn_locked` | 用户键盘 + cron 定时器（后台线程抢占 `agent_lock`） | `input()` 阻塞，cron 靠抢锁 |
| **s13** | **`wait_for_cli_event`** | **用户键盘 + 收件箱（`select` 多路复用）** | **`select` 250ms 轮询** |

**s13 独特之处**：唯一把两个事件源在**同一个循环**里 select 起来——不依赖后台线程"抢占"，也不让后台结果"傻等到下次输入"。**更接近真正的 event loop**。

#### 为什么用文件轮询而不是 select fd

`select` 在**普通文件**上的行为跨 OS 不一致——Linux 上普通文件总被认为"随时可读"，`select` 立即返回起不到阻塞作用。真要监听文件变化需 `inotify` (Linux) / `kqueue` (macOS) / `ReadDirectoryChangesW` (Windows)——引入平台判断。

**用 peek + 250ms 轮询是最简单可靠的跨平台方案**，代价只是最坏 250ms 延迟，对 LLM agent 秒级响应场景完全够用。

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | 优先检查收件箱 | wake 优先级高于 user |
| 2 | `BUS.peek()` 非阻塞观察 | 只查不消费 |
| 3 | `prompt_visible` 状态位 | 提示符只打印一次 |
| 4 | `select` + 250ms 超时 | 响应性与 CPU 占用平衡 |
| 5 | 只监听 stdin，收件箱轮询 | 跨平台可移植 |
| 6 | 返回 `(kind, payload)` 元组 | 主循环统一分支处理 |
| 7 | `line == ""` 判断 EOF | Unix 惯例识别 Ctrl+D |

### 一句话总结

`wait_for_cli_event` 是 **s13 主循环的"双事件源阻塞器"**：**每 250ms 用 `select` 醒一次，优先检查 Lead 收件箱——有消息返回 `("wake", None)`；否则检查 stdin 可读——有输入返回 `("user", line)`，EOF 返回 `("quit", None)`**。让 Lead 主线程能**同时监听键盘和后台 teammate 消息**，把 s13 从 s11/s12 的"用户驱动型 agent"升级为"多事件源驱动型 event loop"，是 s13 从单 agent 走向多 agent 团队协作的**关键 IO 抽象**——没有它，Lead 就只能等用户敲一下才能感知一次团队状态，多 Agent 协作的实时性无从谈起。

---

## 9. s13 Task `owner` 字段的枚举取值

### 用户

> owner 有哪些枚举（选中 s13_agent_teams/code.py 第 463 行 `task = load_task(str(assignment["task_id"]))`）

### 助手

#### 类型声明（Task 类第 113-119 行）

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None      # ← 类型:字符串或 None
    blockedBy: list[str]
```

**`owner` 是 `str | None`——不是严格 Python enum，只是约定俗成的字符串标签**。

#### 4 种取值分类

| 值 | 语义 | 何时出现 | 代码位置 |
|---|---|---|---|
| `None` | **无人认领**（初始 pending 态） | `create_task` 创建时默认 | 第 144 行，第 480 行释放时也置回 |
| `"lead"` | **Lead agent 自己认领** | Lead 直接跑任务不派发 | 通过 `TeammateAgent(self.name="lead")` 调用 |
| `"agent"` | **通用兜底名字** | `claim_task/complete_task` 默认参数值 | 第 281/311/814/823 行 |
| `"<teammate 名字>"` | **具体队友认领** | Lead spawn teammate 后队友用自己 name 认领 | 第 1150/1219/1394 行 |

#### 关键设计观察

**开放式字符串标签，不是封闭枚举**：

- 没有 `class OwnerType(Enum): ...`
- 没有白名单校验
- teammate 名字由 Lead spawn 时自由指定（第 1375 行 `spawn_teammate_thread(name: str, ...)`）

**好处**：扩展性强，加新 teammate 就起个名字。**代价**：类型系统提供不了保护，靠代码约定。

#### `"agent"` vs `"lead"` 的微妙区别

- **`"agent"`** 出现在**独立 CLI 工具（run_claim_task/run_complete_task）**默认值里，代表**"通用兜底身份"**
- **`"lead"`** 是**Team 系统的正式角色名**，Lead 也作为特殊 teammate 参与消息路由（`sender="lead"`, `target="lead"`）

`"lead"` 广泛用在 MessageBus 的 sender/target 字段（第 1015/1060/1066/1088/1092/1105/1450 行），是**协议里的一等公民**；`"agent"` 只出现在 task 相关函数的默认参数里。

#### 判断逻辑速览

代码里对 owner 只做**同一性判断**：

```python
task.owner is None       # 未认领 → 可 claim
task.owner is not None   # 已被认领 → 不能重复 claim
task.owner == owner      # 是"我"认领的 → 校验完成权限
task.owner != owner      # 不是"我"的 → 拒绝完成
```

**对具体名字（"lead"/"agent"/"teammate_xxx"）没有做过内容判断**——只关心"是不是同一个 owner 字符串"。印证它是"标签"而非"枚举"的定位。

### 一句话总结

`owner` 是 `str | None` 类型的**字符串标签**（不是严格枚举），4 类取值：**`None`（未认领）/ `"lead"`（Lead 认领）/ `"agent"`（独立 CLI 默认值）/ `"<teammate 名字>"`（多 Agent 模式下队友的 name）**。代码里对它只做同一性判断（`is None` / `== owner`）不做内容判断，是"开放式标签"设计而非封闭枚举——想加新角色直接起名字，代价是类型系统无法校验合法性，全靠代码约定。

---

## 10. s13 `MessageBus` 完整解析：文件收件箱通信基础设施

### 用户

> 解释下 MessageBus 的用法以及用例（选中 s13_agent_teams/code.py 第 843-844 行 `class MessageBus: """Thread-safe file mailboxes with destructive reads."""`）

### 助手

#### 一句话定位

**s13 的核心通信枢纽**——基于文件的、线程安全的消息队列，让 Lead 和多个 teammate agent 在不同线程里可靠地交换消息。文档字符串把三大特征全说透了：**Thread-safe 多线程安全 / file mailboxes 文件存储 / destructive reads 读一次消费掉**。

#### 4 个 API 概览

| 方法 | 用途 | 阻塞? |
|---|---|---|
| **`send(from, to, content, ...)`** | 生产者投递消息 | 不阻塞 |
| **`read_inbox(agent)`** | 消费者取走全部消息（**读完删除**） | 不阻塞 |
| **`peek(agent)`** | 观察者非破坏性查看（有货吗？） | 不阻塞 |
| **`wait_for_messages(agent, timeout)`** | 消费者阻塞等待新消息 | **阻塞** |

#### 底层数据模型

每个 agent 一个 `.jsonl` 文件：

```
mailboxes/
├── lead.jsonl              ← Lead 的收件箱
├── backend_engineer.jsonl  ← teammate 的收件箱
└── ...
```

每行一条 JSON：

```json
{"from": "backend_engineer", "to": "lead", "content": "任务已完成",
 "type": "result", "ts": 1735300000.123, "metadata": {}}
```

**为什么用 jsonl 而不是 JSON 数组**：`send` 只需 O(1) 追加（`.open("a")`），不用先读再写整个数组。一行 write 是文件系统级原子操作。

#### 4 个方法的实现细节

**① `send`：生产者投递**

```python
def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
    msg = {"from": from_agent, "to": to_agent, "content": content, ...}
    with self._changed:
        MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
        with self._path(to_agent).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
        self._changed.notify_all()             # ★ 唤醒所有 wait_for_messages
```

关键：`open("a")` 追加模式 O(1)，`notify_all` 唤醒等待者。

**② `read_inbox`：破坏性读取**

```python
def _read_unlocked(self, agent: str) -> list[dict]:
    inbox = self._path(agent)
    if not inbox.exists():
        return []
    msgs = [json.loads(line) for line in inbox.read_text().splitlines()
            if line.strip()]
    inbox.unlink()                             # ★ 读完立即删除文件
    return msgs
```

**"destructive reads" 含义**：读完立刻 unlink——同一批消息只被同一个消费者读到一次，保证 exactly-once delivery，无需维护"已读指针"。

**③ `peek`：非破坏性观察**

```python
def peek(self, agent: str) -> bool:
    inbox = self._path(agent)
    return inbox.exists() and inbox.stat().st_size > 0
```

只查不动，主线程能非阻塞判断"有没有新消息"。

**④ `wait_for_messages`：阻塞式等待（最精妙的部分）**

```python
def wait_for_messages(self, agent, timeout=None) -> list[dict]:
    deadline = None if timeout is None else time.monotonic() + timeout
    with self._changed:
        while not self.peek(agent):              # ← 防假唤醒
            remaining = ... deadline - time.monotonic() ...
            if remaining is not None and remaining <= 0:
                return []                        # 超时返回空
            self._changed.wait(remaining)        # ★ 释放锁+睡觉,等 notify
        return self._read_unlocked(agent)
```

**`threading.Condition` 机制**：`self._changed = Condition(self._lock)`。`wait(remaining)` 原子地做三件事：释放锁 → 阻塞睡觉 → 被 notify_all 或超时唤醒后重新拿锁。

比"time.sleep(0.5) 循环 peek"精准太多：有消息 → **毫秒级**唤醒；没消息 → **完全不占 CPU**；`while not peek()` 防止假唤醒。

#### 3 个安全设计点

**① 路径穿越防护**

```python
def _path(self, agent: str) -> Path:
    if not is_valid_agent_name(agent):
        raise ValueError(f"Invalid mailbox recipient: {agent!r}")
    path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
    if not path.is_relative_to(MAILBOX_ROOT):
        raise ValueError(f"Mailbox path escapes directory: {agent!r}")
    return path
```

双重防线：名字白名单 + 路径 resolve 后检查——防 LLM prompt injection 产生的恶意 agent 名。

**② `ensure_ascii=True`** 跨编码可移植（CJK 转义为 `\uXXXX`）

**③ `RLock` 可重入锁** 允许同一线程嵌套调用

#### s13 的三大消息流用例

从 17 处 `BUS.*` 调用归纳出三条消息流水：

**用例 1：Lead → Teammate 派发指令（4 处）**

```python
BUS.send("lead", to, content)                          # 普通消息
BUS.send("lead", teammate, "...shutdown.", ...)        # 关机命令
BUS.send("lead", teammate, task, "plan_request")       # 请求计划
BUS.send("lead", state.sender, content, "plan_approval_response", ...)
```

**用例 2：Teammate → Lead 汇报（6 处）**

```python
BUS.send(from_name, "lead", plan, "plan_approval_request", ...)
BUS.send(self.name, "lead", "Shutdown acknowledged.", ...)
BUS.send(self.name, "lead", summary, "result")
BUS.send(self.name, "lead", "Waiting for more work.", ...)
# 及各种进度上报
```

**用例 3：主线程等待事件（3 处）**

```python
# wait_for_cli_event: 非阻塞查询
if BUS.peek("lead"):
    return "wake", None

# 主循环: 判断是否所有 teammate 退出
elif had_teammates and not BUS.peek("lead"):
    print("[all teammates shut down]")

# teammate 线程等待新任务
inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
```

#### 通信拓扑图（星型）

```
                  ┌────────────┐
                  │ Lead 主线程 │
                  └──────┬─────┘
                         │
          ┌──────────┬───┼───┬──────────┐
          ▼          ▼       ▼          ▼
      teammate    ...    ...        teammate
       线程 A                         线程 N
          │          │       │          │
          └──────────┴───┬───┴──────────┘
                         │ 所有消息经 BUS 转发
                         ▼
             ┌────────────────────────┐
             │ MessageBus (全局单例)   │
             │ mailboxes/*.jsonl       │
             └────────────────────────┘
```

**Lead 和 teammate 从不直接通信**——所有通信都通过 BUS 中转（**星型拓扑**）。好处：Lead 不维护 teammate 引用；teammate 挂了 Lead 不受影响（消息堆在收件箱）；新增 teammate 无需改 Lead 代码。

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | 每 agent 一个 jsonl 文件 | O(1) 追加,天然分区 |
| 2 | Destructive reads (读完删) | Exactly-once,无需读指针 |
| 3 | `Condition + notify_all` | 毫秒级唤醒,0 CPU 空转 |
| 4 | `while not peek()` 循环 | 防假唤醒 |
| 5 | 名字白名单 + is_relative_to 双检查 | 路径穿越深度防御 |
| 6 | `ensure_ascii=True` | 跨编码可移植 |
| 7 | `RLock` | 允许同线程嵌套 |
| 8 | 全局单例 `BUS` | Lead + 所有 teammate 共享 |

### 一句话总结

`MessageBus` 是 s13 的**通信基础设施核心**——基于文件的、线程安全的、破坏性读取的多 agent 消息队列。`send` 生产、`read_inbox` 消费（读完自动删除保证 exactly-once）、`peek` 非阻塞观察、`wait_for_messages` 用 `Condition + notify_all` 实现毫秒级唤醒。所有 Lead ↔ teammate 通信都走这个总线（**星型拓扑**），配合每个 agent 独立的 `.jsonl` 文件收件箱，实现了**跨线程可靠、无耦合、无 CPU 空转**的通信协议，是 s13 从单 agent 走向多 Agent 团队协作的**神经系统**。

---

## 11. s13 `wait_for_messages` 的动态超时重算逻辑

### 用户

> 这一段是什么意思？（选中 s13_agent_teams/code.py 第 894-898 行 `wait_for_messages` 的 while 循环判断和 remaining 计算）

### 助手

这 5 行是 `wait_for_messages` 的**"计算还需要等多久"逻辑**——解决多线程编程经典难题：**总超时时间固定，但可能被多次唤醒，每次唤醒后剩余可等待时间要动态重算**。

#### 完整代码（含上下文）

```python
def wait_for_messages(self, agent, timeout=None) -> list[dict]:
    deadline = None if timeout is None else time.monotonic() + timeout   # ★ 起点
    with self._changed:
        while not self.peek(agent):                                       # ← 选中开始
            remaining = (None if deadline is None
                         else deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                return []                                                 # ← 选中结束
            self._changed.wait(remaining)
        return self._read_unlocked(agent)
```

#### 逐行拆解

**第 1 行：`while not self.peek(agent):`**

只要收件箱空就继续循环。用 `while` 而不是 `if` 是**防假唤醒**——`Condition.wait()` 在某些 OS 上偶尔无故被唤醒（spurious wakeup），或被 notify 后消息被别的消费者读走。醒来必须**重新检查条件**。

**第 2-3 行：`remaining = ...` 动态重算**

**为什么每次都要重算，而不是直接 `wait(timeout)`**？

关键场景：假设 `timeout=10, deadline=100`（当前 t=90）：

```
├─ 第 1 次:remaining=10, wait(10)
├─ t=93 假唤醒(无消息) → peek False → 继续
├─ 第 2 次:remaining=7, wait(7)         ← 只等剩余的 7 秒
├─ t=95 又被假唤醒
└─ 第 3 次:remaining=5, wait(5)
```

如果每次都 `wait(10)`：被打断 100 次就永远醒不过来——每次都从"还剩 10 秒"开始等，**总等待成了无限循环**。

**正确做法**：**用绝对时间 `deadline` 作为锚点**，每次用当前时间和 deadline 的差动态算剩余时长——**保证"我说等 10 秒就是最多等 10 秒"的用户契约**。

**第 4-5 行：`if remaining <= 0: return []` 超时兜底**

如果算出剩余时间用光了，直接返回空列表。避免走多余循环迭代（醒来 → peek → 算 → 判断），提前 return 更干净。

`remaining is not None` 判断：兼容 `timeout=None`（永久等待）场景——这时不应触发超时跳出，让代码走到 `wait(None)` 无限等待。

#### 为什么用 `time.monotonic()` 而不是 `time.time()`

```python
deadline = time.monotonic() + timeout
```

**`time.monotonic()` 是单调递增时钟**：
- 永远只增不减（即便系统时间被 NTP 调整或用户改时钟）
- 单位是秒（float）
- 原点无意义（不代表 Unix 时间，只算差值用）

**用 `time.time()` 的坑**：墙钟时间可能被 NTP 倒调——回拨 5 秒就多出 5 秒超时。`time.monotonic()` 是 Python 官方推荐的**计时标准做法**。

#### 具体时序案例

`timeout=10, deadline=100`：

| 时刻 | 事件 | remaining | 行为 |
|---|---|---|---|
| t=90 | 首次进入 | 10 | wait(10) |
| t=93 | 假唤醒（无消息） | 7 | wait(7) |
| t=95 | 别的消费者读走消息 | 5 | wait(5) |
| t=98 | 终于有消息 | 2 | peek True → 退出 → 读消息返回 |

**总用时 8 秒**，符合 timeout=10 约束。

若无 remaining 动态重算 → 每次 wait 都传 10 → 假唤醒不断重置 → 可能等 30 秒 → 违反用户约定。

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | `deadline` 用绝对时间锚点 | 无论被唤醒多少次，总超时不变 |
| 2 | `time.monotonic()` 而非 `time.time()` | 抗系统时钟回拨 |
| 3 | `while not peek()` 循环判断 | 防假唤醒 + 防条件竞争 |
| 4 | 每次循环重算 remaining | 让 wait 只等"真正剩余"的时间 |
| 5 | `remaining <= 0` 提前 return | 避免多余循环迭代 |
| 6 | `remaining is not None` 兼容永久等待 | 支持 timeout=None 场景 |

### 一句话总结

这 5 行代码实现了**"动态计算剩余可等时间"**的关键逻辑：**用绝对时间 deadline 作为超时锚点，每次循环用 `deadline - time.monotonic()` 计算剩余可等秒数，用光了 `return []`**。解决了"多次唤醒后总超时不能被重置"的经典多线程难题，配合 `time.monotonic()` 抗时钟回拨、`while not peek()` 防假唤醒，实现了**精确、鲁棒、可预测的阻塞等待语义**——这是 Python 标准库 `threading.Condition` 阻塞等待的**教科书级写法**。

---

## 12. s13 `_read_unlocked` 设计解析：破坏性读的最底层原语

### 用户

> 解释下 `_read_unlocked` 的设计（选中 s13_agent_teams/code.py 第 858-865 行）

### 助手

#### 完整代码

```python
def _read_unlocked(self, agent: str) -> list[dict]:
    inbox = self._path(agent)
    if not inbox.exists():
        return []
    msgs = [json.loads(line) for line in inbox.read_text().splitlines()
            if line.strip()]
    inbox.unlink()
    return msgs
```

#### 一句话定位

**"以文件为单位、破坏性地一次性读走某个 agent 收件箱内所有消息"**。方法名前缀 `_` 表示私有，后缀 `_unlocked` 明确告诉调用者**"这个方法内部不加锁，必须外部持锁调用"**。

#### 3 个关键设计维度

**维度 1：命名约定 —— `_unlocked` 后缀的深意**

Python 并发编程"锁契约命名"惯例：

- 带 `_unlocked` 后缀 = "我不加锁，你确保调用时已持锁"
- 不带 = "我自加锁"

对比 MessageBus 的两个上层方法：

```python
def read_inbox(self, agent: str) -> list[dict]:      # 公开 API,自加锁
    with self._lock:
        return self._read_unlocked(agent)             # ← 复用逻辑

def wait_for_messages(self, agent, timeout=None):
    with self._changed:                                # 外部已持锁
        while not self.peek(agent):
            ...
        return self._read_unlocked(agent)             # ← 无需再加锁
```

**为什么拆出 `_unlocked` 版**：如果读逻辑本身加锁，`wait_for_messages` 里会形成"锁的嵌套"——虽然 `RLock` 可重入不死锁，但增加不必要开销。**拆出无锁版让不同上下文按需组合**——Java `Vector`、C++ STL、Linux kernel 都用过的经典模式。

**维度 2：破坏性读（destructive read）—— 语义核心**

```python
msgs = [json.loads(line) for line in inbox.read_text().splitlines()
        if line.strip()]
inbox.unlink()                    # ★ 读完立即删除整个文件
```

`unlink()` 是整段代码**最重要的一行**。

传统方案 vs s13 方案：

| 传统方案 | s13 方案 |
|---|---|
| 维护"已读指针" | 读完直接删文件 |
| 需额外元数据存储 | 无元数据 |
| 需处理多消费者指针推进 | 天然独占（读到=拥有全部） |
| 需防重复消费 | 物理删除天然防重复 |

**破坏性读的三个直接好处**：

1. **Exactly-once 语义**：同一批消息只被读到一次
2. **无状态**：不需维护"已读到哪里"的持久化状态，重启行为一致
3. **自然清理**：消息读走后文件消失，磁盘不膨胀

**代价**：读取失败无法回滚（`unlink` 后代码崩就丢消息）。s13 接受了这个代价——它是"尽力而为"的通信系统，LLM agent 挂了整个 turn 都要重来，丢消息不是最大问题。

**维度 3：存在性检查 + 空行过滤 —— 边界条件的严谨**

```python
if not inbox.exists():
    return []
```

**为什么先检查存在**：大多数时候文件不存在（agent 还没收到消息或上次读走后没新消息）。直接 `read_text()` 会抛 `FileNotFoundError`。"没消息"不算异常，用条件判断而非 try/except **语义更清晰**。

```python
msgs = [json.loads(line) for line in inbox.read_text().splitlines()
        if line.strip()]
```

**为什么 `if line.strip()` 过滤**：

- `read_text().splitlines()` 遇末尾空行返回空串
- 文件被手动编辑过可能有空行
- 空字符串传给 `json.loads` 会崩

`line.strip()` 是**防御性容错**，防止意外空行导致 JSON 解析崩。

**为什么用 `read_text()` 一次性读**：收件箱通常很小（一次 send 加一行），内存无压力，逻辑简单。

**列表推导式**：比 for 循环更简洁更快，是 Python 惯用法。

#### 与另外两个读方法的对比

| 方法 | 加锁 | 修改状态 | 返回值 | 使用场景 |
|---|---|---|---|---|
| `peek(agent)` | 自加 | ❌ 只观察 | `bool` | 主线程非阻塞判断"有没有消息" |
| `read_inbox(agent)` | 自加 | ✅ 读完删除 | `list[dict]` | 消费者主动拉取 |
| **`_read_unlocked(agent)`** | ❌ 外部持锁 | ✅ 读完删除 | `list[dict]` | 内部私有原语，被上面两者和 `wait_for_messages` 复用 |

**分层设计**：`_read_unlocked` 是**最底层原语**（只做核心动作），`read_inbox` 和 `wait_for_messages` 是**上层组合**（加锁 + 等待 + 复用原语）。**"底层无锁 + 上层加锁"**的经典分层模式。

#### 潜在改进点

- **`read_text(encoding="utf-8")` 显式指定编码**：`send` 用了 utf-8，读时也显式指定更严谨（避免 Windows 默认 CP1252）
- **用 try/except 替代 exists()**：极端并发下 `exists()` 后到 `read_text()` 之间可能被别的进程删除；但 s13 场景基本无影响

#### 关键设计要点

| # | 设计 | 目的 |
|---|---|---|
| 1 | 方法名 `_read_unlocked` | 命名契约：`_` 私有 + `_unlocked` 不加锁 |
| 2 | `inbox.exists()` 前置检查 | "无消息"不算异常，用条件判断 |
| 3 | `read_text().splitlines()` 一次读 | 简单直接，收件箱小无压力 |
| 4 | `if line.strip()` 空行过滤 | 防御性容错 |
| 5 | 列表推导式 | Pythonic，简洁快速 |
| 6 | `inbox.unlink()` 删文件 | 破坏性读核心，exactly-once + 无状态 |
| 7 | 无锁版本 + 上层组合 | 允许 `wait_for_messages` 复用 |

### 一句话总结

`_read_unlocked` 是 `MessageBus` **"破坏性读取"的最底层原语**——**7 行代码实现"检查文件存在 → 一次性读入 → 空行过滤 → JSON 解析 → 删除文件 → 返回列表"**。方法名 `_unlocked` 后缀是**并发编程的锁契约命名惯例**（"我不加锁，请外部持锁调用"），让上层 `read_inbox`（自加锁）和 `wait_for_messages`（已持锁）都能**复用同一段核心逻辑**。核心创新在 `inbox.unlink()` ——**读完即删的破坏性读**，用文件系统的原子删除实现了 exactly-once delivery，避免了传统消息队列需要维护"已读指针"的复杂性。这是**基础设施代码"简洁即强大"的典范**——把复杂问题（并发通信）用最少的代码和最简单的机制（文件系统）解决。

---

## 13. s16 Workflow Runtime 解决什么问题（举例讲透）

### 用户

> 举例解释 workflow runtime 解决什么问题？（选中 s16_workflow_runtime/README.zh.md 全文）

### 助手

#### 一句话回答

**s16 解决"多 agent 编排的重复劳动、稳定性和中断恢复"三大痛点**——把由多次 agent 调用组成的**固定业务流程**，从"依赖对话历史+模型每次凭直觉安排步骤"升级为"**代码写死编排 + 磁盘 journal 记录进度 + 结构化输出保证接口**"的可靠工作流。

#### 用一个具体例子把问题讲透

**任务**：agent 做代码审查，标准流程是：

1. 从 4 个维度审查代码（安全 / 性能 / 可读性 / 逻辑）
2. 每个维度找可疑点 findings
3. 每个 finding 独立做对抗性验证（排除误报）
4. 汇总真实问题

#### 方案 A：纯 s01~s15 主循环做法（模型驱动）

```
turn 1: 模型 → 看安全 → 得发现 A
turn 2: 模型 → 看性能 → 得发现 B
...
turn 4: 模型 → 验证 A
turn 5: 模型 → 验证 B
...
turn 20+: 汇总
```

**4 个痛点**（README 直接列出的 3 个 + 1 个）：

| 痛点 | 具体表现 |
|---|---|
| **① 串行等待** | 4 个维度一个一个查，明明可以并行 |
| **② 输出结构漂浮** | 模型每次输出格式都不一样，下游代码接不住 |
| **③ 无法续跑** | 跑到 15 轮时中断，前 14 轮全作废 |
| **④ 步骤靠对话历史撑** | 步骤顺序在 messages[] 里，上下文一压缩就丢 |

#### 方案 B：s16 workflow 做法

把固定流程写成代码保存起来：

```python
async def sample_workflow(ctx, args):
    ctx.phase("Review")
    changes = args.get("changes", "")

    async def audit(_v, dimension, _i):
        return await ctx.agent(
            f"检查{dimension}:{changes}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}"
        )

    async def verify(audited, dimension, _i):
        ctx.phase("Verify")
        verdicts = await ctx.parallel([                     # 每条 finding 并行验证
            (lambda f=f: ctx.agent(f"验证:{f}", schema=VERDICT_SCHEMA))
            for f in audited["findings"]])
        return {"confirmed": [...]}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)  # 4 维度并行走 audit→verify
    return {"confirmed": [...]}
```

模型只需**一次 tool_use**：`{"name": "Workflow", "input": {"name": "review-changes", "args": {...}}}`

#### 3 个痛点如何被解决

**① 并行 vs 串行 —— `pipeline` 和 `parallel` 原语**

```
方案 A (串行,假设每次 10 秒 × 20 轮 = 200 秒):
安全 → 性能 → 可读 → 逻辑 → 验证 A → 验证 B → ...

方案 B (pipeline 4 路并行,约 30-40 秒):
安全 → 验证发现
性能 → 验证发现       ← 4 维度同时推进
可读 → 验证发现
逻辑 → 验证发现
```

README 关键定义：**"item A 跑到第 3 阶段时，item B 可能还在第 1 阶段"**——不同 item 不等齐，各自推进。

**② 输出结构稳定 —— schema 校验 + 一次重试**

```python
run = await asyncio.to_thread(self.runner.run, prompt, schema, label)
result = run.value
if schema is not None:
    ok, err = SimpleJsonSchema(schema).validate(result)
    if not ok:
        retry = await asyncio.to_thread(
            self.runner.run, prompt + "\n\n返回合法的 JSON。", schema, label)
        result = retry.value
```

**强制子 agent 输出 JSON，不符合自动重试一次**。下游代码 `for f in audited["findings"]:` 直接遍历，不用解析自然语言。

**③ 可恢复 —— journal + resume_from_run_id（最核心创新）**

每次 `agent()` 完成后立即追加到磁盘 journal：

```
.runtime/run_20260828_001.journal.jsonl
──────────────────────────────────────
{"key": "agent-1234567890", "value": {"findings": [...]}}   ← 安全审查
{"key": "agent-2345678901", "value": {"findings": [...]}}   ← 性能审查
                                        ← Ctrl+C 中断!
```

续跑时用**内容哈希**当 key（不是计数器）：

```python
def key(self, kind, label, prompt, schema):
    basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
    return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

cached = self.journal.cached(key)
if cached is not MISS:
    return cached          # ← 命中!直接返回,不重新调 LLM
```

**关键**：key 不能用计数器（parallel/pipeline 里完成顺序不定，两次跑会错位）。用**调用内容哈希**（prompt+schema+label），跨运行稳定。

**具体演示**：

```
初次运行 → 4 轮 audit 完成后 Ctrl+C
runId = run_20260828_001

python s16_workflow_runtime/code.py resume
────────────────────────────
[Review] audit:安全 → cached (0 秒 0 token)   ← 命中
[Review] audit:性能 → cached (0 秒 0 token)   ← 命中
[Verify] verify:xxx → 重新跑                    ← 从这继续
```

README 结尾原话：**"续跑命中全部缓存时显示 agents=0 tokens=0"**——无损续跑省时省钱。

#### 完整对比表

| 维度 | s01~s15 主循环 | s16 Workflow |
|---|---|---|
| 谁决定下一步 | 模型每轮决定 | 脚本代码预先写死 |
| 步骤记录 | messages[]（易丢） | journal.jsonl（持久） |
| 并行能力 | 无 | parallel + pipeline |
| 输出结构 | 模型自由 | schema 强制校验 + 重试 |
| 中断恢复 | 从头 | runId + journal 缓存 |
| tool_use 次数 | 20+ 轮 | **1 次** |
| 中间结果 | 塞 messages 污染上下文 | 存变量,不进历史 |

#### 什么时候用 s16？什么时候不用？

**用 s16**：
- ✅ 重复性任务（代码审查、批量翻译、数据分析）
- ✅ 流程固定（步骤和依赖执行前已知）
- ✅ 可并行
- ✅ token 消耗大，重跑代价高

**不用 s16**：
- ❌ 探索性任务（调 bug，每步依赖发现）
- ❌ 单次任务（一次调用够了）
- ❌ 流程需要 LLM 动态决策（决策点太多写不进代码）

#### Harness 分工哲学的深化

回到 s13 讨论过的核心哲学：

```
LLM 层 ← 语义决策 (该做什么)
Harness 层 ← 工程确定性 (怎么做)
```

**s16 深化**：LLM 只需决定"用哪个 workflow + 参数"，Harness 负责一切执行（并发调度、schema 校验、断点续跑、进度上报、任务生命周期）。

README 结尾点破：**"s06 的子 agent 是模型临场派一次；s16 把编排写成可续跑的宿主代码。"**

### 一句话总结

**s16 Workflow Runtime 解决的问题** = 让"重复的固定流程"从"每次都靠 LLM 现场重新规划"变成"一次写死+可靠执行"。用 `pipeline/parallel` 解决**并行等待**痛点，用 `schema` 校验解决**输出漂浮**痛点，用 `journal + resume_from_run_id + 稳定 key` 解决**中断丢失**痛点。核心思想：**"模型决定单步（哪个 workflow + 什么参数），脚本决定编排（步骤 + 并发 + 恢复策略）"**——把 LLM 的语义能力和工程代码的确定性执行力**分而治之**。适合代码审查、批量翻译这种"步骤已知+需要并行+跑得贵不能白跑"的重复任务。

---

## 14. s15 → s16 演进 HTML 可视化图解

### 用户

> 给出一个 html，生动解释从 s15 到 s16 的转变过程。突出关键特性。方便理解

### 助手

创建了 [s15-to-s16-evolution.html](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/.chatlogs/s15-to-s16-evolution.html)，一个信息饱满的暗色科技风可视化页面。

#### HTML 结构（8 大板块）

1. **顶部渐变标题 + 章节导航链**（s01 → … → s15 → **s16** → s17）
2. **一句话对比卡片**（左 s15 蓝 vs 右 s16 橙，中间脉动箭头动画）
3. **三大痛点故事**：代码审查场景 + 20 轮串行 timeline + turn 15 崩溃震动动画 + 红色 lost banner
4. **s16 三大解法可视化**：
   - pipeline 4×4 网格（audit → verify 并行推进）
   - schema 校验前后对比代码
   - resume 动画：初次运行中断 → resume 时 audit 块变金色 💾 显示 "0 秒"
5. **6 大关键机制卡片**（悬停变色）：Workflow 工具 / 编排原语 / schema 校验 / journal / 稳定 key / 事件流
6. **完整对照表**（9 个维度对比）
7. **分层哲学**（三层堆叠图 LLM/编排/Harness，s16 加了中间那层）
8. **使用建议**（左绿✅ 右红❌ 双栏对照）

#### 关键视觉设计

- **配色语义化**：s15 蓝色 `#58a6ff`，s16 橙色 `#f78166`，高亮金 `#ffd33d`
- **动效**：中间箭头 `pulse` 呼吸、崩溃轮次 `shake` 震动、卡片悬停浮起
- **信息饱满**：所有 3 大痛点、6 大机制、9 项对比、2 类使用场景一次说清
- **代码块**：真实 Python 代码带语法高亮（关键字/字符串/注释着色）

#### 核心突出的关键特性

- **s16 不替换 s15**——只是工具池里多出一个 `Workflow` 工具
- **一次 tool_use 跑完整套编排**（相比 s15 的 20+ 轮）
- **`pipeline` 不等齐**："item A 跑到 stage 3 时，item B 可能还在 stage 1"
- **稳定内容哈希 key**：不是计数器，两次跑能精准对上
- **agents=0 tokens=0**：命中缓存完全零成本
- **分层哲学的深化**：s15 承担基础执行确定性 → s16 承担编排级确定性

打开方式：直接双击 `.chatlogs/s15-to-s16-evolution.html` 或用浏览器打开。

---

## 15. s17 Goal Loop 讲什么：独立判断器决定 agent 是否真的完成

### 用户

> file:///Users/cxhui/Documents/学习文档/ai/learn-claude-code/s17_goal_loop 解释 s17 在讲什么

### 助手

#### 一句话概括

**s17 解决"agent 什么时候真正算做完"这个哲学问题**——把 s01 起就默认的"模型不调工具 = 任务完成"升级为"独立判断器读对话记录，判断目标条件是否真的达成"。README 副标题精准点破：

> "模型不再调用工具，只代表这一轮想停；目标是否完成，再交给一个独立判断器。"

#### 从 s01 到 s16 的"停止条件"演进

s17 之前所有课时的 agent loop 退出条件都是：

```python
while True:
    response = llm.call(messages)
    tool_calls = extract_tool_uses(response)
    if not tool_calls:
        return                          # ← 模型不调工具就退出
    run_tools(tool_calls)
```

**问题**：这行代码把"模型这一轮没想调工具"和"任务已完成"划了等号。

**漏洞场景**：

| 场景 | 表面上 | 实际上 |
|---|---|---|
| 修 bug 到测试全绿 | 模型说"修好了" | 从没跑过测试或没绿 |
| 完成 checklist 所有验收项 | 停在中间某项 | 后面还有 3 项没做 |
| "单测通过且 lint 无错" | 跑完单测就停 | lint 完全没运行 |

**s01~s16 的停止判断权全在主模型自己手里**。s17 说这不合适，要加一个**独立于主模型**的裁判。

#### 核心机制：/goal 命令 + Stop hook

**用户交互**：

```
/goal pytest tests/auth 退出码为 0，并且 lint 没有错误
```

**主循环关键改动（就几行）**：

```python
if tool_results:
    messages.append({"role": "user", "content": tool_results})
    continue

# ★ 原来的 return 位置,现在插入 Goal 判断
decision = await self.goal.evaluate_after_turn(self.messages)
if decision.action == "block":
    self.messages.append({"role": "user", "content": decision.reason})
    continue                                     # ← 裁判说没完,继续下一轮
return SessionResult(text=text, status=decision.action)
```

#### 两个模型分工

```
主模型 (干活)                        Goal 裁判 (独立判断)
- 修代码、跑测试、调工具              - 独立 LLM 调用
- 决定"这一轮要不要调工具"            - 只读:看对话历史
                                    - 无工具:不能读文件不能跑测试
       │                            - 输出 JSON: {ok, reason, impossible}
       │ 主模型说停 → 触发 Goal Stop hook
       ▼
```

**关键设计**：**裁判没有工具**。只能根据对话里已经写过的内容做判断：

- 主模型跑了 pytest → 结果写进对话 → 裁判看到 → 通过
- 主模型说"我修好了"但**从没跑过测试** → 对话里没证据 → 裁判拒绝

裁判返回：

```json
{
  "ok": false,
  "reason": "对话中还没有出现 pytest 的退出码",
  "impossible": false
}
```

- `ok=true` → 完成退出
- `ok=false` → 未完成，reason 塞回 messages，continue
- `impossible=true` → 不可能达成，认输退出

#### 想解决的 4 个具体痛点

**痛点 1：模型"假装完成"**——裁判要求**证据在对话里**（测试结果、exit code、编译输出）。主模型 system prompt 明确要求"把命令和结果写进对话让裁判检查"。

**痛点 2：多轮任务需要自我驱动**——裁判说 block → 自动 continue → 主模型不需要用户催就下一轮。agent 从"一问一答"变成"自主奔向目标"。

**痛点 3：消息即状态**——裁判和主模型**共享同一份 messages[]**，裁判理由直接追加进对话。README 明确反对引入独立 CommandQueue：**"把它画进 Gate 会把'谁做决定'和'决定从哪条路送回来'混成一件事"**。

**痛点 4：自动机制必须有出口**——Goal 本身没默认轮数限制，但保留两道**通用兜底**：

1. 主循环全局 max_turns（`MAX_TURNS=20`）
2. Stop hook 连续 block 次数上限

达到上限时**把控制权还给用户，但不伪装成完成，也不主动清除目标**。判断器失败时同样原则：**停止自动续轮，保留目标，不能在无法判断时宣称成功**——安全性核心原则。

#### 4 个核心部件

| 部件 | 作用 |
|---|---|
| `GoalState` | 保存条件、判断次数、开始时间、最近 reason |
| `PromptGoalEvaluator` | 独立 LLM 调用读对话返回 JSON 判断 |
| `GoalController` | 设置/查看/清除 Goal，实现 Stop hook |
| `AgentSession` | 在原来的退出位置接入 Goal 判断 |

#### 三个 /goal 子命令

```
/goal <条件>       # 设置或替换目标
/goal              # 查看状态(次数/时间/token/最近原因)
/goal clear        # 清除(别名 stop/off/reset/none/cancel)
```

#### 好的完成条件 3 要素

1. **结束状态**：最终要达到什么
2. **验证方式**：用什么命令证明
3. **限制条件**：不能破坏什么

**坏例子**：`把代码弄好`（太模糊）

**好例子**：

```
/goal 完成登录模块迁移，直到 pytest tests/auth 退出码为 0，
      并且没有修改 tests/auth 之外的测试文件
```

#### 和后台任务/Workflow 的协作

**痛点**：后台任务/workflow 异步跑。主模型停时后台没完，立即判断没意义。

**解法**：Stop hook 返回 **`defer`**，保留目标，不调判断器。后台通过 `submit_background_result()` 把通知塞进 messages，主循环继续。

**关键**：workflow 完成通知**没有特殊权限**——就是普通消息进对话，裁判根据实际结果判断。判断器不需要知道"是不是 workflow 通知"，只看内容。

#### s16 vs s17 分工

| 层级 | 关注 |
|---|---|
| **s16 Workflow** | "一批工作怎样执行" —— 步骤/并行/schema/恢复 |
| **s17 Goal Loop** | "整件事情是否已完成" —— 结果是否满足最终要求 |

两个机制**正交**。同宿主可同时用：workflow 跑完写结果进对话 → Goal 判断整个任务是否完成 → 没完成继续。

#### 分层哲学再深化

```
LLM 层       ← 语义决策(该做什么、写什么代码)
编排层 s16   ← 并发/journal/schema
Harness 层   ← 工具/权限/记忆/团队/后台/cron
──────────
Goal 层 s17  ← 裁判(判断整体目标是否达成)  ★ 新增
```

**s17 独特之处**：它是这个分层里**第一个"审查主模型自己"的层**。之前所有层都为主模型服务，s17 是**监督主模型**。这是 agent 从"工具集"升级为"能自主奔向目标的智能体"的**质变点**。

### 一句话总结

**s17 Goal Loop 就是给 agent 装了一个"独立裁判"**——用户通过 `/goal <条件>` 声明目标，主循环退出条件从"模型不调工具"升级为"独立判断器读对话记录判断条件已达成"。裁判是**没有工具的只读 LLM**，只能根据对话里已出现的证据（命令输出、测试结果、编译日志）判断。未达成就把理由塞回 messages 让主模型自动 continue 下一轮。用**两道通用兜底**（全局 max_turns + 连续 block 上限）+ **判断器失败保留目标不伪装成功**的安全原则守住自动化边界。它和 s16 正交：s16 管"一批工作怎么高效跑完"，s17 管"整件事到底做没做完"——是全系列 agent 从"聊天工具"升级为"自主奔向目标的智能体"的**最后一块拼图**。

---

## 附录：本次会话关键文件引用

- [s12_cron_scheduler/code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s12_cron_scheduler/code.py)
- [s11_background_tasks/code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s11_background_tasks/code.py)
- [s13_agent_teams/code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s13_agent_teams/code.py)
- [s13_agent_teams/README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s13_agent_teams/README.zh.md)
- [s15_integrated_harness/README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s15_integrated_harness/README.zh.md)
- [s16_workflow_runtime/README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s16_workflow_runtime/README.zh.md)
- [s17_goal_loop/README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s17_goal_loop/README.zh.md)
- [.chatlogs/s15-to-s16-evolution.html](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/.chatlogs/s15-to-s16-evolution.html)（本次生成的可视化图解）
- [.chatlogs/chat-2026-08-27-kimi-k3-setup-and-s01-to-s09-qa.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/.chatlogs/chat-2026-08-27-kimi-k3-setup-and-s01-to-s09-qa.md)（前一天的归档，s01~s11 内容）

---

*本 md 由 AI 助手于 2026-08-28 归档。后续同主题（s12 及后续章节源码解读）对话将继续追加到此文件。*
