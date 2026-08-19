"""第 16 章 demo：配置分层 + JSON-RPC 对接口。

运行（无需 API，纯本地）：
    uv run python chapters/16-settings-jsonrpc/src/demo.py

演示：
1. 分层配置：schema defaults < composition base < user document，带 revision；
2. JSON-RPC 线格式往返：settings.get / echo / 未知方法 /
   非法参数 / 坏 JSON，各打印请求与响应原文。
"""

from __future__ import annotations

import json
from typing import Any

from rpc import RpcDispatcher
from settings import Settings, SettingsConflictError


def exchange(dispatcher: RpcDispatcher, request: dict[str, Any]) -> None:
    """打印一次线格式往返。"""
    wire = json.dumps(request, ensure_ascii=False)
    response = dispatcher.dispatch(wire)
    print(f"  → {wire}")
    print(f"  ← {json.dumps(response, ensure_ascii=False)}")


def main() -> None:
    print("=== ① namespace + 三层配置 + revision ===")
    settings = Settings(user_document={"agent": {"model": "deepseek-chat"}})
    agent_settings = settings.register(
        "agent",
        defaults={"model": "deepseek-v4-flash", "max_steps": 10},
        base={"max_steps": 20},
    )
    print(f"  解析值: {dict(agent_settings.get())}")
    print("  ← 默认 model 被用户层覆盖，默认 max_steps 被 base 层覆盖")
    revision = agent_settings.revision
    agent_settings.update({"model": "deepseek-v4-max"}, expected_revision=revision)
    print(f"  更新后: {dict(agent_settings.get())}，revision={agent_settings.revision}")
    try:
        agent_settings.update({"max_steps": 30}, expected_revision=revision)
    except SettingsConflictError as error:
        print(f"  陈旧写入被拒绝: [{error.code}] {error}")

    print()
    print("=== ② JSON-RPC 线格式往返 ===")
    dispatcher = RpcDispatcher()
    dispatcher.register(
        "settings.get",
        lambda params: dict(settings.get(str(params.get("namespace", "")))),
    )
    dispatcher.register("echo", lambda params: params.get("text", ""))

    exchange(dispatcher, {"jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"namespace": "agent"}})
    exchange(dispatcher, {"jsonrpc": "2.0", "id": 2, "method": "echo", "params": {"text": "你好"}})
    exchange(dispatcher, {"jsonrpc": "2.0", "id": 3, "method": "unknown.tool", "params": {}})
    exchange(dispatcher, {"jsonrpc": "2.0", "id": 4, "method": "echo", "params": [1, 2]})
    # 缺少 jsonrpc 版本字段 → Invalid Request
    exchange(dispatcher, {"id": 5, "method": "echo", "params": {"text": "缺 jsonrpc 版本"}})
    print("  → 这不是 JSON{{{")
    print(f"  ← {json.dumps(dispatcher.dispatch('这不是 JSON{{'), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
