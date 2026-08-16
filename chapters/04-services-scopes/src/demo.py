"""第 04 章 demo：四个魔法时刻。

运行（无需 API，纯本地）：
    uv run python chapters/04-services-scopes/src/demo.py

对照 README 观察：
1. 服务后到自动启动（agent 等 tools 等到自动醒来）
2. 提供者被卸载 → 依赖方自动卸载
3. 读服务必须 inject（严格访问报错）
4. 洋葱瀑布：不碰核心代码，给所有工具加超时日志
"""

from __future__ import annotations

from context import Context


def llm_provider(ctx: Context, _config: object) -> None:
    ctx.provide("llm", {"provider": "deepseek", "model": "deepseek-chat"})
    print("  [llm-provider] 已提供 llm 服务")


def agent(ctx: Context, _config: object) -> None:
    # 读服务走 __getattr__：声明过 inject 才能读
    print(f"  [agent] 启动！llm={ctx.llm} tools={ctx.tools}")


agent.inject = ["llm", "tools"]  # 依赖声明：官方 cordis 的 Object.assign 模式


def tools_provider(ctx: Context, _config: object) -> None:
    ctx.provide("tools", {"calculator": "safe-eval"})
    print("  [tools-provider] 已提供 tools 服务")


def tools_provider_v2(ctx: Context, _config: object) -> None:
    ctx.provide("tools", {"calculator": "v2"})
    print("  [tools-provider-2] 重新提供 tools（版本+1）")


def main() -> None:
    ctx = Context()
    print("=== 时刻 1：服务后到，插件自动醒来 ===")

    ctx.plugin(llm_provider)

    agent_handle = ctx.plugin(agent)
    print(f"  [agent] 当前状态: {agent_handle.state}   ← 依赖不齐，安静等待")

    ctx.plugin(tools_provider)
    print(f"  [agent] 当前状态: {agent_handle.state}      ← 依赖齐了，自动启动！")

    print()
    print("=== 时刻 2：提供者被卸载，依赖方自动卸载 ===")

    tools_handle = ctx.plugin(tools_provider_v2)
    print(f"  重新 provide tools（版本+1）后 [agent] 状态: {agent_handle.state}")

    tools_handle.dispose()
    print(f"  卸载 tools 提供者后 [agent] 状态: {agent_handle.state}   ← 级联卸载")

    print()
    print("=== 时刻 3：读服务必须 inject ===")
    try:
        print(ctx.llm)  # llm 服务仍在线上，但没人声明依赖它
    except AttributeError as error:
        print(f"  报错: {error}")
        print("  ← 依赖显式化不是约定，是语法")

    print()
    print("=== 时刻 4：洋葱瀑布 ===")

    def timeout_policy(c: Context, _config: object) -> None:
        def wrap(exec_: dict, next_: object) -> str:
            print(f"  [timeout-policy] 开始执行工具 {exec_['name']}")
            result = next_()  # 放行进入内层，返回值沿链回传
            print(f"  [timeout-policy] 工具 {exec_['name']} 完成")
            return result

        c.on("tools/execute", wrap)

    ctx.plugin(timeout_policy)

    def core_executor(exec_: dict) -> str:
        print(f"  [core] 真正执行 {exec_['name']}……")
        return "计算结果: 42"

    result = ctx.waterfall("tools/execute", {"name": "calculator"}, core_executor)
    print(f"  最终结果: {result}")


if __name__ == "__main__":
    main()
