# 11｜命令执行：给 Agent 最危险的一只手，配好锁

> 预计时间：60 分钟 ｜ 前置：完成第 10 章 ｜ 本章纯本地运行，不调用模型

文件工具只能读写文件，而真实的开发任务还需要**执行命令**：跑测试、
跑构建、git 操作。第 10 章的文件工具最坏后果是「写错一个文件」；
命令执行的最坏后果是**一切**——`rm -rf` 能删掉整个家目录，一条
恶意命令能读走全部密钥。所以命令执行必须配锁，而且是双层锁：

1. **模式门**：什么模式下能执行什么样的命令（第 10 章沙箱模式的
   命令版）；
2. **审批**：模式放行了还不够，真正执行前要过人这一关——官方把这
   一步叫 approval，策略分 `ask`（问一下）与 `never`（直接拒）。

本章实现命令执行器与审批决策链，并如实交代官方与我们的一处关键
差异：官方的 `bash-sandbox` 用**内核级隔离**（macOS seatbelt /
Linux landlock）把命令的文件写效应挡在系统调用层；教学版不实现
内核沙箱，只实现决策层。官方文档第 85 行把这句边界写得很清楚：
「限制只覆盖文件影响……这些模式不是通用安全沙箱」。

## 11.1 原理：执行、超时与审批的三个问题

**问题一：输出往哪去？** 命令的 stdout/stderr 会刷屏，而且模型
需要读到它们。答案：捕获进结果对象——`capture_output=True` 把
两路输出收进 `CommandResult`，模型拿到的是干净的结构化文本。

**问题二：命令挂死了怎么办？** `sleep 9999`、等输入的交互程序、
死循环——Agent 的命令绝不能无限期占住进程。答案：`timeout` 参数，
超时即强制终止，结果里带 `timed_out` 标记。官方同样有协作式
超时（`tool-call-timeout-policy`），教学版用 subprocess 内置的
硬超时。

**问题三：谁有权决定放行？** 模式门挡「类别」——read-only 模式
下写类命令根本没商量；审批管「个案」——模式放行了的命令，逐条
问人。官方把审批做成独立服务 `user-approval`，结果四值：
`allowed-once`（唯一放行值，一次性）、`rejected`、`cancelled`、
`unavailable`（没有审批通道时 fail closed，默认拒绝）。

## 11.2 run_command：捕获、超时、结构化结果

```python
@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_command(command: str, cwd: str, timeout_seconds: float = 30.0) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            shell=True,
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

三个参数的用意：

- `shell=True`：按 shell 语法解析，管道、重定向、`&&` 都能用——
  开发任务离不开这些。
- `cwd`：命令在**哪个目录**执行。Agent 的命令必须落在工作区内
  执行（第 10 章的 workspace 概念延续），这是执行上下文的最基本
  约束。
- `timeout`：超时是硬终止——不是「通知一下」，是直接杀掉进程。
  值得注意的设计：超时不抛异常让调用方崩溃，而是变成一条正常
  结果（`exit_code=-1` + 标记）——对 Agent 来说，「命令超时了」
  是给模型看的事实，不是程序的崩溃。

## 11.3 决策链：票据 → 模式门 → 审批

`ShellPolicy.decide` 是本章的心脏，三条规则按顺序命中即返回：

```python
    def decide(self, command: str) -> tuple[bool, str]:
        # 1) 一次性票据：绕过一切，用后即焚
        if self._granted_once == command:
            self._granted_once = None
            return True, "allowed-once（一次性授权）"

        first_word = command.strip().split()[0] if command.strip() else ""

        # 2) 模式门
        if self.mode == "read-only":
            if any(first_word.startswith(prefix) for prefix in READ_ONLY_PREFIXES):
                return True, "read-only 白名单放行"
            return False, f"[sandbox] read-only 模式拒绝写类/未知命令: {command}"

        # 3) 审批
        if self.approval_policy == POLICY_NEVER:
            return False, "[approval] 审批策略为 never，直接拒绝"
        outcome = self.approver(command)
        if outcome == APPROVAL_ALLOWED_ONCE:
            return True, "approved（本轮放行）"
        # cancelled / unavailable / rejected → 全部拒绝（fail closed）
        return False, "[approval] 审批被拒绝"
