# 04｜服务与依赖

> 预计时间：70 分钟 ｜ 前置：完成第 03 章 ｜ 本章纯本地运行，不调用模型

第 03 章的插件系统已经能够安装和清理插件，但插件之间只能通过 `ctx.on` 发送事件，不能直接使用其他插件提供的能力。真实 Agent 中，模型连接和工具注册表由不同插件提供，Agent 循环却同时依赖二者。本章解决插件之间如何声明和获取这些共享能力。

cordis 使用依赖注入解决这个问题。插件在安装前声明自己需要哪些能力，这些能力称为服务；运行环境负责解析依赖。所需服务尚未就绪时，插件保持等待，服务出现后再启动。本章将实现并验证三个行为：

1. 服务后到，插件自动醒来；
2. 提供者被卸载，依赖方自动卸载；
3. 读服务必须先声明依赖，依赖显式化是语法，不是约定。

本章最后还会实现事件系统的升级版，waterfall。官方用它搭起整个工具执行管线，一个插件不碰核心代码，就能给所有工具加上超时和日志。

## 学习目标

完成本章后，你将能够：

- 使用 `provide` 注册服务，并让服务随提供者一起卸载；
- 使用 `inject` 声明依赖，理解插件的等待、启动和重新加载；
- 通过 `__getattr__` 阻止插件读取未声明的服务；
- 理解 waterfall 如何让多个插件依次包装同一次工具执行。

## 4.1 两种共享方式，为什么选依赖注入

插件 A 提供了模型连接，插件 B 要用它。最直白的做法是全局变量：

```python
llm_client = None  # 模块级全局变量

def plugin_a(ctx, _config):
    global llm_client
    llm_client = DeepSeekClient()

def plugin_b(ctx, _config):
    llm_client.chat(...)  # 直接用全局变量
```

这个写法能够运行，但存在三个问题：

1. 顺序耦合。B 必须比 A 晚安装，而且 B 隐含依赖“A 已经安装完成”。插件数量增加后，安装顺序很难维护。
2. 无法卸载。A 被卸载后，`llm_client` 该不该清空？B 还指着它呢。
3. 无法替换。测试时如果要给 B 换成模拟模型，只能修改共享的全局变量，也会影响其他使用者。

依赖注入把找服务从用服务里拆出来。B 不主动找 A，只声明我需要一个叫 llm 的服务；环境负责在 llm 就绪时启动 B，把服务递进 B 的手里，记在 B 的依赖快照里。B 不关心 llm 是谁提供的、什么时候提供的。若要更换实现，先卸载旧 provider，再注册新 provider；同名服务不会被静默覆盖。

官方把这套机制做得更彻底：连读服务这个动作都要检查有没有声明依赖，读未声明的服务直接报错。为什么这么严格？一个没有声明约束的框架里，任何插件都能随手抓任何服务，插件之间的真实依赖关系散落在几百个文件里，卸载一个服务时没人知道谁会受影响。强制声明让依赖关系写在了明面上，环境的登记簿里查得到每个插件依赖谁。

## 4.2 provide 与 get

先给环境加一张服务表。`provide` 注册服务，`get` 非严格查找：

```python
class Context:
    def __init__(self) -> None:
        # 第 03 章字段略
        self._services: dict[str, tuple[object, int, int]] = {}
        self._version = 0

    def provide(self, name: str, value: object) -> Disposer:
        if name in self._services:
            raise ValueError(f'服务 "{name}" 已被注册')
        self._version += 1
        provider_uid = self._current.uid if self._current is not None else 0
        registration = (value, provider_uid, self._version)
        self._services[name] = registration
        self._notify()

        def unregister() -> None:
            if self._services.get(name) is registration:
                del self._services[name]
                self._notify()

        if self._current is not None:
            self._current.collect(unregister)
        return unregister

    def get(self, name: str) -> object | None:
        impl = self._services.get(name)
        return impl[0] if impl is not None else None
```

服务表的值是一个三元组 `(value, provider_uid, version)`，后两项为 4.3 节的依赖重算准备，这里先记住含义：

- `provider_uid` 是谁提供的。第 03 章给每个句柄发过全局唯一的 uid。
- `version` 是第几次 provide。每次 `provide` 递增，同名服务被重新提供时 version 变化，依赖方据此知道自己手里的服务过期了。

`provide` 把 single-shot 注销函数挂到当前插件名下，提供者被卸载，服务随之注销。注册时若名称已经存在会立即报错，调用方必须先卸载旧 provider；注销时还会检查服务表里仍是自己的那一条注册，绝不会误删后来者。

`_notify()` 遍历全部句柄重算依赖，是整套机制的中枢：

