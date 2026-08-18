# 11｜命令执行与审批

> 预计时间：60 分钟 ｜ 前置：完成第 10 章 ｜ 本章纯本地运行，不调用模型

文件工具只能读写文件，而真实的开发任务还需要执行命令：跑测试、跑构建、
git 操作。第 10 章的文件工具最坏后果是写错一个文件；命令执行的最坏后果是
一切，rm -rf 能删掉整个家目录，一条恶意命令能读走全部密钥。所以命令执行
必须配锁，而且是双层锁：

1. 模式门：什么模式下能执行什么样的命令，第 10 章沙箱模式的命令版；
2. 审批：模式放行了还不够，真正执行前要过人这一关。官方把这叫
   approval，策略分 ask 与 never，问一下与直接拒。

本章实现命令执行器与审批决策链，并如实交代官方与教学版的一处关键差异：
官方的 bash-sandbox 用内核级隔离，macOS seatbelt、Linux landlock，把命令
的文件写效应挡在系统调用层；教学版不实现内核沙箱，只实现决策层。官方
文档第 85 行把这句边界写得很清楚：限制只覆盖文件影响，这些模式不是通用
安全沙箱。

## 11.1 原理：执行、超时与审批的三个问题

问题一，输出往哪去。命令的 stdout 与 stderr 会刷屏，而且模型需要读到
它们。答案：捕获进结果对象，`capture_output=True` 把两路输出收进
CommandResult，模型拿到的是干净的结构化文本。

问题二，命令挂死了怎么办。sleep 9999、等输入的交互程序、死循环，Agent
的命令绝不能无限期占住进程。答案：timeout 参数，超时即强制终止，结果里
带 timed_out 标记。官方同样有超时机制，tool-call-timeout-policy 在
exec.signal 上设置协作式截止时间，教学版用 subprocess 内置的硬超时。

问题三，谁有权决定放行。模式门挡类别，read-only 模式下写类命令根本没
商量；审批管个案，模式放行了的命令，逐条问人。官方把审批做成独立服务
user-approval，结果四值：allowed-once 是唯一放行值，一次性授权，只适用
于所请求的那一个动作；其余是 rejected、cancelled、unavailable，没有审批
通道时 fail closed，默认拒绝。

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

- `shell=True` 按 shell 语法解析，管道、重定向、&& 都能用，开发任务
  离不开这些。
- `cwd` 决定命令在哪个目录执行。Agent 的命令必须落在工作区内执行，
  第 10 章的 workspace 概念延续，这是执行上下文的最基本约束。
- `timeout` 是硬终止，不是通知一下，是直接杀掉进程。超时不抛异常让
  调用方崩溃，而是变成一条正常结果，exit_code 为 −1 加标记。对 Agent
  来说，命令超时了是给模型看的事实，不是程序的崩溃。

## 11.3 决策链：票据、模式门、审批

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
        return False, "[approval] 审批被拒绝"
```

逐条看三个决策。

票据先行。`grant_once(command)` 签发的票据绕过模式门与审批，但只对同一条
命令生效，用一次即焚。它的场景正是官方 sandbox_permissions 升级流：模型
在 read-only 模式请求执行一条写命令，用户审批通过，只放行这一条，下一条
写命令还要再问。一次性三个字是防滑坡的关键，审批放行的不是模型这个人，
是这一次动作。官方词汇表里同样只有 allowed-once，没有 allow-always。

模式门在审批前。read-only 模式下，只读命令直接放行，无风险的动作不该
惊动审批，审批疲劳会让人对真正的危险也随手点同意；写类命令直接拒绝，
只读模式里没有商量余地，问都不用问。白名单是教学近似，真实内核沙箱按
系统调用拦截，不看命令文本，命令文本可以伪装。

审批 fail closed。never 策略直接拒绝；ask 策略调审批回调，只有
allowed-once 放行，其余三种结果全部拒绝。默认状态永远是不安全，审批
通道出故障时，宁可 Agent 停摆，不能让它裸奔。官方写明服务自身绝不会
提示人类，无头或组合不完整的部署返回 unavailable 并以拒绝方式关闭。

## 11.4 跑一遍完整 demo

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

四条路径各讲了一个道理：② 模式门按类别挡，只读模式不碰写命令；③ 审批
按个案管，同一模式，用户点头与否结局相反；④ 票据的一次性，第一次持票
放行，第二次票没了，模式门重新生效。

## 本章小结

- `run_command`：subprocess 三件套，shell、cwd、timeout，超时转结构化结果
- `ShellPolicy.decide`：票据、模式门、审批的三段决策链
- 审批四结果语义：allowed-once 唯一放行、fail closed
- 诚实边界：教学版无内核隔离，官方 bash-sandbox 用 seatbelt 与 landlock

## 对照官方

| 官方实现 | 我们对应实现 | 说明 |
|----------|--------------|------|
| [`packages/shell/bash-sandbox/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/shell/bash-sandbox/README.zh.md) | `ShellPolicy` | danger-full-access 不作限制在第 15 行；只限制文件影响、不是通用安全沙箱在第 85 行；教学版不实现内核隔离 |
| [`packages/interaction/user-approval/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/interaction/user-approval/README.zh.md) | 审批链 | 四结果与一次性授权在第 5 行；ask 与 never 策略在第 11 行；无内置应答者 fail closed 在第 62 行 |
| [`packages/guard/timeout-policy/README.zh.md`](https://github.com/deepseek-ai/DeepSeek-Harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/guard/timeout-policy/README.zh.md) | `timeout` | 官方超时是协作式，只在 exec.signal 上通知（第 5、32 行），教学版用 subprocess 硬超时 |

## 练习

1. **超时实验。** 执行 sleep 3，timeout 设为 1，观察 timed_out 标记与
   stderr 文案；把 timeout 提到 10 再试。
2. **管道与退出码。** 执行 ls /不存在 | grep x，观察 exit_code 是管道的
   哪一段的，shell 默认返回最后一段的退出码；讨论 Agent 读到 exit=0
   会怎样误判。
3. **票据绑定。** 签发 grant_once("rm -f a.txt") 后执行 rm -f b.txt，
   观察票据不匹配；讨论官方为什么要按所请求的那一个动作精确绑定。
4. **审批疲劳。** 设计一个场景，Agent 连续 5 次请求写命令，说明为什么
   read-only 白名单要跳过审批；再讨论记住本次会话的批准与 allowed-once
   的取舍。
