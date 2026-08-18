# 16｜配置与 RPC

> 预计时间：55 分钟 ｜ 前置：完成第 07 章 ｜ 本章纯本地运行，不调用模型

一个完整的 Agent 框架不能只活在 python demo.py 里。真实的使用方式是：IDE
插件、命令行工具、网页前端从进程外驱动 Agent，发消息、查状态、取结果。
这带来本章的两个主题：

1. 配置：这些外部入口和 Agent 本体怎么共享同一套配置，不互相矛盾？
2. RPC：进程之间的调用语言长什么样，错误怎么表达？

官方把第二个主题做成了 Typert RPC 网关，api/gateway 文档第 5 行写明：为
Host 与 Client 两侧的 Cordis 环境提供 Typert RPC endpoint。教学版实现它的
协议近亲 JSON-RPC 2.0，一个只有几页规范的极简标准，足够讲清跨进程调用的
全部核心问题。

## 16.1 原理：配置为什么必须分层

配置散落三处时，经典混乱是这样的：代码里写死模型名，.env 里又一个模型
名，命令行再传一个，同一次运行，三个地方给出三个答案。分层的解法是给
每个来源一个明确的优先级：

```
显式覆盖（程序调用方传入）  >  .env 文件  >  默认值（代码内置）
```

- 默认值兜底：新用户零配置就能跑，默认值要选大多数情况下正确的那个；
- .env 覆盖：本地个性化，API Key 属于这一类，文件被 gitignore，不进
  版本库；
- 显式覆盖：调用方，比如 RPC 请求，带着明确意图传入，最高优先。

读配置永远按这个顺序逐级回落，任何时刻这个配置项的值是什么只有一个
答案。官方 settings 服务同样是分层解析：schema 默认值，然后组合的 base，
最后用户文档分节。

## 16.2 Settings：三层逐级回落

```python
class Settings:
    def __init__(self, env_path=None, defaults=None, overrides=None):
        self._defaults = dict(defaults or {})
        self._env: dict[str, str] = {}
        self._overrides = dict(overrides or {})
        if env_path is not None and env_path.exists():
            self._load_env(env_path)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._overrides:
            return self._overrides[key]
        if key in self._env:
            return self._env[key]
        if key in self._defaults:
            return self._defaults[key]
        return default
```

三层结构各存各的，`get` 按优先级逐级回落，一眼能看懂，也一眼能验证。
`_load_env` 的解析与第 01 章 load_api_key 同一套规则，跳过注释与空行、
剥引号。

## 16.3 JSON-RPC 2.0：跨进程调用的最小语言

进程 A 想调用进程 B 的一个方法，线上只传文本。JSON-RPC 2.0 把这个对话
规定成两种消息：

```json
{ "jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"key": "MODEL"} }
{ "jsonrpc": "2.0", "id": 1, "result": "deepseek-chat" }
```

- 请求：method 要调什么、params 传什么、id 是本次调用的编号，响应
  原样带回，异步时对得上号；
- 响应：要么 result 成功，要么 error 失败。错误也是响应，绝不靠对端
  崩了来表达失败。

错误有标准错误码：-32700 解析失败、-32600 请求不合法、-32601 方法不
存在、-32602 参数不合法。对端拿到数字就能程序化地分类处理，不用解析
错误文本。

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
        return RpcError(INVALID_PARAMS, "Invalid params: 必须是 JSON 对象")
    return RpcRequest(jsonrpc="2.0", id=data.get("id"), method=method, params=params)
```

一个设计贯穿全章：所有校验失败都返回结构化错误，而不是抛异常让对端
崩溃。RPC 的边界就是信任边界，对端发来的任何文本都可能畸形，手滑、坏
程序、恶意构造，解析层必须把它们全部转成合法的错误响应。dispatch 的
路由部分同理：

```python
    def dispatch(self, text: str) -> dict[str, Any]:
        parsed = parse_request(text)
        if isinstance(parsed, RpcError):
            return _error_response(None, parsed)
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

