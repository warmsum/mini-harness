"""第 16 章：JSON-RPC 2.0 —— 让外部程序驱动 Agent。

对应官方 packages/api/gateway（Host 与 Client 两侧的 Typert RPC
endpoint，:5「Host 入口提供 ctx.typertGateway……两者使用同一份
生成的 InvocationDescriptor 约定」）。教学版实现 JSON-RPC 2.0 的
最小对接口：请求/响应/错误的线格式 + 方法分发器。

为什么 Agent 框架需要 RPC 接口？IDE 插件、CLI、网页前端都要
从「进程外」控制 Agent——发消息、读状态、取结果。RPC 就是
这个进程边界的通用语言：一个端口说人话（JSON 文本），两边
都能实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class RpcRequest:
    """一条 JSON-RPC 请求。"""

    jsonrpc: str
    id: Any
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class RpcError:
    code: int
    message: str


def parse_request(text: str) -> RpcRequest | RpcError:
    """解析线格式文本。三类失败各对应标准错误码。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return RpcError(PARSE_ERROR, "Parse error: 不是合法 JSON")
    if not isinstance(data, dict):
        return RpcError(INVALID_REQUEST, "Invalid Request: 请求必须是 JSON 对象")
    if data.get("jsonrpc") != "2.0":
        return RpcError(INVALID_REQUEST, "Invalid Request: jsonrpc 必须为 2.0")
    method = data.get("method")
    if not isinstance(method, str) or not method:
        return RpcError(INVALID_REQUEST, "Invalid Request: method 缺失")
    params = data.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return RpcError(INVALID_PARAMS, "Invalid params: 必须是 JSON 对象")
    return RpcRequest(
        jsonrpc="2.0",
        id=data.get("id"),
        method=method,
        params=params,
    )


class RpcDispatcher:
    """方法分发器：注册处理器，按 method 路由。

    对应官方 gateway 的 invoke：解析描述符、校验参数、调用公开
    业务方法并校验结果（api-gateway :9）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, method: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        if method in self._handlers:
            raise ValueError(f'方法 "{method}" 已被注册')
        self._handlers[method] = handler

    def dispatch(self, text: str) -> dict[str, Any]:
        """处理一条线格式请求，返回线格式响应。

        任何失败都变成结构化 error 响应——绝不抛异常让对端
        猜发生了什么（JSON-RPC 的「错误也是响应」约定）。"""
        parsed = parse_request(text)
        if isinstance(parsed, RpcError):
            return _error_response(None, parsed)
        handler = self._handlers.get(parsed.method)
        if handler is None:
            return _error_response(
                parsed.id,
                RpcError(METHOD_NOT_FOUND, f"Method not found: {parsed.method}"),
            )
        try:
            result = handler(parsed.params)
        except TypeError as error:
            return _error_response(
                parsed.id, RpcError(INVALID_PARAMS, f"Invalid params: {error}")
            )
        except Exception as error:
            return _error_response(
                parsed.id, RpcError(INTERNAL_ERROR, f"Internal error: {error}")
            )
        return {"jsonrpc": "2.0", "id": parsed.id, "result": result}


def _error_response(request_id: Any, error: RpcError) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": error.code, "message": error.message},
    }
