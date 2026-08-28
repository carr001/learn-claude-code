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

## 附录：本次会话关键文件引用

- [s12_cron_scheduler/code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s12_cron_scheduler/code.py)
- [s11_background_tasks/code.py](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s11_background_tasks/code.py)
- [s13_agent_teams/README.zh.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/s13_agent_teams/README.zh.md)
- [.chatlogs/chat-2026-08-27-kimi-k3-setup-and-s01-to-s09-qa.md](/Users/cxhui/Documents/学习文档/ai/learn-claude-code/.chatlogs/chat-2026-08-27-kimi-k3-setup-and-s01-to-s09-qa.md)（前一天的归档，s01~s11 内容）

---

*本 md 由 AI 助手于 2026-08-28 归档。后续同主题（s12 及后续章节源码解读）对话将继续追加到此文件。*