三层防御：解析失败是对端的问题，方法不存在是调用方的问题，处理器抛错
是这里的问题，INTERNAL_ERROR 兜底。任何路径都有响应。官方网关 invoke
是同一套思想，api/gateway 文档第 9 行写明：每次调用都校验具名参数是否
完全匹配，调用公开的业务方法，并校验其结果。把边界变成全响应。

## 16.5 跑一遍完整 demo

```bash
uv run python chapters/16-settings-jsonrpc/src/demo.py
```

完整输出，本地确定性运行：

```
=== ① 分层配置 ===
  MODEL（.env 覆盖默认）: deepseek-chat
  MAX_TURNS（只有默认值）: 10
  DEEPSEEK_API_KEY（只有 .env）: sk-local-test
  MODEL（显式覆盖后）: deepseek-v4-max

=== ② JSON-RPC 线格式往返 ===
  → {"jsonrpc": "2.0", "id": 1, "method": "settings.get", "params": {"key": "MODEL"}}
  ← {"jsonrpc": "2.0", "id": 1, "result": "deepseek-v4-max"}
  → {"jsonrpc": "2.0", "id": 2, "method": "echo", "params": {"text": "你好"}}
  ← {"jsonrpc": "2.0", "id": 2, "result": "你好"}
  → {"jsonrpc": "2.0", "id": 3, "method": "unknown.tool", "params": {}}
  ← {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found: unknown.tool"}}
  → {"jsonrpc": "2.0", "id": 4, "method": "echo", "params": [1, 2]}
  ← {"jsonrpc": "2.0", "id": null, "error": {"code": -32602, "message": "Invalid params: 必须是 JSON 对象"}}
  → {"id": 5, "method": "echo", "params": {"text": "缺 jsonrpc 版本"}}
  ← {"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Invalid Request: jsonrpc 必须为 2.0"}}
  → 这不是 JSON{{{
  ← {"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "Parse error: 不是合法 JSON"}}
```

六条请求各演示一条路径：正常取值、正常回声、未知方法、参数类型错误、缺
协议版本、彻底不是 JSON。每一条都得到结构化响应，包括最后两条垃圾输入。

## 本章小结

- `Settings`：显式覆盖、.env、默认值的三层回落
- `parse_request`：JSON-RPC 2.0 线格式校验，失败即结构化错误
- `RpcDispatcher`：注册、路由、三层防御
- 标准错误码五件套

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/api/gateway/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/api/gateway/README.zh.md) | `RpcDispatcher` | 官方 Typert RPC endpoint 在第 5 行；invoke 校验参数、调用业务方法并校验结果在第 9 行，思想与本章一致，协议换成更简单的 JSON-RPC |
| [`packages/settings/settings/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/settings/settings/README.zh.md) | `Settings` | 官方分层解析在默认值、base、用户文档三层（第 5 行）；官方还有 schema 校验与 describe 脱敏机密值（第 12 行），教学版只做三层回落 |

官方用 Typert 而非 JSON-RPC 的动机：方法签名由 TypeScript 类型生成，两端
共享同一份描述符，参数校验在编译期就锁死，比运行时手写校验更不容易出错。
教学版用 JSON-RPC 是为了把跨进程调用的核心问题，线格式、错误表达、信任
边界，用最小面积讲清。

## 练习

1. **优先级验证。** 把 demo 里的显式覆盖删掉，重跑，观察 MODEL 回落到
   .env 值；再删掉 .env 里的 MODEL，观察回落到默认值。
2. **通知语义。** JSON-RPC 2.0 规定 id 为 null 的请求是通知，无需响应。
   给 dispatcher 加这个规则：id 为 null 时执行但不返回响应，并讨论它
   的适用场景，心跳、日志上报。
3. **批量请求。** JSON-RPC 2.0 支持数组形式的批量请求，一次发多条，
   一次回多条。实现 dispatch_batch，单条失败不影响其他条。
4. **错误即数据。** 把 demo 的六条往返看成协议测试用例，为每一对请求
   与预期响应写一个断言，体会结构化错误对自动化测试的友好，无需解析
   文本就能断言错误码。
