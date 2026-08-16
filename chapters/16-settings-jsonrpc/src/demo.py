"""第 16 章 demo：配置分层 + JSON-RPC 对接口。

运行（无需 API，纯本地）：
    uv run python chapters/16-settings-jsonrpc/src/demo.py

演示：
1. 分层配置：显式覆盖 > .env > 默认值（用临时 .env 演示）；
2. JSON-RPC 线格式往返：settings.get / echo / 未知方法 /
   非法参数 / 坏 JSON，各打印请求与响应原文。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from rpc import RpcDispatcher
from settings import Settings


def exchange(dispatcher: RpcDispatcher, request: dict) -> None:
    """打印一次线格式往返。"""
    wire = json.dumps(request, ensure_ascii=False)
    response = dispatcher.dispatch(wire)
    print(f"  → {wire}")
    print(f"  ← {json.dumps(response, ensure_ascii=False)}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text(
            "DEEPSEEK_API_KEY=sk-local-test\nMODEL=deepseek-chat\n",
            encoding="utf-8",
        )

        print("=== ① 分层配置 ===")
        settings = Settings(
            env_path=env_path,
            defaults={"MODEL": "deepseek-v4-flash", "MAX_TURNS": "10"},
            overrides={},
        )
        print(f"  MODEL（.env 覆盖默认）: {settings.get('MODEL')}")
        print(f"  MAX_TURNS（只有默认值）: {settings.get('MAX_TURNS')}")
        print(f"  DEEPSEEK_API_KEY（只有 .env）: {settings.get('DEEPSEEK_API_KEY')}")
        settings.set("MODEL", "deepseek-v4-max")  # 显式覆盖，最高优先
        print(f"  MODEL（显式覆盖后）: {settings.get('MODEL')}")

        print()
        print("=== ② JSON-RPC 线格式往返 ===")
        dispatcher = RpcDispatcher()
        dispatcher.register("settings.get", lambda params: settings.get(str(params.get("key", ""))))
        dispatcher.register("echo", lambda params: params.get("text", ""))

        exchange(dispatcher, {"jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"key": "MODEL"}})
        exchange(dispatcher, {"jsonrpc": "2.0", "id": 2, "method": "echo", "params": {"text": "你好"}})
        exchange(dispatcher, {"jsonrpc": "2.0", "id": 3, "method": "unknown.tool", "params": {}})
        exchange(dispatcher, {"jsonrpc": "2.0", "id": 4, "method": "echo", "params": [1, 2]})
        # 缺少 jsonrpc 版本字段 → Invalid Request
        exchange(dispatcher, {"id": 5, "method": "echo", "params": {"text": "缺 jsonrpc 版本"}})
        print("  → 这不是 JSON{{{")
        print(f"  ← {json.dumps(dispatcher.dispatch('这不是 JSON{{'), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
