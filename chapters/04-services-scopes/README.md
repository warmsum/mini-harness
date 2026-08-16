# 04｜服务与依赖：让插件学会「等待」和「拦截」

> 预计时间：70 分钟 ｜ 前置：完成第 03 章 ｜ 本章纯本地运行，不调用模型

第 03 章的插件系统能安装插件、能自动清理，但插件之间是**孤岛**：每个插件
只能靠 `ctx.on` 喊话，没有办法直接使用别的插件提供的能力。真实 Agent 里
这行不通——「模型连接」是一项能力，「工具注册表」是另一项，而 Agent 循环
插件两个都要用。本章解决的就是这个问题：**插件之间如何共享能力**。

官方 cordis 的答案叫**依赖注入**（Dependency Injection）：插件在安装前
**声明**自己需要哪些能力（叫服务，service），环境负责把这些能力递到它
手里；需要的服务还没就绪时，插件**安静等待**，服务一出现，它**自动
启动**。这套机制会带来三个让人眼前一亮的时刻，本章逐一实现并演示：

1. 服务后到，插件自动醒来；
2. 提供者被卸载，依赖方自动卸载；
3. 读服务必须先声明依赖——依赖显式化是语法，不是约定。

本章最后还会实现事件系统的升级版——**瀑布**（waterfall），官方用它搭起
整个工具执行管线：一个插件不碰核心代码，就能给所有工具加上超时、日志。

## 4.1 原理：两种共享能力的方式，为什么选依赖注入

插件 A 提供了模型连接，插件 B 要用它。最直白的做法是什么？全局变量：

```python
llm_client = None  # 模块级全局变量

def plugin_a(ctx, _config):
    global llm_client
    llm_client = DeepSeekClient()

def plugin_b(ctx, _config):
    llm_client.chat(...)  # 直接用全局变量
```

这个写法能跑，但有三个致命伤：

1. **顺序耦合**：B 必须比 A 晚安装，而且 B 内部写死了「A 一定先装好」这个
   假设。插件一多，安装顺序变成暗雷。
2. **无法卸载**：A 被卸载后，`llm_client` 该不该清空？B 还指着它呢。
3. **无法替换**：测试时想给 B 换个假模型，只能改全局变量，牵一发动全身。

依赖注入把「找服务」从「用服务」里拆出来。B 不主动找 A，只声明
「我需要一个叫 llm 的服务」；环境负责在 llm 就绪时启动 B，把服务递进
B 的手里（记在 B 的依赖快照里）。B 从此不关心 llm 是谁提供的、什么时候
提供的、会不会被换成别的——这正是 3.1 节「复用与替换」的落地。

官方把这套机制做得更彻底：连「读服务」这个动作都要检查你有没有声明
依赖（读未声明的服务直接报错）。为什么要这么严格？想象一个没有声明
约束的框架：任何插件都能随手抓任何服务，插件之间的真实依赖关系散落在
几百个文件里，卸载一个服务时没人知道谁会受影响。强制声明让依赖关系
**写在了明面上**——环境的登记簿里查得到每个插件依赖谁。

## 4.2 provide / get：服务的注册与查找

先给环境加一张**服务表**。`provide` 注册服务，`get` 非严格查找：

```python
class Context:
    def __init__(self) -> None:
        # ...（第 03 章字段略）
        self._services: dict[str, tuple[object, int, int]] = {}
        self._version = 0

    def provide(self, name: str, value: object) -> Disposer:
        self._version += 1
        provider_uid = self._current.uid if self._current is not None else 0
        self._services[name] = (value, provider_uid, self._version)
        self._notify()

        def unregister() -> None:
            if name in self._services:
                del self._services[name]
                self._notify()

        if self._current is not None:
            self._current.collect(unregister)
        return unregister

    def get(self, name: str) -> object | None:
        impl = self._services.get(name)
        return impl[0] if impl is not None else None
```

服务表的值是一个三元组 `(value, provider_uid, version)`，后两项是为
4.3 节的依赖重算准备的，这里先记住它们的含义：

