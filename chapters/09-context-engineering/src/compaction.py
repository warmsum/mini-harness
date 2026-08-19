"""第 09 章：上下文压缩 —— 长会话的护身符。

对应官方 packages/compaction/compaction-basic（官方最精妙的设计之一）。
教学版保留完整策略骨架：
1. should_compact —— 压力 >= floor(contextWindow × 0.8)
2. summarize    —— 重放「system + 被压缩区」给同一个 LLM + 官方压缩指令
3. replace      —— 用 <compacted-summary> 替换被压缩区（替换而非追加！）
4. 失败处理      —— 摘要不缩小 → 重试 → 仍不行 → 保留原文继续
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from client import DeepSeekClient, Message
from meter import TokenMeter, estimate_message

# 压缩指令：逐字复刻官方 COMPACTION_INSTRUCTION
# （packages/compaction/compaction-basic/README.zh.md 中的英文原文）
COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""

# checkpoint 开场白：逐字复刻官方 CHECKPOINT_PREAMBLE
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established "
    "background and build on it without restating it. Continue the task directly from "
    "the messages that follow, without acknowledging this checkpoint."
)

SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"


@dataclass(frozen=True)
class CompactResult:
    messages: list[Message]
    ok: bool
    shadowed_count: int
    shadowed_tokens: int
    checkpoint_tokens: int
    attempts: int
    reason: str


def should_compact(
    meter: TokenMeter,
    messages: list[Message],
    tools: list[Any] | None = None,
) -> bool:
    """压力是否达到阈值（官方 thresholdRatio 默认 0.8）。"""
    pressure = meter.pressure(meter.measure(messages, tools))
    return pressure.over_threshold


def select_shadowed_start(
    meter: TokenMeter, messages: list[Message], retain_ratio: float = 0.16
) -> int:
    """从尾部向前累积 token，凑够保留预算（默认 16% contextWindow），
    之前的消息是被压缩区。返回被压缩区结束的下标（即保留区起点）。
    返回 -1 表示无可压缩区（整个历史都在保留预算内）。"""
    retain_tokens = int(meter.context_window * retain_ratio)
    accumulated = 0
    tail_start = len(messages)
    # 下标 0 是 system（envelope的一部分），不参与压缩
    for index in range(len(messages) - 1, 0, -1):
        accumulated += estimate_message(messages[index])
        tail_start = index
        if accumulated >= retain_tokens:
            break
    return tail_start if tail_start > 1 else -1


def build_summary_prompt(messages: list[Message], tail_start: int) -> list[Message]:
    """构造压缩调用的输入：system 原文 + 被压缩区原文（逐字重放），
    末尾追加压缩指令作为最后一条 user 消息。

    KV cache 复用的来源：这个重放与「下一次真实请求」的前缀逐字节
    一致——provider 的前缀缓存直接命中，只有末尾的指令和摘要输出
    是未缓存的。"""
    return [
        messages[0],  # system：原样重放
        *messages[1:tail_start],  # 被压缩区逐字重放
        Message(role="user", content=COMPACTION_INSTRUCTION),
    ]


def build_checkpoint_message(summary: str) -> Message:
    """把摘要包装成 checkpoint 消息：preamble + <compacted-summary> 标签。"""
    return Message(
        role="user",
        content=(
            f"{CHECKPOINT_PREAMBLE}\n\n"
            f"{SUMMARY_OPEN_TAG}\n{summary}\n{SUMMARY_CLOSE_TAG}"
        ),
    )


def replace(
    messages: list[Message], tail_start: int, checkpoint: Message
) -> list[Message]:
    """用 checkpoint 替换被压缩区 [1..tail_start)。
    替换而非追加——追加只会让未来的输入更长，替换才真正缩短上下文
    （官方 README.zh.md：替换减少未来输入历史而非追加第二份）。"""
    return [messages[0], checkpoint, *messages[tail_start:]]


def compact(
    client: DeepSeekClient,
    meter: TokenMeter,
    messages: list[Message],
    threshold_ratio: float = 0.8,
    retain_ratio: float = 0.16,
    retries: int = 1,
) -> CompactResult:
    """压缩主流程（对应官方 compactIfNeeded）：
    压力超阈值 → 选范围 → 压缩调用 → 缩小校验 → 替换 → 复测；
    失败路径：摘要不缩小拒绝重试，重试用尽保留原文继续。"""
    threshold_tokens = int(meter.context_window * threshold_ratio)
    current = messages
    last_good: CompactResult | None = None
    attempts = 0

    for attempt in range(retries + 1):
        if meter.measure(current).total_tokens < threshold_tokens:
            break  # 压力已回安全区（可能是上一轮替换的效果）

        tail_start = select_shadowed_start(meter, current, retain_ratio)
        if tail_start < 0:
            break  # 无可压缩区

        shadowed = current[1:tail_start]
        shadowed_tokens = sum(estimate_message(m) for m in shadowed)

        # 压缩调用：同一个 LLM，重放 + 官方指令（无 tools，纯文本总结）
        prompt = build_summary_prompt(current, tail_start)
        summary = client.chat(prompt)
        attempts += 1
        summary_text = summary.content or ""

        checkpoint = build_checkpoint_message(summary_text)
        checkpoint_tokens = estimate_message(checkpoint)

        # 缩小校验：checkpoint 不小于被压缩区 → 拒绝本次摘要
        if checkpoint_tokens >= shadowed_tokens:
            print(
                f"    ↳ 第 {attempt + 1} 次摘要未缩小"
                f"（{checkpoint_tokens} >= {shadowed_tokens} token），拒绝"
            )
            continue

        current = replace(current, tail_start, checkpoint)
        last_good = CompactResult(
            messages=current,
            ok=True,
            shadowed_count=len(shadowed),
            shadowed_tokens=shadowed_tokens,
            checkpoint_tokens=checkpoint_tokens,
            attempts=attempts,
            reason="",
        )
        if meter.measure(current).total_tokens < threshold_tokens:
            return CompactResult(
                messages=current,
                ok=True,
                shadowed_count=last_good.shadowed_count,
                shadowed_tokens=last_good.shadowed_tokens,
                checkpoint_tokens=last_good.checkpoint_tokens,
                attempts=attempts,
                reason="压力降到阈值以下，压缩收敛",
            )

    if last_good is not None:
        return CompactResult(
            messages=last_good.messages,
            ok=True,
            shadowed_count=last_good.shadowed_count,
            shadowed_tokens=last_good.shadowed_tokens,
            checkpoint_tokens=last_good.checkpoint_tokens,
            attempts=last_good.attempts,
            reason="替换后仍超阈值，保留已替换结果继续",
        )
    return CompactResult(
        messages=current,
        ok=False,
        shadowed_count=0,
        shadowed_tokens=0,
        checkpoint_tokens=0,
        attempts=attempts,
        reason="摘要未缩小或无可压缩区，保留原文继续",
    )
