# 08｜会话持久化

> 预计时间：50 分钟 ｜ 前置：完成第 05 章 ｜ 本章纯本地运行，不调用模型

第 05 章的会话日志目前只保存在内存中，程序退出后历史就会丢失。要支持崩溃恢复、稍后继续或迁移会话，事件日志必须写入磁盘。本章实现一个文件存储，并说明第 05 章 `subscribe` 接口如何用于增量持久化。

DeepSeek Harness 将这项能力实现为独立后端包 session-persistence-jsonl，采用 JSONL 格式：首行是文件头，之后每行保存一个 JSON 对象。JSONL 易于跨语言读取，也便于直接检查；尾部单行损坏时，前面的完整记录仍可恢复。官方默认还支持 zstd 压缩和分片打包，教学版只保留未压缩的 JSONL，以便观察核心流程。

## 学习目标

完成本章后，你将能够：

- 把 `SessionEvent` 按 JSONL 格式写入文件并重新加载；
- 首次创建时原子发布，后续只追加尚未落盘的事件；
- 校验文件头、格式版本和事件编号；
- 识别残缺尾行，并在恢复出的 Session 中为未闭合工具、step 与 turn 补充崩溃收尾事件。

## 8.1 三个必须回答的问题

写一个把日志存进文件的存储，看似只是序列化加写文件，实际有三个问题必须正面回答：

1. 第一次创建怎么避免半文件？先写临时文件、`fsync`，再用 `os.replace` 原子发布 header 和已有前缀。
2. 后续事件怎么保存？目标文件已经公开后不能每次整份重写；`save()` 先确认磁盘日志是内存日志的严格前缀，再只追加新增事件并 `fsync`。
3. 哪些损坏可以修？只有文件末尾“没有换行的最后一个片段”能证明是一次未完成 append，可以安全截断。任何完整行或中间行解析失败都必须拒绝，不能借“崩溃恢复”静默丢掉后续数据。

## 8.2 落盘：首次原子发布，随后仅追加

```python
class JsonlStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, session: Session) -> None:
        if not self.path.exists():
            # header + 当前前缀写入临时文件，fsync 后原子发布
            ...
            os.replace(tmp_path, self.path)
            return

        persisted, torn_offset = self._read_records()
        if torn_offset is not None:
            raise ValueError("请先 load() 修复残缺尾部")
        if tuple(persisted) != session.events[:len(persisted)]:
            raise ValueError("磁盘日志不是当前日志的前缀")
        # 只追加 pending，并 flush + fsync
```

逐段看：

- 首行 header 写 format 与 version。它的用途在加载侧：未来的格式演进靠 version 判断，读错文件靠 format 拦截。
- 每行一条事件，事件四元组 id、type、ts、data 原样序列化。`ensure_ascii=False` 让中文原样保存，文件里的人类可读性更好。
- 首次发布使用临时文件、`fsync` 和 `os.replace`；后续调用验证前缀后仅追加。这保留了官方“已公开历史永不重写”的核心语义。

这里假设只有一个写进程。`exists()` 后再 `os.replace()` 不能阻止两个进程同时首次发布；教学版也没有文件锁。单进程下它能避免暴露半文件，但不能宣称具备跨进程的 no-clobber 保证。

## 8.3 读回：校验与崩溃修复

```python
    def load(self) -> Session:
        if not self.path.exists():
            raise FileNotFoundError(f"会话文件不存在: {self.path}")

        events, torn_offset = self._read_records()
        if torn_offset is not None:
            with self.path.open("r+b") as file:
                file.truncate(torn_offset)
        _append_recovery_closers(events)
        return Session.from_log(events)
```

加载侧的三层防御，对应 8.1 的三个问题：

1. 文件不存在直接抛 `FileNotFoundError`，调用方不该拿到一个空会话假装一切正常。
2. header 校验失败立即抛错。读到别人的文件、未来版本的文件，都该响亮失败而不是猜。
3. `_read_records` 只把末尾未换行片段标为 torn tail；完整坏行一律报错。截断后，加载器总会扫描开放状态，并依次合成缺失的 `tool/result`、`step/end` 和真实 turn 编号的 `turn/end`，不会再写死 `turn=0`。这一步不能只在出现 torn tail 时执行：进程也可能恰好在一条完整事件写完后、收尾事件写入前崩溃。

