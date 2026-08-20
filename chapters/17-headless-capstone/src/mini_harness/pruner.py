"""第 09 章：可回放的工具结果 head/middle/tail 剪枝。"""

from __future__ import annotations

from dataclasses import dataclass

from .client import Message
from .meter import estimate_message
from .session import Session, SessionEvent

PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


@dataclass(frozen=True)
class PruneResult:
    replacements: int
    chars_removed: int


class ToolResultPruner:
    def __init__(
        self,
        threshold_chars: int = 8192,
        head_chars: int = 4096,
        tail_chars: int = 1024,
    ) -> None:
        if (
            not isinstance(threshold_chars, int)
            or isinstance(threshold_chars, bool)
            or threshold_chars <= 0
        ):
            raise ValueError("threshold_chars 必须是正整数")
        if (
            not isinstance(head_chars, int)
            or isinstance(head_chars, bool)
            or head_chars < 0
        ):
            raise ValueError("head_chars 必须是非负整数")
        if (
            not isinstance(tail_chars, int)
            or isinstance(tail_chars, bool)
            or tail_chars < 0
        ):
            raise ValueError("tail_chars 必须是非负整数")
        if head_chars + len(PRUNE_MARKER) + tail_chars > threshold_chars:
            raise ValueError("head + marker + tail 不能超过 threshold")
        self.threshold_chars = threshold_chars
        self.head_chars = head_chars
        self.tail_chars = tail_chars

    def prune_content(self, text: str) -> str | None:
        """Python ``str`` 切片按 Unicode code point，不拆代理项对。"""
        if len(text) <= self.threshold_chars:
            return None
        tail = text[-self.tail_chars :] if self.tail_chars else ""
        replacement = text[: self.head_chars] + PRUNE_MARKER + tail
        if len(replacement) > self.threshold_chars or len(replacement) >= len(text):
            raise RuntimeError("tool-result prune replacement 必须更小且不超阈值")
        return replacement

    def prune_session(self, session: Session) -> PruneResult:
        """扫描一个稳定表层快照，追加 replacement，完整原事件不删除。"""
        candidates = [
            event
            for event in _surface_events(session.events)
            if event.type == "tool/result"
        ]
        replacements = 0
        removed = 0
        for event in candidates:
            content = event.data.get("content")
            if not isinstance(content, str):
                continue
            pruned = self.prune_content(content)
            if pruned is None:
                continue
            data = dict(event.data)
            data["content"] = pruned
            data["surface_op"] = {
                "op": "replace",
                "start": event.id,
                "end": event.id,
            }
            data["source_event_seqs"] = [event.id]
            session.append(
                "compaction/prune",
                {
                    "shadowed_range": {"start": event.id, "end": event.id},
                    "shadowed_seqs": [event.id],
                    "shadowed_token_count": estimate_message(
                        Message(
                            role="tool",
                            content=content,
                            tool_call_id=event.data.get("call_id"),
                        )
                    ),
                },
            )
            session.append("tool/result", data)
            replacements += 1
            removed += len(content) - len(pruned)
        return PruneResult(replacements, removed)


def _surface_events(events: tuple[SessionEvent, ...]) -> list[SessionEvent]:
    nodes: list[SessionEvent] = []
    for event in events:
        if event.type not in {"user/message", "assistant/message", "tool/result"}:
            continue
        operation = event.data.get("surface_op")
        if not isinstance(operation, dict) or operation.get("op") != "replace":
            nodes.append(event)
            continue
        start = operation.get("start")
        end = operation.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start > end:
            raise ValueError("surface replacement 范围无效")
        indexes = [
            index
            for index, node in enumerate(nodes)
            if node.id in range(start, end + 1)
        ]
        if not indexes:
            raise ValueError("surface replacement 引用了不存在的节点")
        nodes[indexes[0] : indexes[-1] + 1] = [event]
    return nodes
