"""第 03 章 demo：mini-cordis 的生命周期与级联清理。

运行（无需 API，纯本地）：
    uv run python chapters/03-python-cordis/src/demo.py

对照 README 观察四个现象：
1. 插件安装后监听器生效（pending → active）
2. 子插件随父插件卸载（级联）
3. 监听器随插件卸载自动解绑（第二次广播无输出）
4. 清理函数逆序执行（后注册的先清理）
"""

from __future__ import annotations

from context import Context


def heartbeat(ctx: Context, _config: object) -> None:
    """父插件：注册一个监听器、启动一个后台任务、再装一个子插件。"""
    print("  [heartbeat] apply 执行：注册 ping 监听器")
    ctx.on("ping", lambda msg: print(f"  [heartbeat] 收到: {msg}"))

    def stop_task() -> None:
        print("  [heartbeat] 停止后台任务")

    # effect 的参数是「启动函数」：立即执行，返回的停止函数记入清理清单。
    # lambda: stop_task 是为了把 stop_task 作为「返回值」交给 effect，
    # 而不是在这里就调用它。
    ctx.effect(lambda: stop_task)
    print("  [heartbeat] 启动后台任务（模拟每 10 秒保存一次状态）")

    print("  [heartbeat] 安装子插件 child")
    ctx.plugin(child)

    print("  [heartbeat] 安装完成")


def child(ctx: Context, _config: object) -> None:
    """子插件：只注册一份自己的清理。"""

    def stop() -> None:
        print("  [child] 清理执行（父插件卸载 → 子插件被销毁）")

    ctx.effect(lambda: stop)
    print("  [child] apply 执行：安装完成")


def main() -> None:
    ctx = Context()

    print("=== 1. 安装 heartbeat ===")
    handle = ctx.plugin(heartbeat)
    print(f"  [state] heartbeat: {handle.state}")

    print()
    print("=== 2. 广播：监听器生效 ===")
    ctx.emit("ping", "第一次广播")

    print()
    print("=== 3. 卸载 heartbeat（注意清理的先后顺序） ===")
    handle.dispose()
    print(f"  [state] heartbeat: {handle.state}")

    print()
    print("=== 4. 再次广播：监听器已自动解绑 ===")
    ctx.emit("ping", "第二次广播")
    print("  （上面没有收到消息 = 监听器随插件卸载自动解绑）")


if __name__ == "__main__":
    main()
