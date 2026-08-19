# 10｜文件系统

> 预计时间：70 分钟 ｜ 前置：完成第 02 章（回归工具主线） ｜ 本章纯本地运行，不调用模型

第 02 章的 calculator 是一个纯函数工具：接收参数、返回结果，不修改外部环境。文件工具则会读取和改写真实代码库，因此需要额外处理两个问题：

1. 边界。Agent 能写哪些文件？一个 bug 或一次恶意诱导，模型的 write 调用会不会把用户家目录删了？
2. 并发。Agent 读到文件内容后、写回之前，用户自己改了同一个文件，Agent 一写就把用户的新改动覆盖了，怎么办？

官方用三个包回答这两个问题：tool-fs 工具层、fs-sandbox 围栏、fs-observation-policy 观察策略。本章把它们合并成一套教学实现：五个文件工具、一个沙箱围栏、一个读后写观察器。

## 学习目标

完成本章后，你将能够：

- 说明 `read-only`、`workspace-write` 与 `danger-full-access` 三种模式的边界；
- 在写文件前规范化路径，并拒绝工作区之外的目标；
- 用读后写检查发现未读取文件和过期版本；
- 实现 read、write、edit、grep 与 glob 这组基础文件工具。

## 10.1 原理：两个问题的官方答案

边界问题的答案是三模式沙箱。官方把文件写效应分成三档：

| 模式 | 能写哪里 |
|------|----------|
| `read-only` | 哪都不能写，读永远放行 |
| `workspace-write` | 工作区根 + 平台临时目录 |
| `danger-full-access` | 任何地方，显式无约束模式 |

这里有两个要点。第一，策略主要约束写操作；读取不会修改文件系统，写入则可能覆盖或删除数据。第二，workspace-write 的可写集合包含工作区和临时目录，因为程序经常需要在 `/tmp` 等位置保存中间产物。

并发问题的答案是读后写观察策略。官方要求模型写已存在的文件前必须先读过它，并在读的时候记录文件状态，写之前核对状态没变，这就是 CAS，compare-and-swap，比较后交换。两道门：没读过就写，以 FS_NOT_OBSERVED 拒绝；读过但文件已被外部改动，以 FS_STALE_VERSION 拒绝，要求重新读。这模拟了工程师的真实工作流：改代码前先看代码，别人动过的代码重新看一遍再改。

## 10.2 沙箱围栏：fence_write

围栏是本章的安全核心，代码不长，每一行都有讲究：

```python
@dataclass(frozen=True)
class SandboxPolicy:
    mode: str = READ_ONLY
    workspace_root: Path = field(default_factory=Path.cwd)

    def __post_init__(self) -> None:
        if self.mode not in WIDER_MODES:
            raise ValueError(f"未知 sandbox mode: {self.mode}")

    def writable_roots(self) -> list[Path]:
        return [
            self.workspace_root,
            Path(tempfile.gettempdir()),
            Path("/tmp"),
        ]

    def fence_write(self, target: Path) -> Path:
        if self.mode == DANGER_FULL_ACCESS:
            return target
        if self.mode == READ_ONLY:
            raise SandboxDeniedError(str(target), self.mode)
        resolved = target.resolve()
        for root in self.writable_roots():
            root_resolved = root.resolve()
            try:
                resolved.relative_to(root_resolved)
                return resolved
            except ValueError:
                continue
        raise SandboxDeniedError(str(target), self.mode)
```

三个关键点：

1. `target.resolve()` 先得到规范化后的真实路径。`workspace/../etc/passwd` 在原始字符串中带有工作区前缀，但规范化后已经位于工作区之外；指向工作区外的符号链接也会被解析到真实目标。官方同样要求在委托前再次规范化路径，以发现工具解析后发生变化的祖先符号链接。
2. `relative_to` 判断包含关系：解析后的目标必须在某个可写根之下。抛 ValueError 表示不在其下，换下一个根；全部试完仍不在，拒绝。
3. 拒绝要响亮且结构化：`SandboxDeniedError` 携带模式信息，格式对齐官方的模型可见标记 `[sandbox: file access denied under <mode> mode]`。模型读到这个错误能立刻理解这是权限问题，下一轮换条路。官方文档写明拒绝是结构化 FsError，不靠 stderr 文本推断。

这里需要明确能力边界：路径围栏是一项约束，不是内核级安全隔离。它能够阻止模型误写工作区之外的路径，但不能抵御恶意代码主动绕过。第 11 章会进一步说明 shell 命令所需的内核级隔离。

## 10.3 观察器：读后写的两道门

```python
@dataclass
class ObservationTracker:
    _observed: dict[str, int] = field(default_factory=dict)

    def record_read(self, path: Path) -> None:
        key = str(path.resolve())
        try:
            self._observed[key] = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._observed.pop(key, None)

    def check_write(self, path: Path) -> None:
        key = str(path.resolve())
        if key not in self._observed:
            raise PermissionError(f"[FS_NOT_OBSERVED] 修改 {path} 之前必须先 read 它")
        current = path.stat().st_mtime_ns
        if current != self._observed[key]:
            raise PermissionError(
                f"[FS_STALE_VERSION] {path} 自上次读取后被外部修改"
                f"（mtime 变化），请重新 read 后再写"
            )
```

- 观察记录使用 mtime 快照：读文件时记下修改时间，写入前再次比较。mtime 的实际时间分辨率取决于文件系统；如果两次修改间隔过短，记录值可能不变，因此 demo 在模拟外部修改前短暂等待。
- 键用 `resolve()` 规范化。macOS 上 `/var` 是指向 `/private/var` 的符号链接，读时记的键和写时查的键若不统一，会出现明明读过却报没读的假阴性。
- 两道门都只防无意的覆盖。CAS 的语义是比较后交换：读完作为比较基准，之后写完之前世界没变才放行，变了就要求重新读。成功写入后观察器会记录新版本，因此同一 Agent 后续写入仍有新鲜基准。这是并发编辑的最小防御，不是文件锁。