```

逐条看三个决策：

**票据先行。** `grant_once(command)` 签发的票据**绕过模式门与审批**
，但只对**同一条命令**生效、用一次即焚。它的场景正是官方
`sandbox_permissions` 升级流：模型在 read-only 模式请求执行一条
写命令 → 用户审批通过 → 只放行**这一条**，下一条写命令还要再问。
「一次性」三个字是防滑坡的关键——审批放行的不是「模型这个人」，
是「这一次动作」。

**模式门在审批前。** read-only 模式下，只读命令（ls/cat/grep……
教学版用前缀白名单近似）直接放行——无风险的动作不该惊动审批，
审批疲劳会让人对真正的危险也随手点同意；写类命令直接拒绝——
只读模式里没有商量余地，问都不用问。注意白名单是教学近似：真实
内核沙箱按系统调用拦截，不看命令文本（命令文本可以伪装）。

**审批 fail closed。** `never` 策略直接拒绝；`ask` 策略调审批回调，
只有 `allowed-once` 放行，其余三种结果（拒绝/取消/无通道）全部
拒绝。默认状态永远是「不安全」——审批通道出故障时，宁可 Agent
停摆，不能让它裸奔。

## 11.4 跑一遍完整 demo

```bash
uv run python chapters/11-shell-sandbox/src/demo.py
```

完整输出（本地确定性运行）：

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

四条拒绝路径各讲了一个道理：② 模式门按「类别」挡（只读模式
不碰写命令）；③ 审批按「个案」管（同一模式，用户点头与否结局
相反）；④ 票据的「一次性」——第一次持票放行，第二次票没了，
模式门重新生效。

## 11.5 本章小结：亲手写了什么

- `run_command`：subprocess 三件套（shell/cwd/timeout）+ 超时
  转结构化结果
- `ShellPolicy.decide`：票据 → 模式门 → 审批的三段决策链
- 审批四结果语义：allowed-once 唯一放行、fail closed
- 诚实边界：教学版无内核隔离，官方 bash-sandbox 用 seatbelt/
  landlock

## 11.6 对照官方 DSH

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/shell/bash-sandbox/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/shell/bash-sandbox/README.zh.md) | `ShellPolicy` | 官方三模式命令执行（第 15 行 danger-full-access 语义）；官方内核隔离（第 85 行）教学版未实现 |
| [`packages/interaction/user-approval/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/interaction/user-approval/README.zh.md) | 审批链 | 官方四结果（allowed-once 唯一放行）、ask/never 策略、无通道 fail closed 与本章一致 |
| [`packages/guard/timeout-policy/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/guard/timeout-policy/README.zh.md) | `timeout` | 官方超时是协作式（AbortSignal），教学版用 subprocess 硬超时 |

## 11.7 练习

1. **超时实验**：执行 `sleep 3`（timeout=1），观察 `timed_out`
   标记与 stderr 文案；把 timeout 提到 10 再试。
2. **管道与退出码**：执行 `ls /不存在 | grep x`，观察 exit_code
   是管道的哪一段的（提示：shell 默认返回最后一段的退出码）；
   讨论 Agent 读到 exit=0 会怎样误判。
3. **票据绑定**：签发 `grant_once("rm -f a.txt")` 后执行
   `rm -f b.txt`，观察票据不匹配；讨论官方为什么要按「所请求的
   那一个动作」精确绑定。
4. **审批疲劳**：设计一个场景（Agent 连续 5 次请求写命令）说明
   为什么 read-only 白名单要跳过审批；再讨论「记住本次会话的
   批准」与 allowed-once 的取舍。