```python
    def _notify(self) -> None:
        for handle in list(self._handles):
            handle._recheck()
```

## 4.3 inject 与依赖等待

现在给插件加声明依赖的能力。Python 里最自然的方式是给插件函数挂一个属性，官方 cordis 在 JavaScript 里用 `Object.assign(fn, {inject})`，思路完全一样：

```python
def agent(ctx, _config):
    print(f"启动！llm={ctx.llm} tools={ctx.tools}")

agent.inject = ["llm", "tools"]
```

句柄构造时读取这个声明，然后重算依赖：

```python
class PluginHandle:
    def __init__(self, ctx, plugin, config):
        # 第 03 章字段略
        self.inject = tuple(dict.fromkeys(getattr(plugin, "inject", ())))
        self._store: dict[str, object] = {}   # 依赖快照
        self._epoch: str | None = None        # 依赖签名
        # 其余同第 03 章
        self._recheck()  # 第 03 章这里是直接 _run()
```

`_recheck` 负责比较依赖状态并决定插件下一步行为：

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

第一步，计算签名。每个依赖名解析成一个 `uid:version` 字符串，服务缺失时记为 `-`，最后拼成 epoch。这个签名表示当前插件解析到的全部服务版本。

第二步，签名没变就返回。`_notify` 会在每次 provide 和注销时叫醒全部句柄，签名比较保证只有真正受影响的插件才会动作。没有这一步，每次 provide 都会导致全体插件重装。

第三步，变了就动作。依赖全齐时，卸载旧状态，填好快照，重新执行插件函数，这就是热重载；依赖缺失时，卸载后回到 `pending` 等待。两条路径覆盖了本章开头的两个结果：

- 服务后到自动启动。agent 声明依赖 tools，tools 未提供时签名是 `"uid:ver,-"`，句柄保持 pending；tools 出现后签名变成 `"uid:ver,uid:ver"`，环境重新检查依赖并启动 agent。
- 提供者卸载自动退场。tools 提供者被卸载，第 03 章的 effect 机制自动注销服务，签名里的 tools 变回 `-`，agent 卸载自己回到 pending。

依赖快照 `_store` 保存环境为插件解析出的服务，4.4 节的严格访问会从这里读取。`inject` 使用去重后的 tuple，而不是 set：依赖顺序稳定，epoch 也就不会因哈希顺序变化而抖动。

## 4.4 __getattr__：读服务必须先声明

Python 对象访问不存在的属性时，解释器会调用 `__getattr__`。这给了我们一个实现依赖显式化的机会，与官方 cordis 用 Proxy 拦截属性读取异曲同工：

```python
    def __getattr__(self, name: str) -> Any:
        handle = self._owner or self._root_context()._current
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

1. 声明过且已就绪，返回快照里的服务值，这是 `ctx.llm` 的常规路径；
2. 声明过但没就绪，报已声明依赖但尚未就绪，插件在 pending 期间误读服务时，这个错误能立刻指出问题所在；
3. 根本没声明，报必须先 inject，即使服务明明存在。

第三种结局正是依赖显式化的语法级体现：demo 时刻 3 里，`ctx.llm` 的服务明明在线上，直接读却报错。插件拿到的不是裸 root Context，而是绑定到自己 fiber 的 view；因此事件回调在安装函数返回后再次读取 `ctx.llm`，仍然会使用这个插件的 inject 与依赖快照。若只依赖全局 `_current`，安装结束后回调会丢失身份，正确声明过的服务也读不到。

## 4.5 waterfall

第 03 章的 `emit` 只能广播通知，监听器不能拦、不能改。真实框架需要更强大的形态：一条执行管线，多个插件都能在管线里加一层包装。官方的工具执行管线就是这种形态，权限插件、超时插件、日志插件各挂一层，谁都不用改核心代码。

这里沿用官方术语 waterfall。多个监听器依次包装最内层的执行器：请求从最外层进入，逐层调用到核心执行器；核心返回后，结果再沿原路径逐层返回：

```mermaid
flowchart LR
    A[调用] --> L1[监听器1] --> L2[监听器2] --> C[核心执行器]
    C --> R2[监听器2 收尾] --> R1[监听器1 收尾] --> B[结果]