- `provider_uid`：谁提供的。第 03 章给每个句柄发过全局唯一的 uid。
- `version`：第几次 provide。每次 `provide` 递增——**同名服务被重新提供
  时，version 变化**，依赖方据此知道自己手里的服务过期了。

另外注意 `provide` 把注销函数挂到了当前插件名下：**提供者被卸载，服务
随之注销**（第 03 章的 effect 机制自动完成）。这正是官方语义——服务跟着
它的提供者走。

`_notify()` 遍历全部句柄重算依赖，是整套机制的中枢：

```python
    def _notify(self) -> None:
        for handle in list(self._handles):
            handle._recheck()
```

## 4.3 inject 与依赖等待：服务后到，自动醒来

现在给插件加「声明依赖」的能力。Python 里最自然的方式是给插件函数挂
一个属性（官方 cordis 在 JavaScript 里用 `Object.assign(fn, {inject})`，
思路完全一样）：

```python
def agent(ctx, _config):
    print(f"启动！llm={ctx.llm} tools={ctx.tools}")

agent.inject = ["llm", "tools"]
```

句柄构造时读取这个声明，然后**重算依赖**：

```python
class PluginHandle:
    def __init__(self, ctx, plugin, config):
        # ...（第 03 章字段略）
        self.inject = frozenset(getattr(plugin, "inject", ()))
        self._store: dict[str, object] = {}   # 依赖快照
        self._epoch: str | None = None        # 依赖签名
        # ...
        self._recheck()  # 第 03 章这里是直接 _run()
```

`_recheck` 是本章的心脏：

```python
    def _recheck(self) -> None:
        if self.state == "disposed":
            return
        resolved: dict[str, object] = {}
        tokens: list[str] = []
        for name in self.inject:
            impl = self._ctx._services.get(name)
            if impl is not None:
                resolved[name] = impl[0]
                tokens.append(f"{impl[1]}:{impl[2]}")  # uid:version
            else:
                tokens.append("-")
        epoch = ",".join(tokens)
        if epoch == self._epoch:
            return
        self._epoch = epoch

        was_active = self.state == "active"
        if was_active:
            self._unload()

        missing = any(name not in resolved for name in self.inject)
        if missing:
            self.state = "pending"
        else:
            self._store = resolved
            self._run()
```

逐段推演这套逻辑：

**第一步：算签名。** 每个依赖名解析出一个 `uid:version` 字符串，拼成
签名（epoch）。服务缺失的依赖记 `-`。签名精确描述了「我看到的服务世界
长什么样」。

**第二步：签名没变就返回。** `_notify` 会在每次 provide/注销时叫醒全部
句柄，签名比较保证只有**真正受影响**的插件才会动作——没有这一步，每次
provide 都会导致全体插件重装。

**第三步：变了就动作。** 依赖全齐：卸载旧状态（如果之前活着），填好
快照，重新执行插件函数——这就是「热重载」；依赖缺失：卸载后回到
`pending` 等待。两条路径覆盖了本章开头的两个魔法时刻：

- **服务后到自动醒来**：agent 声明依赖 tools，tools 未提供时签名是
  `"uid:ver,-"`，句柄保持 pending；tools 出现后签名变成
  `"uid:ver,uid:ver"`，不等任何人吩咐，agent 自己启动。
- **提供者卸载自动退场**：tools 提供者被卸载（第 03 章的 effect 机制
  自动注销服务），签名里的 tools 变回 `-`，agent 卸载自己回到 pending。

依赖快照 `_store` 是「环境递给插件的那只手」——4.4 节的严格访问就查它。

## 4.4 __getattr__：读服务必须先声明

Python 对象访问不存在的属性时，解释器会调用 `__getattr__`。这给了我们
一个实现「依赖显式化」的机会——与官方 cordis 用 Proxy 拦截属性读取
异曲同工（官方 `context.ts` 第 74 行）：

```python
    def __getattr__(self, name: str) -> Any:
        handle = self._current
        if handle is not None:
            if name in handle._store:
                return handle._store[name]
            if name in handle.inject:
                raise AttributeError(f'服务 "{name}" 已声明依赖但尚未就绪')
        if name in self._services:
            raise AttributeError(f'读取服务 "{name}" 前必须在 inject 里声明')
        raise AttributeError(f"Context 没有属性 {name!r}")
```

