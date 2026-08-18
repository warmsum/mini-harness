# 08｜会话持久化

> 预计时间：50 分钟 ｜ 前置：完成第 05 章 ｜ 本章纯本地运行，不调用模型

到目前为止，会话日志只活在内存里，程序一退出，全部历史随之消失。真实
Agent 必须能落盘：崩溃后恢复、隔天继续、把会话搬到另一台机器，都靠磁盘
上的那份日志。第 05 章埋下的 `subscribe` 接口在本章兑现，写一个把事件
日志写进磁盘的存储。

官方把这件事做成独立后端包 session-persistence-jsonl，格式选 JSONL：每行
一个 JSON 对象，首行是文件头。这个格式的朴素正是它的优点，任何语言都能
读，任何编辑器都能看，一行损坏不影响其他行。官方默认还会做 zstd 压缩与
分片打包，教学版只保留裸 JSONL，核心语义一致。

## 8.1 三个必须回答的问题

写一个把日志存进文件的存储，看似只是序列化加写文件，实际有三个问题必须
正面回答：

1. 写到一半崩溃了怎么办？如果直接往目标文件写，进程在中途被杀，磁盘上
   就留下一个半截文件，下次加载读到坏行，整个会话报废。答案是原子发布：
   先写临时文件，写完整后用 `os.replace` 一次换名。文件系统保证这个换名
   是原子的，任何时刻，目标路径上要么是完整的旧文件，要么是完整的新
   文件，绝无中间态。
2. 加载时遇到坏数据怎么办？文件可能被手动编辑、被截断、被换行符污染。
   答案是分层防御：文件头校验格式与版本，读错文件立刻失败；逐行解析时
   遇到残缺尾行直接截断，并合成一条收尾事件补上缺失的轮次边界。日志在
   磁盘上也必须保持第 05 章建立的轮次闭合不变量。
3. 性能怎么办？每条事件来一次磁盘写，慢。答案是批量落盘：内存里攒一批，
   统一刷。官方叫 flush，配 200ms 合并窗口。教学版把批量省了，demo 的
   会话只有几条事件，`save()` 一次性写入，真实的增量 flush 留给练习 2。

## 8.2 落盘：JSONL 与原子发布

```python
class JsonlStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, session: Session) -> None:
        lines = [
            json.dumps({"format": HEADER_FORMAT, "version": HEADER_VERSION}, ensure_ascii=False)
        ]
        for event in session.events:
            lines.append(
                json.dumps(
                    {
                        "id": event.id,
                        "type": event.type,
                        "ts": event.ts,
                        "data": event.data,
                    },
                    ensure_ascii=False,
                )
            )
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)
```

逐段看：

- 首行 header 写 format 与 version。它的用途在加载侧：未来的格式演进靠
  version 判断，读错文件靠 format 拦截。
- 每行一条事件，事件四元组 id、type、ts、data 原样序列化。
  `ensure_ascii=False` 让中文原样保存，文件里的人类可读性更好。
- `os.replace` 是原子发布的心脏。写入 `.tmp` 临时文件，完整后一次换名。
  官方在 POSIX 上用硬链接无覆盖发布加 fsync 达成同样效果，教学版用
  Python 内置的等价手段。

## 8.3 读回：校验与崩溃修复

```python
    def load(self) -> Session:
        if not self.path.exists():
            raise FileNotFoundError(f"会话文件不存在: {self.path}")

        lines = self.path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        if (
            header.get("format") != HEADER_FORMAT
            or header.get("version") != HEADER_VERSION
        ):
            raise ValueError(f"无法识别的会话文件头: {header}")

        events: list[SessionEvent] = []
        for line in lines[1:]:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # 残缺尾行：截断 + 合成收尾
                events.append(
                    SessionEvent(
                        id=len(events),
                        type="turn/end",
                        ts=0.0,
                        data={"turn": 0, "reason": "crashed"},
                    )
                )
                break
            events.append(
                SessionEvent(
                    id=raw["id"], type=raw["type"], ts=raw["ts"], data=raw["data"]
                )
            )
        return Session.from_log(events)
```

加载侧的三层防御，对应 8.1 的三个问题：

1. 文件不存在直接抛 `FileNotFoundError`，调用方不该拿到一个空会话假装
   一切正常。
2. header 校验失败立即抛错。读到别人的文件、未来版本的文件，都该响亮
   失败而不是猜。
3. `json.loads` 抛 `JSONDecodeError` 的那一行就是进程被杀时写到一半的行，
   后面的内容全部不可信，直接截断，并合成一条 reason 为 crashed 的
   `turn/end`。轮次边界在磁盘上也必须闭合，这是第 05 章不变量在恢复
   路径上的延续。官方对崩溃恢复有同样的设计：保留完整解码记录，从
   不完整处截断，重新编码合成的步骤与轮次 closer。

最后 `Session.from_log` 做第 05 章的 id 连续性校验，三层防御之后还有一个
老朋友把关。

## 8.4 跑一遍完整 demo

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

第 ③ 节的损伤由 demo 自己制造：先把文件最后一行 turn/end 删掉，模拟轮次
没写完就崩溃；再追加半行，模拟正在写一条事件时被杀。加载时两处损伤都被
修复，残缺行截断、收尾合成。日志在磁盘上也保持着内存里的全部不变量，
这是持久化的及格线。

## 本章小结

- `JsonlStore.save()`：JSONL 序列化、临时文件、`os.replace` 原子发布
- `JsonlStore.load()`：header 校验、残缺尾行截断、合成 turn/end 收尾
- 三层防御加 `from_log` 连续性校验的完整恢复链

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/session/session-persistence-jsonl/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/session/session-persistence-jsonl/README.zh.md) | `JsonlStore` | JSONL 后端定义在第 5 行；延迟实体化加硬链接无覆盖发布在第 43 行，与临时文件加 rename 同义 |
| 同上，第 44 行 | `save` | 官方仅追加、绝不重写、失败回滚字节长度，教学版整文件重写，语义等价但非增量 |
| 同上，第 18 行 | （未实现） | 官方 packChunks 把连续流式分片打包成一行，zstd 压缩在第 36 行，纯存储优化，教学版不实现 |
| 同上，第 45 行 | 崩溃修复 | 官方保留完整解码记录、截断不完整尾部、合成步骤与轮次 closer，与教学版截断加合成 turn/end 同构 |

官方的持久化是增量 flush，订阅 session/event、批量落盘；教学版的 `save()`
是快照式。练习 2 会把它改造成增量版，那时第 05 章的 `subscribe` 接口
正式上岗。

## 练习

1. **版本演进。** 把 header 的 version 改成 2 再 load，观察报错；然后给
   load 加一个 v1 到 v2 迁移分支，体验格式演进的真实做法。
2. **增量 flush。** 给 JsonlStore 加 `attach(session)` 方法，内部用
   `session.subscribe` 监听新事件，攒够 5 条批量追加写盘。实现后思考
   批量写与原子发布如何兼容。
3. **坏行实验。** 在文件中间而非末尾插入一行垃圾，观察 load 的行为；
   解释为什么只截断尾行的修复策略对中间坏行无效，以及官方如何应对，
   官方默认产物的 header 与每个 append 批次都带 checksum frame。
4. **目录布局。** 真实框架的会话文件不会全部堆在一个目录，官方按项目
   目录与会话 id 分两级目录。设计一个同样的布局，实现
   `JsonlStore.locate(session_id)`。