## 10.4 五个文件工具

工具层把围栏和观察器串进每个操作。`read_file` 带行号输出并记录观察：

```python
def read_file(path: Path, tracker: ObservationTracker) -> str:
    text = path.read_text(encoding="utf-8")
    tracker.record_read(path)
    lines = text.splitlines()
    numbered = "\n".join(f"{i + 1:>4}: {line}" for i, line in enumerate(lines))
    return f"{numbered}\n\n(End of file - total {len(lines)} lines)"
```

`edit_file` 是 str-replace 局部替换，处理一个经典问题：old_string 匹配多处时的歧义：

```python
def edit_file(path, old_string, new_string, policy, tracker, replace_all=False):
    target = policy.fence_write(path)
    if not target.exists():
        raise FileNotFoundError(f"[FS_NOT_FOUND] {target} 不存在")
    tracker.check_write(target)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ValueError(f"[FS_EDIT_NOT_FOUND] old_string 未在 {target} 中找到")
    if count > 1 and not replace_all:
        raise ValueError(
            f"[FS_AMBIGUOUS_EDIT] old_string 在 {target} 中匹配了 {count} 处；"
            "请提供更具体的 old_string，或设置 replace_all=True"
        )
    updated = text.replace(old_string, new_string)
    target.write_text(updated, encoding="utf-8")
    return f"updated {target} ({count} 处替换)"
```

歧义报错是教学重点。模型经常写出太短的 old_string，两个字在文件里出现两次，这时拒绝并说明原因，比悄悄改第一处好得多，模型下一轮会给出更具体的匹配串。错误信息本身就是给模型看的指导。

`grep` 与 `glob` 是搜索工具，正则搜内容、模式找文件，完整实现见源码。

## 10.5 运行完整示例

```bash
uv run python chapters/10-filesystem/src/demo.py
```

完整输出，本地确定性运行，临时路径随系统变化：

```
━━━ 1. read_file：带行号 + 页脚 ━━━
   1: 第一行：hello
   2: 第二行：world

(End of file - total 2 lines)

━━━ 2. 观察策略：没读过的文件不许改 ━━━
  [FS_NOT_OBSERVED] 修改 …/workspace/todo.txt 之前必须先 read 它

━━━ 3. 歧义编辑：old_string 匹配多处 ━━━
  [FS_AMBIGUOUS_EDIT] old_string 在 …/workspace/todo.txt 中匹配了 2 处；请提供更具体的 old_string，或设置 replace_all=True
  updated …/workspace/todo.txt (2 处替换)

━━━ 4. 外部修改：读后写不是盲写（mtime CAS） ━━━
   1: name,score

(End of file - total 1 lines)
  [FS_STALE_VERSION] …/workspace/data.csv 自上次读取后被外部修改（mtime 变化），请重新 read 后再写
  重新读取后：
   1: name,score
   2: external,edit

(End of file - total 2 lines)
  written 20 chars to …/workspace/data.csv

━━━ 5. grep 与 glob ━━━
  grep 'sandbox':
todo.txt:1: 复习 sandbox
  glob '*.txt':
notes.txt
todo.txt

━━━ 6. 逃出工作区：沙箱拒绝 ━━━
  [sandbox: file access denied under workspace-write mode]: /Users/…/.mini-harness-escape-test.txt

━━━ 7. 升级审批：严格更宽 ━━━
  升级到 danger-full-access：获批（教学版直接放行）
```

七部分输出覆盖了文件工具的一条完整路径。拒绝信息同时给出修正方向：文件未读取时先读；匹配不唯一时提供更具体的文本；文件已变化时重新读取；路径越界时改用允许的位置。

## 本章小结

- `SandboxPolicy.fence_write`：三模式、可写根集合、resolve 规范化、结构化拒绝
- `ObservationTracker`：读记 mtime、写前 CAS 双门
- 五个工具：read 带行号与页脚并记录观察，write 全量写入，edit 唯一匹配 str-replace，grep 搜内容，glob 找文件
- 升级审批：严格更宽表

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/fs/fs-sandbox/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/fs/fs-sandbox/README.zh.md) | `SandboxPolicy` | 对齐三种模式、可写根集合、“约束而非安全边界”的定位与结构化 FsError |
| [`packages/fs/fs-observation-policy/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/fs/fs-observation-policy/README.zh.md) | `ObservationTracker` | 官方读后写 CAS 思想；官方经 fs 事件门禁实现，write-intent 与 observed 两类事件，教学版简化为 mtime 快照 |
| [`packages/fs/tool-fs/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/fs/tool-fs/README.zh.md) | 五个工具函数 | 官方工具层还负责把 FsError 渲染成模型可见的 sandbox 标记 |

## 练习

1. **符号链接逃逸。** 在工作区内建一个指向家目录的符号链接，尝试经它写入，观察 resolve 如何识破；再把链接目标换成工作区内文件，对比结果。
2. **read-only 模式。** 把 policy 换成 read-only，重跑 demo，观察哪些操作还能通过、哪些被拒，解释读永远放行的设计。
3. **误报与漏报。** 观察器的 mtime 方案有两个已知弱点：同一纳秒内两次修改检测不到，这是漏报；touch 一下就会触发拒绝，这是误报。讨论官方用事件机制为什么能做得更准。
4. **错误即指导。** 把 edit_file 的歧义报错信息改得含糊，只报失败两个字，再扮演模型尝试修正，体会错误信息质量对 Agent 自愈能力的影响。
