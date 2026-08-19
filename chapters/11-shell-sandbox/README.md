# 11｜命令执行与审批

> 预计时间：60 分钟 ｜ 前置：完成第 10 章 ｜ 本章纯本地运行，不调用模型

文件工具只能完成预先定义的读写操作，开发任务还需要运行测试、构建和 Git 命令。Shell 命令的能力范围更大，一条命令既可能删除大量文件，也可能读取不应暴露的数据。因此，执行命令前需要经过两层决策：

1. 模式门：什么模式下能执行什么样的命令，第 10 章沙箱模式的命令版；
2. 审批：模式放行了还不够，真正执行前要过人这一关。官方把这叫 approval，策略分 ask 与 never，问一下与直接拒。

本章实现命令执行器与审批决策链，并如实交代官方与教学版的一处关键差异：官方的 bash-sandbox 用内核级隔离，macOS seatbelt、Linux landlock，把命令的文件写效应挡在系统调用层；教学版不实现内核沙箱，只实现决策层。即使是官方沙箱，限制也只覆盖文件影响，并不是通用安全沙箱。

## 学习目标

完成本章后，你将能够：

- 使用 `subprocess` 捕获命令输出、退出码与超时状态；
- 按模式、审批策略和一次性授权决定是否执行命令；
- 解释 allowed-once 与 fail closed 的安全含义；
- 区分教学版的文本决策层与官方内核级隔离。

## 11.1 原理：执行、超时与审批的三个问题

问题一，如何处理输出。命令的 stdout 与 stderr 既要避免直接占满终端，也要交给模型读取。`capture_output=True` 将两路输出收集到 CommandResult 中，调用方可以按字段处理。

问题二，命令挂死了怎么办。sleep 9999、等输入的交互程序、死循环，Agent 的命令绝不能无限期占住进程。答案：timeout 参数，超时即强制终止，结果里带 timed_out 标记。官方同样有超时机制，tool-call-timeout-policy 在 exec.signal 上设置协作式截止时间，教学版用 subprocess 内置的硬超时。

问题三，谁有权决定放行。模式门挡类别，read-only 模式下写类命令根本没商量；审批管个案，模式放行了的命令，逐条问人。官方把审批做成独立服务 user-approval，结果四值：allowed-once 是唯一放行值，一次性授权，只适用于所请求的那一个动作；其余是 rejected、cancelled、unavailable，没有审批通道时 fail closed，默认拒绝。

## 11.2 run_command：捕获、超时、结构化结果

```python
@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_command(
    command: str,
    cwd: str,
    timeout_seconds: float = 30.0,
    *,
    use_shell: bool = True,
) -> CommandResult:
    try:
        argv = command if use_shell else shlex.split(command)
        completed = subprocess.run(
            argv,
            shell=use_shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            exit_code=-1,
            stdout="",
            stderr=f"命令超时（>{timeout_seconds}s），已强制终止",
            timed_out=True,
        )
```

几个关键参数的用意：

- `use_shell=True` 按 shell 语法解析，管道、重定向、`&&` 都能用；`False` 时先用 `shlex.split` 拆成参数列表，完全绕过 shell 语法。
- `cwd` 决定命令在哪个目录执行。Agent 的命令必须落在工作区内执行，第 10 章的 workspace 概念延续，这是执行上下文的最基本约束。
- `timeout` 是硬终止，不是通知一下，是直接杀掉进程。超时不抛异常让调用方崩溃，而是变成一条正常结果，exit_code 为 −1 加标记。对 Agent 来说，命令超时了是给模型看的事实，不是程序的崩溃。

为什么还要提供 `use_shell=False`？因为文本白名单只能识别第一条命令。假如把 `ls; rm file` 交给 shell，第一词看起来是只读的 `ls`，后半句却会删除文件。因此，本章对 read-only 白名单命令关闭 shell 解析；只有经过更宽模式和审批的命令才使用完整 shell 语法。

## 11.3 决策链：票据、模式门、审批

`ShellPolicy.decide` 按顺序检查三类规则，任一规则得出结论后立即返回：

```python
    def decide(self, command: str) -> tuple[bool, str]:
        # 1) 一次性票据：绕过一切，用后即焚
        if self._granted_once == command:
            self._granted_once = None
            return True, "allowed-once（一次性授权）"

        try:
            words = shlex.split(command)
        except ValueError as error:
            return False, f"[sandbox] 无法解析命令: {error}"
        first_word = words[0] if words else ""

        # 2) 模式门
        if self.mode == "read-only":
            if first_word in READ_ONLY_COMMANDS:
                return True, "read-only 白名单放行"
            return False, f"[sandbox] read-only 模式拒绝写类/未知命令: {command}"

        # 3) 审批
        if self.approval_policy == POLICY_NEVER:
            return False, "[approval] 审批策略为 never，直接拒绝"
        outcome = self.approver(command)
        if outcome == APPROVAL_ALLOWED_ONCE:
            return True, "approved（本轮放行）"
        return False, "[approval] 审批被拒绝"
```

