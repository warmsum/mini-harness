"""第 04 章：mini-cordis 的服务与依赖注入。

在第 03 章基础上新增四样东西：
1. `provide` / `get` —— 服务的注册与查找
2. `inject` 依赖声明 —— 依赖未齐时插件保持 pending，服务后到自动启动
3. `__getattr__` 严格访问 —— 读服务必须先声明依赖
4. `waterfall` —— waterfall 模型事件（可拦截管线）

对应官方 vendor/cordis 的 reflect（服务）与 events（瀑布）模块。
"""

from __future__ import annotations

from typing import Any, Callable

Disposer = Callable[[], None]
PluginFn = Callable[["Context", Any], Disposer | None]


class PluginHandle:
    """一次插件安装的句柄。第 04 章新增：依赖声明与依赖快照。"""

    _next_uid = 1

    def __init__(self, ctx: "Context", plugin: PluginFn, config: Any) -> None:
        self.uid = PluginHandle._next_uid
        PluginHandle._next_uid += 1
        self.name = getattr(plugin, "__name__", f"plugin#{self.uid}")
        self.config = config
        self.state = "pending"

        self._ctx = ctx
        self._plugin = plugin
        self._disposers: list[Disposer] = []
        self._error: Exception | None = None

        # 依赖声明：插件函数的 inject 属性（对应官方 Object.assign(fn, {inject})）
        self.inject: frozenset[str] = frozenset(getattr(plugin, "inject", ()))
        # 依赖快照：inject 声明的服务解析成功后存在这里，
        # __getattr__ 读服务时优先查它（对应官方 fiber.store）
        self._store: dict[str, object] = {}
        self._epoch: str | None = None  # 依赖签名；None = 从未检查

        ctx._handles.append(self)
        if ctx._current is not None:
            ctx._current.collect(self.dispose)

        # 依赖检查：声明了 inject 且依赖未齐 → 保持 pending，等待服务后到
        self._recheck()

    # ------------------------------------------------------------------
    # 依赖重算：服务后到自动启动 / 提供者卸载自动卸载
    # ------------------------------------------------------------------

    def _recheck(self) -> None:
        """重算依赖签名（epoch）。签名变化才动作，避免重复启动。"""
        if self.state == "disposed":
            return
        resolved: dict[str, object] = {}
        tokens: list[str] = []
        for name in self.inject:
            impl = self._ctx._services.get(name)  # (value, provider_uid, version)
            if impl is not None:
                resolved[name] = impl[0]
                tokens.append(f"{impl[1]}:{impl[2]}")
            else:
                tokens.append("-")
        epoch = ",".join(tokens)
        if epoch == self._epoch:
            return
        self._epoch = epoch

        was_active = self.state == "active"
        if was_active:
            self._unload()  # 依赖提供者换了 → 先卸载旧状态

        missing = any(name not in resolved for name in self.inject)
        if missing:
            self.state = "pending"
        else:
            self._store = resolved
            self._run()

    # ------------------------------------------------------------------
    # 状态机（第 03 章相同，卸载时顺带清空依赖快照）
    # ------------------------------------------------------------------

    def _run(self) -> None:
        previous = self._ctx._current
        self._ctx._current = self
        try:
            result = self._plugin(self._ctx, self.config)
            if result is not None:
                self._disposers.append(result)
            self.state = "active"
        except Exception as error:
            self.state = "failed"
            self._error = error
            raise
        finally:
            self._ctx._current = previous

    def _unload(self) -> None:
        for disposer in reversed(self._disposers):
            disposer()
        self._disposers.clear()
        self._store.clear()

    def collect(self, disposer: Disposer) -> None:
        self._disposers.append(disposer)

    def dispose(self) -> None:
        if self.state == "disposed":
            return
        self._unload()
        self.state = "disposed"
        # 卸载后通知全体：依赖本插件服务的插件要跟着卸载
        self._ctx._notify()


class Context:
    """插件安装的环境。第 04 章新增：服务表、依赖注入、瀑布事件。"""

    def __init__(self) -> None:
        self._handles: list[PluginHandle] = []
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._current: PluginHandle | None = None
        # 服务表：name -> (value, provider_uid, version)
        self._services: dict[str, tuple[object, int, int]] = {}
        self._version = 0

    # ------------------------------------------------------------------
    # 插件安装
    # ------------------------------------------------------------------

    def plugin(self, plugin: PluginFn, config: Any = None) -> PluginHandle:
        return PluginHandle(self, plugin, config)

    # ------------------------------------------------------------------
    # 服务：provide / get / notify
    # ------------------------------------------------------------------

    def provide(self, name: str, value: object) -> Disposer:
        """注册一个服务。服务出现 → 通知全体重算依赖（自动启动的触发点）。"""
        self._version += 1
        provider_uid = self._current.uid if self._current is not None else 0
        self._services[name] = (value, provider_uid, self._version)
        self._notify()

        def unregister() -> None:
            if name in self._services:
                del self._services[name]
                self._notify()

        if self._current is not None:
            self._current.collect(unregister)  # 提供者卸载 → 服务随之注销
        return unregister

    def get(self, name: str) -> object | None:
        """非严格查找：找不到返回 None（对应官方 ctx.get）。"""
        impl = self._services.get(name)
        return impl[0] if impl is not None else None

    def _notify(self) -> None:
        """遍历全部句柄重算依赖（官方 reflect.notify 的教学简化版）。"""
        for handle in list(self._handles):
            handle._recheck()

    # ------------------------------------------------------------------
    # 严格访问：读服务必须先声明 inject
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """属性查找失败时被调用——ctx.tools 这类「服务访问」走这里。

        三种结局（对应官方 reflect 的 Proxy get trap）：
        1. 当前插件声明了依赖且已就绪 → 返回服务值
        2. 声明了依赖但未就绪 → 报「依赖未就绪」
        3. 没声明 → 报「必须先 inject」（依赖显式化是语法，不是约定）
        """
        handle = self._current
        if handle is not None:
            if name in handle._store:
                return handle._store[name]
            if name in handle.inject:
                raise AttributeError(f'服务 "{name}" 已声明依赖但尚未就绪')
        if name in self._services:
            raise AttributeError(f'读取服务 "{name}" 前必须在 inject 里声明')
        raise AttributeError(f"Context 没有属性 {name!r}")

    # ------------------------------------------------------------------
    # 事件：on / emit / waterfall
    # ------------------------------------------------------------------

    def on(self, event: str, listener: Callable[..., Any]) -> Disposer:
        self._listeners.setdefault(event, []).append(listener)

        def remove() -> None:
            self._listeners[event].remove(listener)

        if self._current is not None:
            self._current.collect(remove)
        return remove

    def emit(self, event: str, *args: Any) -> None:
        for listener in list(self._listeners.get(event, [])):
            listener(*args)

    def waterfall(self, event: str, *args: Any) -> Any:
        """waterfall 模型事件。最后一个参数是 next（最内层执行器）。

        每个监听器收到 (…args, next)；调 next() 继续向里传，返回值沿链
        回传。监听器调 next() 不带参数时，原参数原样向下传；带参数则用
        新参数。不调 next() 即否决本次派发。
        """
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

    # ------------------------------------------------------------------
    # 副作用入口（第 03 章相同）
    # ------------------------------------------------------------------

    def effect(self, fn: Callable[[], Disposer | None]) -> Disposer:
        disposer = fn()
        if disposer is not None and self._current is not None:
            self._current.collect(disposer)
        return disposer if disposer is not None else (lambda: None)
