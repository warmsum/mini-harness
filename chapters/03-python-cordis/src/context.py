"""第 03 章：mini-cordis —— 一个迷你插件系统。

对应官方 DSH vendor 目录下的 cordis（DSH 的插件底座）。
本章实现它的核心三件事：
1. `Context`      —— 插件安装的环境（容器）
2. `PluginHandle` —— 一次安装的句柄：状态机 + 资源清单
3. `effect`       —— 副作用的唯一入口：注册资源的同时注册它的清理方式

第 04 章在此基础上加入服务与依赖注入。
"""

from __future__ import annotations

from typing import Any, Callable

# 清理函数：被调用时释放某份资源（解绑监听器、关文件、停任务……）
Disposer = Callable[[], None]

# 插件本体：收到 ctx 与 config，返回一个可选的清理函数
PluginFn = Callable[["Context", Any], Disposer | None]


class PluginHandle:
    """一次插件安装的句柄，记录这次安装的状态与全部资源。

    状态机（对应官方 Fiber 状态机的教学子集）：
        pending（已创建）→ active（安装成功）→ disposed（已卸载）
        安装失败 → failed
    """

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

        # 1) 登记到环境：全局可见
        ctx._handles.append(self)

        # 2) 级联销毁的关键一步：如果此刻有插件正在安装（父插件），
        #    把「销毁自己」挂到父插件的清理清单上。父被卸载 → 子自动被卸载。
        if ctx._current is not None:
            ctx._current.collect(self.dispose)

        # 3) 立即安装
        self._run()

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """执行插件本体，把返回的清理函数收进清单。"""
        previous = self._ctx._current
        self._ctx._current = self  # 让 apply 期间注册的资源都记到本句柄名下
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

    def collect(self, disposer: Disposer) -> None:
        """把一份清理函数记到本句柄名下（effect / on 内部都调它）。"""
        self._disposers.append(disposer)

    def dispose(self) -> None:
        """卸载：逆序执行全部清理函数（后注册的先清理）。"""
        if self.state == "disposed":
            return
        for disposer in reversed(self._disposers):
            disposer()
        self._disposers.clear()
        self.state = "disposed"


class Context:
    """插件安装的环境：管理句柄与事件监听器。"""

    def __init__(self) -> None:
        self._handles: list[PluginHandle] = []
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._current: PluginHandle | None = None

    # ------------------------------------------------------------------
    # 插件安装
    # ------------------------------------------------------------------

    def plugin(self, plugin: PluginFn, config: Any = None) -> PluginHandle:
        """安装一个插件，返回它的句柄。"""
        return PluginHandle(self, plugin, config)

    # ------------------------------------------------------------------
    # 副作用：effect 是唯一的入口
    # ------------------------------------------------------------------

    def effect(self, fn: Callable[[], Disposer | None]) -> Disposer:
        """立即执行 fn，把它返回的清理函数挂到当前插件名下。

        为什么需要这个入口？插件的 apply 期间会创建各种资源（监听器、
        文件、任务），创建资源的代码和释放资源的代码往往相隔很远。
        effect 把两者绑在一起：谁创建谁负责交出清理方式，句柄负责在
        卸载时统一执行。
        """
        disposer = fn()
        if disposer is not None and self._current is not None:
            self._current.collect(disposer)
        return disposer if disposer is not None else (lambda: None)

    # ------------------------------------------------------------------
    # 事件：on / emit
    # ------------------------------------------------------------------

    def on(self, event: str, listener: Callable[..., Any]) -> Disposer:
        """注册事件监听器。返回的解绑函数同时挂在当前插件名下——
        插件卸载时监听器自动解绑，不会留下「幽灵监听器」。"""
        self._listeners.setdefault(event, []).append(listener)

        def remove() -> None:
            self._listeners[event].remove(listener)

        if self._current is not None:
            self._current.collect(remove)
        return remove

    def emit(self, event: str, *args: Any) -> None:
        """同步广播事件给全部监听器（不等待返回值）。"""
        for listener in list(self._listeners.get(event, [])):
            listener(*args)
