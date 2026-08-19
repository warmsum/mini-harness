# 16｜配置与 RPC

> 预计时间：55 分钟 ｜ 前置：完成第 07 章 ｜ 本章纯本地运行，不调用模型

当 Agent 需要被 IDE 插件、命令行工具或网页前端调用时，外部进程必须能够发送消息、查询状态并读取结果。这带来本章的两个主题：

1. 配置：这些外部入口和 Agent 本体怎么共享同一套配置，不互相矛盾？
2. RPC：进程之间的调用语言长什么样，错误怎么表达？

官方把第二个主题做成了 Typert RPC 网关，为 Host 与 Client 两侧的 Cordis 环境提供 endpoint。教学版实现它的协议近亲 JSON-RPC 2.0，一个很小的标准，足够讲清跨进程调用的核心问题。

## 学习目标

完成本章后，你将能够：

- 按 namespace 注册配置，并按“schema 默认值、组合 base、用户文档”解析；
- 使用深度不可变快照、revision 和 Compare-and-Swap 防止配置被误改或覆盖；
- 读懂 JSON-RPC 2.0 的请求、成功响应和错误响应；
- 实现请求解析、方法注册与分发；
- 把不可信输入转换成结构化协议错误，而不是让服务进程退出。

## 16.1 原理：配置为什么要分 namespace 和层

真实 Harness 由很多插件组成。模型插件可能有 `model`，搜索插件也可能有 `model`；如果所有键都塞进一个大字典，重名和归属很快变得混乱。官方让每个插件注册自己的 namespace，例如 `agent`、`web-search-deepseek`，然后在该分节内部做三层解析：

```
schema defaults  <  composition base  <  user document
```

- schema defaults 是插件自带的默认值，让零配置也有合理行为；
- composition base 是当前 profile/组装给该插件的基础配置；
- user document 是用户真正保存的覆盖层，优先级最高。

三层采用深合并。用户只改 `agent.model` 时，不会把 base 中的 `max_steps` 一并抹掉。`replace({})` 则是刻意重置用户分节，让值重新继承 base 与 defaults。

## 16.2 Settings：注册、读取和安全写入

```python
class Settings:
    def register(self, namespace, *, defaults=None, base=None, validate=None):
        if namespace in self._registrations:
            raise ValueError(f'namespace "{namespace}" 已注册')
        # 保存三层信息，并先验证当前解析值
        ...
        return SettingsScope(self, namespace)

    def get(self, namespace):
        return _freeze(self._resolved(namespace))

    def _resolved(self, namespace):
        registration = self._require(namespace)
        return _merge(
            _merge(registration.defaults, registration.base),
            self._user_section(namespace),
        )
```

注册时会拒绝非法或重复 namespace；已有用户分节若过不了 validator，注册本身也失败。`get()` 返回深度不可变、与内部状态脱离的快照：外部不能改字典，嵌套 list 也被冻结为 tuple。

写入有两种形态：

- `update(patch)`：把 patch 深合并进用户分节；
- `replace(section)`：整体替换用户分节。

每个 namespace 有独立的单调 revision。配置 UI 先读到 revision 4，准备保存时别人已经写到 revision 5，那么带 `expected_revision=4` 的写入会抛出 `SettingsConflictError(code="SETTINGS_CONFLICT")`，而不是覆盖新值。这就是第 13 章 GoalRef 同一种 Compare-and-Swap 思路。

写入前还会做 lossless JSON 校验：拒绝循环引用、非字符串键、NaN/Infinity、负零、超出 JSON 安全范围的整数以及非 JSON 类型。`watch()` 返回幂等 disposer，解析值真正变化时回调收到 `(next, prev)`。

## 16.3 JSON-RPC 2.0：跨进程调用的最小语言

进程 A 调用进程 B 的方法时，进程间传输的是文本。JSON-RPC 2.0 使用请求与响应两类 JSON 消息描述这次调用：

```json
{ "jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"namespace": "agent"} }
{ "jsonrpc": "2.0", "id": 1, "result": {"model": "deepseek-chat"} }
```

- 请求：method 要调什么、params 传什么、id 是本次调用的编号，响应原样带回，异步时对得上号；
- 响应：成功时包含 result，失败时包含 error。调用失败也应返回协议消息，而不是通过服务进程退出表达。

错误有标准错误码：-32700 解析失败、-32600 请求不合法、-32601 方法不存在、-32602 参数不合法。对端拿到数字就能程序化地分类处理，不用解析错误文本。

`RpcError` 还带一个可选的 `request_id`。彻底无法解析、无法确认请求身份时
响应 id 为 null；请求结构和 id 已经有效、只是 params 类型错误时，错误响应
必须原样带回该 id，调用方才能把失败对应到正确请求。

## 16.4 RpcDispatcher：解析、路由、结构化错误