最后，`Session.from_log` 继续执行第 05 章建立的 id 连续性校验。

## 8.4 运行完整示例

```bash
uv run python chapters/08-persistence/src/demo.py
```

完整输出，本地确定性运行，时间戳会变：

```
=== ① 落盘：磁盘上的 JSONL 原文 ===
  {"format": "mini-harness-jsonl", "version": 1}
  {"id": 0, "type": "turn/start", "ts": ..., "data": {"turn": 1}}
  {"id": 1, "type": "user/message", "ts": ..., "data": {"content": "1+2*3 等于几？"}}
  {"id": 2, "type": "assistant/message", "ts": ..., "data": {"content": null, "tool_call…
  {"id": 3, "type": "tool/result", "ts": ..., "data": {"call_id": "call_1", "content": "…
  {"id": 4, "type": "assistant/message", "ts": ..., "data": {"content": "1+2*3 = 7"}}
  {"id": 5, "type": "turn/end", "ts": ..., "data": {"turn": 1, "reason": "completed"}}
  ← 首行 header，之后每行一条事件

=== ② 读回：重放一致性 ===
  读回 6 条事件，类型序列与原始一致: True

=== ③ 模拟崩溃：最后一条 turn/end 还没写，进程就被杀 ===
  #0  turn/start
  #1  user/message
  #2  assistant/message
  #3  tool/result
  #4  assistant/message
  #5  turn/end  ← 合成收尾
  ← 残缺尾行被截断，缺失的轮次收尾被合成 turn/end 补上
```

第 ③ 节由 demo 主动制造损坏：先删除最后一行 turn/end，模拟轮次尚未写完；再追加半行，模拟进程在写事件时退出。加载器会截断磁盘上的残缺片段，并在返回的内存 Session 中合成收尾事件。即使磁盘末尾是完整行，只要工具、step 或 turn 仍开放，也会合成相同的崩溃收尾。合成事件不会在 `load()` 内自动写回；调用方随后对恢复出的 Session 执行 `save()`，它们才会追加到磁盘。

## 本章小结

- `JsonlStore.save()`：首次原子发布、前缀校验、后续仅追加
- `JsonlStore.load()`：严格 header/事件校验，只截断 torn tail，并始终按开放状态合成工具/step/turn 收尾
- 三层防御加 `from_log` 连续性校验的完整恢复链

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/session/session-persistence-jsonl/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/session/session-persistence-jsonl/README.zh.md) | `JsonlStore` | 对齐仅追加 JSONL 与恢复前校验；教学版首次写使用 `os.replace`，不具备官方跨进程 no-clobber 发布的完整保证 |
| 同上 | `save` | 两者都首次原子发布、随后仅追加；官方额外实现批量 append、写失败回滚与跨平台发布细节 |
| 同上 | （未实现） | 官方还会打包连续流式分片并支持 zstd 压缩；这些是存储优化，教学版不实现 |
| 同上 | 崩溃修复 | 教学版只截断真正不完整的尾部，再按开放状态合成工具、step 与 turn closer；中间损坏响亮失败 |

官方还用协调器订阅 session/event、按窗口批量 flush，并以 zstd checksum frame 作为默认物理格式；教学版由调用方显式 `save()`，每次把尚未保存的尾部追加进去。

## 练习

1. **版本演进。** 把 header 的 version 改成 2 再 load，观察报错；然后给 load 加一个 v1 到 v2 迁移分支，体验格式演进的真实做法。
2. **增量 flush。** 给 JsonlStore 加 `attach(session)` 方法，内部用 `session.subscribe` 监听新事件，攒够 5 条批量追加写盘。实现后思考批量写与原子发布如何兼容。
3. **坏行实验。** 在文件中间而非末尾插入一行垃圾，观察 load 的行为；解释为什么只截断尾行的修复策略对中间坏行无效，以及官方如何应对，官方默认产物的 header 与每个 append 批次都带 checksum frame。
4. **目录布局。** 真实框架的会话文件不会全部堆在一个目录，官方按项目目录与会话 id 分两级目录。设计一个同样的布局，实现 `JsonlStore.locate(session_id)`。