三种结局：

1. **声明过且已就绪** → 返回快照里的服务值（`ctx.llm` 的常规路径）；
2. **声明过但没就绪** → 报「已声明依赖但尚未就绪」——插件在 pending 期
   间误读服务时，这个错误能立刻指出问题所在；
3. **根本没声明** → 报「必须先 inject」——即使服务明明存在。

第三种结局正是依赖显式化的语法级体现：demo 时刻 3 里，`ctx.llm` 的服务
明明在线上，直接读却报错。值得注意的是 `__getattr__` 的一个工程代价：
它只在普通属性查找失败时才被调用，且会干扰 `copy`/`pickle` 等依赖属性
探测的库——官方 cordis 的 Proxy 同样有这类取舍，这是「严格」的代价。

## 4.5 waterfall：洋葱模型

第 03 章的 `emit` 只能广播通知，监听器不能拦、不能改。真实框架需要
更强大的形态：一条**执行管线**，多个插件都能在管线里加一层包装。官方
的工具执行管线就是这种形态——权限插件、超时插件、日志插件各挂一层，
谁都不用改核心代码。

这种形态叫 **waterfall**（瀑布），俗称洋葱模型：监听器像洋葱皮一样
一层层包住最内层的执行器。请求从最外层进入，一层层往里钻，到达最内层
执行器后，结果再一层层往外返回：

```mermaid
flowchart LR
    A[调用] --> L1[监听器1] --> L2[监听器2] --> C[核心执行器]
    C --> R2[监听器2 收尾] --> R1[监听器1 收尾] --> B[结果]
```

实现（核心只有十行）：

```python
    def waterfall(self, event: str, *args: Any) -> Any:
        *call_args, next_fn = args
        listeners = list(self._listeners.get(event, []))

        def dispatch(index: int, *inner_args: Any) -> Any:
            if index >= len(listeners):
                return next_fn(*inner_args)
            listener = listeners[index]

            def deeper(*new_args: Any) -> Any:
                return dispatch(index + 1, *(new_args if new_args else inner_args))

            return listener(*inner_args, deeper)

        return dispatch(0, *call_args)
```

三个约定：

1. **最后一个参数是 next**：最内层的执行器。
2. **每个监听器收到 `(…参数, next)`**：调 `next()` 放行进入内层，返回值
   沿链回传；不调 `next()` 即否决这次派发（权限插件的用法）。
3. **`next()` 不带参数时原参数原样下传**，带参数则替换——这给拦截改写
   留了口子。

典型用法——一个「超时策略」插件，给所有工具执行加上日志，不改核心一行：

```python
def timeout_policy(c: Context, _config) -> None:
    def wrap(exec_: dict, next_: object) -> str:
        print(f"开始执行工具 {exec_['name']}")
        result = next_()
        print(f"工具 {exec_['name']} 完成")
        return result

    c.on("tools/execute", wrap)
```

执行时：

```python
result = ctx.waterfall("tools/execute", {"name": "calculator"}, core_executor)
```

外层 `wrap` 先打印「开始」，调 `next()` 钻进 `core_executor` 真正干活，
拿回结果打印「完成」，把结果原样交回调用方。这个模式在官方 Harness 里
就是 `tools/pre-execute → tools/execute → tools/post-execute` 三级瀑布的
雏形，第 05 章会把它做成真正的工具注册表。

## 4.6 作用域：官方的 isolate 与我们的简化

官方 cordis 还有一个本章标题里的概念：**作用域**（isolate）。它的用途
是「同一个服务名，不同插件看到不同实例」——比如每个 agent 各有自己的
工具注册表、自己的文件系统后端。官方用 `ctx.isolate(name)` 给服务名
分配作用域标签，查找时按标签隔离。