逐条看三个决策。

一次性授权优先。`grant_once(command)` 签发的授权可以绕过模式门与审批，但只对完全相同的一条命令生效，使用一次后立即失效。它对应官方 sandbox_permissions 的升级流程：模型在 read-only 模式请求执行写命令，用户批准后只放行当前动作，后续写命令仍需重新判断。官方词汇表同样使用 allowed-once，而不是 allow-always。

模式检查发生在审批之前。read-only 模式下，白名单只做精确命令名匹配：`grep` 可以，`grep-and-delete` 不可以。放行后还会以 `shell=False` 执行，分号、管道和重定向只会成为普通参数，不能偷偷追加第二条命令。这里的白名单仍然只是教学近似，真实内核沙箱按系统调用产生的实际影响拦截，不依赖命令文本。

审批遵循 fail closed。never 策略直接拒绝；ask 策略调用审批回调，只有 allowed-once 放行，其余三种结果全部拒绝。审批通道不可用时，Agent 停止当前动作，而不是绕过检查继续执行。官方说明，无头或组合不完整的部署会返回 unavailable，并按拒绝处理。

## 11.4 运行完整示例

```bash
uv run python chapters/11-shell-sandbox/src/demo.py
```

完整输出，本地确定性运行：

```
━━━ ① 只读命令：模式门放行 ━━━
  exit=0
  stdout:
precious.txt
  stderr: (空)

━━━ ② read-only 模式拒绝写类命令 ━━━
  stderr: [sandbox] read-only 模式拒绝写类/未知命令: rm -f precious.txt
（命令未执行）
  文件还在吗: True

━━━ ③ 审批流：ask + 模拟用户（workspace-write 模式） ━━━
  [用户拒绝] [approval] 审批被拒绝
（命令未执行）
  [用户批准] exit=0，文件还在吗: False
  [never 策略] [approval] 审批策略为 never，直接拒绝
（命令未执行）

━━━ ④ 一次性授权：grant_once 用后即焚 ━━━
  第 1 次执行（持票据）: exit=0，文件还在吗: False
  第 2 次执行（票据已焚）: [sandbox] read-only 模式拒绝写类/未知命令: rm -f precious.txt
（命令未执行）
  文件还在吗: True
```

四条路径各讲了一个道理：② 模式门按类别挡，只读模式不碰写命令；③ 审批按个案管，同一模式，用户点头与否结局相反；④ 票据的一次性，第一次持票放行，第二次票没了，模式门重新生效。

## 本章小结

- `run_command`：subprocess、cwd、timeout 与可选 shell 解析，超时转结构化结果
- `ShellPolicy.decide`：票据、模式门、审批的三段决策链
- read-only 白名单使用精确命令名和 `shell=False`，拒绝命令拼接绕过
- 审批四结果语义：allowed-once 唯一放行、fail closed
- 实现边界：教学版不包含内核隔离，官方 bash-sandbox 使用 seatbelt 与 landlock

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/shell/bash-sandbox/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/README.zh.md) | `ShellPolicy` | 官方支持 danger-full-access，并明确沙箱只限制文件影响；教学版不实现内核隔离 |
| [`packages/interaction/user-approval/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/interaction/user-approval/README.zh.md) | 审批链 | 官方定义四种审批结果、ask/never 策略和 fail closed；教学版保留同样的决策语义 |
| [`packages/guard/timeout-policy/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy/README.zh.md) | `timeout` | 官方通过 `exec.signal` 协作式通知超时，教学版使用 subprocess 硬超时 |

## 练习

1. **超时实验。** 执行 sleep 3，timeout 设为 1，观察 timed_out 标记与 stderr 文案；把 timeout 提到 10 再试。
2. **管道与退出码。** 执行 ls /不存在 | grep x，观察 exit_code 是管道的哪一段的，shell 默认返回最后一段的退出码；讨论 Agent 读到 exit=0 会怎样误判。
3. **票据绑定。** 签发 grant_once("rm -f a.txt") 后执行 rm -f b.txt，观察票据不匹配；讨论官方为什么要按所请求的那一个动作精确绑定。
4. **审批疲劳。** 设计一个场景，Agent 连续 5 次请求写命令，说明为什么 read-only 白名单要跳过审批；再讨论记住本次会话的批准与 allowed-once 的取舍。
