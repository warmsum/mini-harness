"""Regression tests for the architecture invariants taught by the chapters."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChapterInvariantTests(unittest.TestCase):
    def run_chapter(self, chapter: str, source: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "chapters" / chapter / "src")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

    def test_session_is_deeply_immutable_and_lossless(self) -> None:
        self.run_chapter(
            "05-session-log",
            """
            from session import Session, SessionEvent

            original = {"nested": {"items": [1, 2]}}
            session = Session()
            event = session.append("test/event", original)
            original["nested"]["items"].append(3)
            assert event.data["nested"]["items"] == (1, 2)
            assert isinstance(session.events, tuple)

            for mutate in (
                lambda: event.data.__setitem__("x", 1),
                lambda: event.data["nested"].__setitem__("x", 1),
            ):
                try:
                    mutate()
                except TypeError:
                    pass
                else:
                    raise AssertionError("frozen event data accepted a mutation")

            for invalid in ({"n": float("nan")}, {"n": -0.0}):
                try:
                    session.append("bad", invalid)
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid JSON number was accepted")

            cyclic = []
            cyclic.append(cyclic)
            try:
                session.append("bad", {"cycle": cyclic})
            except ValueError:
                pass
            else:
                raise AssertionError("list cycle was accepted")

            replay_input = {"value": [1]}
            replayed = Session.from_log([SessionEvent(0, "test/event", 1.0, replay_input)])
            replay_input["value"].append(2)
            assert replayed.events[0].data["value"] == (1,)
            """,
        )

    def test_plugin_cleanup_duplicate_service_and_bound_context(self) -> None:
        self.run_chapter(
            "03-python-cordis",
            """
            from context import Context

            ctx = Context()
            seen = []
            def broken(plugin_ctx, _config):
                plugin_ctx.on("ping", lambda: seen.append("ghost"))
                raise RuntimeError("install failed")
            try:
                ctx.plugin(broken)
            except RuntimeError:
                pass
            ctx.emit("ping")
            assert seen == []

            remove = ctx.on("ping", lambda: None)
            remove()
            remove()
            """,
        )
        self.run_chapter(
            "04-services-scopes",
            """
            from context import Context

            ctx = Context()
            seen = []
            def provider(plugin_ctx, _config):
                plugin_ctx.provide("value", 42)
            def consumer(plugin_ctx, _config):
                seen.append(plugin_ctx.value)
                plugin_ctx.on("check", lambda: seen.append(plugin_ctx.value))
            consumer.inject = ["value"]

            provider_handle = ctx.plugin(provider)
            consumer_handle = ctx.plugin(consumer)
            ctx.emit("check")
            assert seen == [42, 42]
            try:
                ctx.plugin(provider)
            except ValueError:
                pass
            else:
                raise AssertionError("duplicate service was accepted")
            provider_handle.dispose()
            assert consumer_handle.state == "pending"
            ctx.emit("check")
            assert seen == [42, 42]
            """,
        )

    def test_prompt_registry_and_inbox_ordering(self) -> None:
        self.run_chapter(
            "07-agent-inbox",
            """
            from client import Message, Tool
            from inbox import Inbox
            from prompt import PromptAssembler
            from registry import ToolRegistry

            prompt = PromptAssembler()
            prompt.section("first", "A={{value}}", order=10)
            prompt.section("second", "B", order=10)
            prompt.variable("value", lambda: "1")
            assert prompt.render() == "A=1\\n\\nB"
            try:
                prompt.section("first", "duplicate")
            except ValueError:
                pass
            else:
                raise AssertionError("duplicate section was accepted")
            missing = PromptAssembler()
            missing.section("x", "{{unknown}}")
            try:
                missing.render()
            except KeyError:
                pass
            else:
                raise AssertionError("unknown variable passed through")

            registry = ToolRegistry()
            registry.register(Tool("zeta", "z", {}, lambda _: "z"))
            registry.register(Tool("alpha", "a", {}, lambda _: "a"))
            assert [tool.name for tool in registry.all()] == ["alpha", "zeta"]

            inbox = Inbox()
            inbox.followup(Message(role="user", content="turn-1"))
            inbox.followup(Message(role="user", content="turn-2"))
            inbox.steer(Message(role="user", content="steer-1"))
            inbox.steer(Message(role="user", content="steer-2"))
            assert [m.content for m in inbox.claim_turn()] == [
                "steer-1", "steer-2", "turn-1"
            ]
            assert [m.content for m in inbox.claim_turn()] == ["turn-2"]
            """,
        )

    def test_agent_records_steps_and_continues_for_late_steer(self) -> None:
        self.run_chapter(
            "07-agent-inbox",
            """
            from agent import Agent
            from client import Message
            from prompt import PromptAssembler
            from registry import ToolRegistry

            class FakeClient:
                MODEL = "fake"
                def __init__(self):
                    self.calls = 0
                    self.agent = None
                def chat(self, messages, tools):
                    self.calls += 1
                    if self.calls == 1:
                        self.agent.steer("late correction")
                    return Message(role="assistant", content=f"answer-{self.calls}")

            client = FakeClient()
            prompt = PromptAssembler()
            prompt.section("persona", "test")
            agent = Agent(client, ToolRegistry(), prompt)
            client.agent = agent
            agent.followup("initial")
            session = agent.run()
            assert client.calls == 2
            types = [event.type for event in session.events]
            assert types.count("step/start") == 2
            assert types.count("step/end") == 2
            assert types.count("request/header") == 1
            assert [m.content for m in session.derive_messages() if m.role == "user"] == [
                "initial", "late correction"
            ]
            """,
        )

    def test_jsonl_is_append_only_and_repairs_only_torn_tail(self) -> None:
        self.run_chapter(
            "08-persistence",
            """
            import tempfile
            from pathlib import Path
            from persistence import JsonlStore
            from session import Session

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "session.jsonl"
                store = JsonlStore(path)
                session = Session()
                session.append("turn/start", {"turn": 3})
                store.save(session)
                prefix = path.read_bytes()
                session.append("step/start", {"turn": 3, "step": 1})
                store.save(session)
                assert path.read_bytes().startswith(prefix)

                with path.open("ab") as file:
                    file.write(b'{"id": 2')
                recovered = store.load()
                assert [e.type for e in recovered.events][-2:] == ["step/end", "turn/end"]
                assert recovered.events[-1].data["turn"] == 3
                assert path.read_bytes().endswith(b"\\n")

                broken = Path(tmp) / "broken.jsonl"
                broken.write_text(
                    '{"format":"mini-harness-jsonl","version":1}\\n'
                    '{not-json}\\n'
                    '{"id":0,"type":"turn/start","ts":1,"data":{"turn":1}}\\n'
                )
                before = broken.read_bytes()
                try:
                    JsonlStore(broken).load()
                except ValueError:
                    pass
                else:
                    raise AssertionError("middle corruption was silently truncated")
                assert broken.read_bytes() == before
            """,
        )

    def test_read_only_filesystem_and_settings_revision(self) -> None:
        self.run_chapter(
            "10-filesystem",
            """
            import tempfile
            from pathlib import Path
            from sandbox import READ_ONLY, SandboxDeniedError, SandboxPolicy

            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "file.txt"
                try:
                    SandboxPolicy(READ_ONLY, Path(tmp)).fence_write(target)
                except SandboxDeniedError:
                    pass
                else:
                    raise AssertionError("read-only allowed a workspace write")
            """,
        )
        self.run_chapter(
            "16-settings-jsonrpc",
            """
            from settings import Settings, SettingsConflictError

            settings = Settings({"agent": {"model": "user"}})
            scope = settings.register(
                "agent", defaults={"model": "default", "steps": 10}, base={"steps": 20}
            )
            assert dict(scope.get()) == {"model": "user", "steps": 20}
            scope.update({"model": "next"}, expected_revision=0)
            try:
                scope.update({"steps": 30}, expected_revision=0)
            except SettingsConflictError:
                pass
            else:
                raise AssertionError("stale settings write was accepted")
            try:
                scope.get()["model"] = "mutated"
            except TypeError:
                pass
            else:
                raise AssertionError("settings snapshot was mutable")
            """,
        )


if __name__ == "__main__":
    unittest.main()