教学版做了诚实的简化：**不实现 isolate**。需要隔离时，直接创建一个全新
的 `Context`（每个子 agent 一个独立环境）——第 14 章的子 agent 就这么做。
这个简化损失了一点内存共享，换来了概念上的干净。官方实现见
`vendor/cordis/src/context.ts` 的 isolate 方法，学有余力时对照阅读。

## 4.7 跑一遍完整 demo

```bash
uv run python chapters/04-services-scopes/src/demo.py
```

完整输出（本地确定性运行）：

```
=== 时刻 1：服务后到，插件自动醒来 ===
  [llm-provider] 已提供 llm 服务
  [agent] 当前状态: pending   ← 依赖不齐，安静等待
  [agent] 启动！llm={'provider': 'deepseek', 'model': 'deepseek-chat'} tools={'calculator': 'safe-eval'}
  [tools-provider] 已提供 tools 服务
  [agent] 当前状态: active      ← 依赖齐了，自动启动！

=== 时刻 2：提供者被卸载，依赖方自动卸载 ===
  [agent] 启动！llm={'provider': 'deepseek', 'model': 'deepseek-chat'} tools={'calculator': 'v2'}
  [tools-provider-2] 重新提供 tools（版本+1）
  重新 provide tools（版本+1）后 [agent] 状态: active
  卸载 tools 提供者后 [agent] 状态: pending   ← 级联卸载

=== 时刻 3：读服务必须 inject ===
  报错: 读取服务 "llm" 前必须在 inject 里声明
  ← 依赖显式化不是约定，是语法

=== 时刻 4：洋葱瀑布 ===
  [timeout-policy] 开始执行工具 calculator
  [core] 真正执行 calculator……
  [timeout-policy] 工具 calculator 完成
  最终结果: 计算结果: 42
```

值得留意的细节：时刻 2 里 agent 打印了两次「启动！」——第二次是
`tools-provider-2` 用新版本重新提供 tools 时，agent 的依赖签名变化触发的
**热重载**（先卸载旧状态、再用新服务重新启动）。这正是 4.3 节签名里
`version` 字段的作用：不只感知「有没有」，还感知「换没换」。

## 4.8 本章小结：亲手写了什么

- `provide` / `get`：服务表 + 版本号 + 提供者注销联动
- `inject` + `_recheck`：依赖签名（uid:version）、pending 等待、热重载、
  依赖卸载级联
- `__getattr__`：读服务必须先声明的语法级约束
- `waterfall`：洋葱模型——可拦截、可改写、返回值沿链回传
- 作用域 isolate：理解概念，教学版用「新 Context」简化，第 14 章兑现

## 4.9 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/vendor/cordis/src/reflect.ts) | `provide` / `_notify` | 官方 provide（第 277 行）与 notify（第 314 行）；官方 notify 按名字过滤、按作用域隔离，教学版全量重算 |
| [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/vendor/cordis/src/fiber.ts) | `_recheck` | 官方依赖解析 `_checkImpl` + epoch 比较 `_setEpoch`（fiber.ts 内部），签名机制一致 |
| [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/vendor/cordis/src/context.ts) | `__getattr__` | 官方 Proxy（第 74 行）+ reflect 的 get trap（第 144 行报错） |
| [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/vendor/cordis/src/events.ts) | `waterfall` | 官方瀑布（第 234-238 行）用 `cbs.shift() ?? inner` 迭代实现，与我们递归等价 |

## 4.10 练习

1. **签名推演**：纸笔推演 demo 时刻 1 中 agent 的签名变化序列
   （`None → "-,-" → "1:1,3:2"`，具体数字以实际 uid/version 为准），
   每步标注触发的 notify 来源。
2. **循环依赖**：写两个互相 inject 的插件（A 要 b，B 要 a），观察它们的
   最终状态，解释为什么谁都启动不了；再想想官方会怎样处理这个问题。
3. **双洋葱**：给 `tools/execute` 再挂一个「重试」监听器（失败时重试
   一次核心执行器），观察两个监听器的包裹顺序与注册顺序的关系。
4. **参数改写**：利用「`next()` 带参数即替换」的约定，写一个把工具名
   大写后再传给内层的监听器，验证内层收到的参数确实被改写。