```

实现，核心只有十行：

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

1. 最后一个参数是 next，最内层的执行器。
2. 每个监听器收到参数加 next，调 `next()` 放行进入内层，返回值沿链回传；不调 `next()` 即否决这次派发，权限插件的用法。
3. `next()` 不带参数时原参数原样下传，带参数则替换，这给拦截改写留了口。

典型用法，一个超时策略插件，给所有工具执行加上日志，不改核心一行：

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

外层 `wrap` 先打印开始，再调用 `next()` 进入 `core_executor`。核心执行器返回结果后，`wrap` 打印完成，并把结果原样交回调用方。这个模式对应官方 Harness 中 `tools/pre-execute → tools/execute → tools/post-execute` 执行管线的简化形态，第 05 章会继续扩展工具相关的数据结构。

## 4.6 作用域：官方的 isolate 与我们的简化

官方 cordis 还有一个本章标题里的概念，作用域 isolate。它的用途是同一个服务名，不同插件看到不同实例，比如每个 agent 各有自己的工具注册表、自己的文件系统后端。官方用 `ctx.isolate(name)` 给服务名分配作用域标签，查找时按标签隔离。

教学版不实现 isolate。需要隔离时，直接创建一个新的 `Context`，让每个子 agent 使用独立环境，第 14 章会采用这种方式。这个方案减少了实例间的内存共享，但概念和实现更简单。官方实现见 `vendor/cordis/src/context.ts` 的 isolate 方法。

## 4.7 运行完整示例

```bash
uv run python chapters/04-services-scopes/src/demo.py
```

完整输出，本地确定性运行：

```
=== 时刻 1：服务后到，插件自动醒来 ===
  [llm-provider] 已提供 llm 服务
  [agent] 当前状态: pending   ← 依赖不齐，安静等待
  [agent] 启动！llm={'provider': 'deepseek', 'model': 'deepseek-chat'} tools={'calculator': 'safe-eval'}
  [tools-provider] 已提供 tools 服务
  [agent] 当前状态: active      ← 依赖齐了，自动启动！

=== 时刻 2：提供者被卸载，依赖方自动卸载 ===
  重名服务被拒绝: 服务 "tools" 已被注册
  卸载 tools v1 后 [agent] 状态: pending
  [agent] 启动！llm={'provider': 'deepseek', 'model': 'deepseek-chat'} tools={'calculator': 'v2'}
  [tools-provider-2] 已提供 tools v2
  注册 tools v2 后 [agent] 状态: active
  卸载 tools v2 后 [agent] 状态: pending   ← 级联卸载

=== 时刻 3：读服务必须 inject ===
  报错: 读取服务 "llm" 前必须在 inject 里声明
  ← 依赖显式化不是约定，是语法

=== 时刻 4：waterfall ===
  [timeout-policy] 开始执行工具 calculator
  [core] 真正执行 calculator……
  [timeout-policy] 工具 calculator 完成
  最终结果: 计算结果: 42
```

一个细节：时刻 2 不允许 v2 直接踩掉 v1。先卸载 v1 后，agent 回到 pending；随后注册 v2，新的 provider uid/version 改变依赖签名，agent 才用新快照重新启动。这样每个服务始终只有一个明确所有者，卸载旧 provider 也不会误删 v2。

## 本章小结

- `provide` 与 `get`：重名拒绝、精确注销、服务表与提供者生命周期联动
- `inject` 与 `_recheck`：依赖签名、pending 等待、热重载、依赖卸载级联
- fiber-bound Context view：回调在安装结束后仍按自己的 inject 读取服务
- `__getattr__`：读服务必须先声明的语法级约束
- `waterfall`：可拦截、可改写、返回值沿链回传
- 作用域 isolate：教学版使用独立 Context 简化，第 14 章会实际使用

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts) | `provide` 与 `_notify` | 官方按名字和作用域通知受影响 fiber；教学版保持重名拒绝与所有权，但用全量重算简化 |
| [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts) | `_recheck` | 官方依赖解析与 epoch 比较在 fiber.ts 内部，签名机制一致 |
| [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | Context view / `__getattr__` | 官方 Proxy 把访问绑定到 fiber 与反射层；Python 版用 owner view 保留同样的调用身份 |
| [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | `waterfall` | 官方与教学版都让监听器按注册顺序包裹内层执行器 |

## 练习

1. **签名推演。** 纸笔推演 demo 时刻 1 中 agent 的签名变化序列，从 `None` 到 `"-,-"` 再到 `"uid:ver,uid:ver"`，每步标注触发的 notify 来源。推演完与代码对照。
2. **循环依赖。** 写两个互相 inject 的插件，A 要 b，B 要 a，观察它们的最终状态，解释为什么谁都启动不了。官方会怎样处理这个问题？查官方文档验证你的猜测。
3. **双层 waterfall。** 给 `tools/execute` 再挂一个重试监听器，失败时重试一次核心执行器，观察两个监听器的包裹顺序与注册顺序的关系。返回值如何穿过两层回到调用方？
4. **参数改写。** 利用 `next()` 带参数即替换的约定，写一个把工具名大写后再传给内层的监听器，验证内层收到的参数确实被改写。这个能力在真实框架里的用途是什么，举一个场景。