```python
def parse_request(text: str) -> RpcRequest | RpcError:
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
        return RpcError(
            INVALID_PARAMS,
            "Invalid params: 必须是 JSON 对象",
            request_id=data.get("id"),
        )
    return RpcRequest(jsonrpc="2.0", id=data.get("id"), method=method, params=params)
```

本章遵循同一条规则：校验失败时返回结构化错误。RPC 是进程边界，收到的文本可能来自输入错误、异常客户端或恶意构造，解析层需要把这些情况都转换成合法响应。dispatch 的路由部分采用相同处理：

```python
    def dispatch(self, text: str) -> dict[str, Any]:
        parsed = parse_request(text)
        if isinstance(parsed, RpcError):
            return _error_response(parsed.request_id, parsed)
        handler = self._handlers.get(parsed.method)
        if handler is None:
            return _error_response(
                parsed.id, RpcError(METHOD_NOT_FOUND, f"Method not found: {parsed.method}")
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
```

这里区分三类失败：解析失败、方法不存在和处理器内部异常，并使用 INTERNAL_ERROR 处理最后一种情况。所有路径都会得到协议响应。官方网关的 invoke 采用相同原则：每次调用都会校验具名参数、调用公开业务方法，并校验返回结果。

## 16.5 运行完整示例

```bash
uv run python chapters/16-settings-jsonrpc/src/demo.py
```

完整输出，本地确定性运行：

```
=== ① namespace + 三层配置 + revision ===
  解析值: {'model': 'deepseek-chat', 'max_steps': 20}
  ← 默认 model 被用户层覆盖，默认 max_steps 被 base 层覆盖
  更新后: {'model': 'deepseek-v4-max', 'max_steps': 20}，revision=1
  陈旧写入被拒绝: [SETTINGS_CONFLICT] settings namespace "agent" 已变化（期望 revision 0，当前 1）

=== ② JSON-RPC 线格式往返 ===
  → {"jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"namespace": "agent"}}
  ← {"jsonrpc": "2.0", "id": 1, "result": {"model": "deepseek-v4-max", "max_steps": 20}}
  → {"jsonrpc": "2.0", "id": 2, "method": "echo", "params": {"text": "你好"}}
  ← {"jsonrpc": "2.0", "id": 2, "result": "你好"}
  → {"jsonrpc": "2.0", "id": 3, "method": "unknown.tool", "params": {}}
  ← {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found: unknown.tool"}}
  → {"jsonrpc": "2.0", "id": 4, "method": "echo", "params": [1, 2]}
  ← {"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "Invalid params: 必须是 JSON 对象"}}
  → {"id": 5, "method": "echo", "params": {"text": "缺 jsonrpc 版本"}}
  ← {"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Invalid Request: jsonrpc 必须为 2.0"}}
  → 这不是 JSON{{{
  ← {"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "Parse error: 不是合法 JSON"}}
```

六条请求各演示一条路径：正常读取 namespace、正常回声、未知方法、参数类型错误、缺协议版本、彻底不是 JSON。每一条都得到结构化响应，包括最后两条垃圾输入。

## 本章小结

- `Settings`：namespace 注册，以及 defaults < base < user 的三层深合并
- 安全写入：不可变快照、lossless JSON、update/replace、revision 冲突与 watch
- `parse_request`：JSON-RPC 2.0 线格式校验，失败即结构化错误
- `RpcDispatcher`：注册、路由、三层防御
- 标准错误码五件套

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/api/gateway/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/api/gateway/README.zh.md) | `RpcDispatcher` | 官方 Typert endpoint 会按 descriptor 校验具名参数和返回值；教学版用更小的 JSON-RPC 运行时校验说明协议边界 |
| [`packages/settings/settings/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/settings/settings/README.zh.md) | `Settings` | 对齐 namespace、三层解析、深冻结快照、update/replace、revision 冲突和 JSON 校验 |

官方还支持 `mutate` 路径操作、secret 脱敏描述、异步顺序 watcher、可写 provider、文件热重载和卸载排空；教学版不实现这些工程能力。官方用 Typert 而非 JSON-RPC：两端共享方法 descriptor，参数和返回值都由 schema 校验。教学版的手写 JSON-RPC 只是用最小面积讲清线格式、错误表达和信任边界，不与 Typert wire 兼容。

## 练习

1. **优先级验证。** 删除 demo 的用户层 `model`，观察它回落到默认值；再在 base 中加入 `model`，确认 base 覆盖 defaults、user 又覆盖 base。
2. **通知语义。** JSON-RPC 2.0 规定 id 为 null 的请求是通知，无需响应。给 dispatcher 加这个规则：id 为 null 时执行但不返回响应，并讨论它的适用场景，心跳、日志上报。
3. **批量请求。** JSON-RPC 2.0 支持数组形式的批量请求，一次发多条，一次回多条。实现 dispatch_batch，单条失败不影响其他条。
4. **错误即数据。** 把 demo 的六条往返看成协议测试用例，为每一对请求与预期响应写一个断言，体会结构化错误对自动化测试的友好，无需解析文本就能断言错误码。
