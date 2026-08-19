"""Regression tests for the architecture invariants taught by the chapters."""

from __future__ import annotations

import os
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

    def test_dotenv_loading_and_environment_precedence(self) -> None:
        self.run_chapter(
            "01-streaming-agent",
            """
            import os
            import tempfile
            from pathlib import Path
            import client

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fake_source = root / "chapters" / "01-streaming-agent" / "src" / "client.py"
                (root / ".env").write_text(
                    'export DEEPSEEK_API_KEY="from-dotenv"  # comment\\n',
                    encoding="utf-8",
                )
                client.__file__ = str(fake_source)
                os.environ.pop("DEEPSEEK_API_KEY", None)
                assert client.load_api_key() == "from-dotenv"
                os.environ["DEEPSEEK_API_KEY"] = "from-environment"
                assert client.load_api_key() == "from-environment"
            """,
        )
        self.run_chapter(
            "17-headless-capstone",
            """
            import os
            import tempfile
            from pathlib import Path
            from mini_harness.client import load_api_key

            original_cwd = Path.cwd()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                nested = root / "project" / "nested"
                nested.mkdir(parents=True)
                (root / "project" / ".env").write_text(
                    "DEEPSEEK_API_KEY=from-parent-dotenv\\n",
                    encoding="utf-8",
                )
                os.environ.pop("DEEPSEEK_API_KEY", None)
                os.chdir(nested)
                try:
                    assert load_api_key() == "from-parent-dotenv"
                finally:
                    os.chdir(original_cwd)
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

    def test_reasoning_content_is_preserved_and_replayed(self) -> None:
        self.run_chapter(
            "05-session-log",
            """
            from client import DeepSeekClient
            from session import Session

            session = Session()
            session.append(
                "assistant/message",
                {
                    "content": None,
                    "reasoning_content": "private reasoning",
                    "tool_calls": [],
                },
            )
            messages = session.derive_messages()
            assert len(messages) == 1
            assert messages[0].reasoning_content == "private reasoning"
            assert DeepSeekClient._wire_message(messages[0]) == {
                "role": "assistant",
                "content": "",
                "reasoning_content": "private reasoning",
            }
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

    def test_prompt_is_reassembled_for_each_step(self) -> None:
        self.run_chapter(
            "06-prompt-tools",
            """
            from agent import run_agent
            from client import Message, Tool, ToolCall
            from prompt import PromptAssembler
            from registry import ToolRegistry

            class FakeClient:
                MODEL = "fake"
                def __init__(self):
                    self.calls = 0
                def chat(self, messages, tools):
                    self.calls += 1
                    if self.calls == 1:
                        return Message(
                            role="assistant",
                            tool_calls=(ToolCall("call-1", "noop", "{}"),),
                        )
                    return Message(role="assistant", content="done")

            renders = 0
            def dynamic_value():
                global renders
                renders += 1
                return str(renders)

            assembler = PromptAssembler()
            assembler.section("dynamic", "value={{value}}")
            assembler.variable("value", dynamic_value)
            registry = ToolRegistry()
            registry.register(Tool("noop", "noop", {"type": "object"}, lambda _: "ok"))
            session = run_agent(FakeClient(), registry, assembler, "start")
            headers = [event for event in session.events if event.type == "request/header"]
            assert [event.data["reason"] for event in headers] == ["initial", "change"]
            assert [event.data["header"]["system"] for event in headers] == [
                "value=1", "value=2"
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

                clean_open = Path(tmp) / "clean-open.jsonl"
                clean_store = JsonlStore(clean_open)
                clean_session = Session()
                clean_session.append("turn/start", {"turn": 7})
                clean_session.append("step/start", {"turn": 7, "step": 2})
                clean_store.save(clean_session)
                persisted_before_load = clean_open.read_bytes()
                recovered_clean = clean_store.load()
                assert [e.type for e in recovered_clean.events][-2:] == [
                    "step/end", "turn/end"
                ]
                assert recovered_clean.events[-1].data["turn"] == 7
                assert clean_open.read_bytes() == persisted_before_load
                clean_store.save(recovered_clean)
                assert clean_open.read_bytes() != persisted_before_load
            """,
        )

    def test_skill_metadata_and_policy_modes_are_validated(self) -> None:
        self.run_chapter(
            "12-instructions-skills",
            """
            import tempfile
            from pathlib import Path
            from skills import SkillCatalog

            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)

                invalid_root = base / "invalid-root"
                skill = invalid_root / "Bad_Name"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "---\\nname: Bad_Name\\ndescription: bad\\n---\\nbody\\n",
                    encoding="utf-8",
                )
                try:
                    SkillCatalog(invalid_root).list()
                except ValueError:
                    pass
                else:
                    raise AssertionError("non-kebab-case directory was accepted")

                unclosed_root = base / "unclosed-root"
                skill = unclosed_root / "valid-name"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "---\\nname: valid-name\\ndescription: missing close\\nbody\\n",
                    encoding="utf-8",
                )
                try:
                    SkillCatalog(unclosed_root).list()
                except ValueError:
                    pass
                else:
                    raise AssertionError("unclosed frontmatter was accepted")

                no_open_root = base / "no-open-root"
                skill = no_open_root / "valid-name"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "name: valid-name\\ndescription: missing open\\n---\\nbody\\n",
                    encoding="utf-8",
                )
                try:
                    SkillCatalog(no_open_root).load("valid-name")
                except ValueError:
                    pass
                else:
                    raise AssertionError("frontmatter without opening marker was accepted")
            """,
        )
        self.run_chapter(
            "10-filesystem",
            """
            from sandbox import SandboxPolicy
            try:
                SandboxPolicy(mode="unknown")
            except ValueError:
                pass
            else:
                raise AssertionError("unknown filesystem mode was accepted")
            """,
        )
        self.run_chapter(
            "11-shell-sandbox",
            """
            from shell import ShellPolicy
            for kwargs in (
                {"mode": "unknown"},
                {"approval_policy": "sometimes"},
            ):
                try:
                    ShellPolicy(**kwargs)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"invalid policy was accepted: {kwargs}")
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

    def test_rpc_error_preserves_request_id(self) -> None:
        self.run_chapter(
            "16-settings-jsonrpc",
            """
            from rpc import INVALID_PARAMS, RpcDispatcher

            response = RpcDispatcher().dispatch(
                '{"jsonrpc":"2.0","id":4,"method":"echo","params":[1,2]}'
            )
            assert response["id"] == 4
            assert response["error"]["code"] == INVALID_PARAMS
            """,
        )

    def test_subagent_diagnostics_and_multi_query_search(self) -> None:
        self.run_chapter(
            "14-subagents-workflow",
            """
            from subagent import run_subagent

            class FailingClient:
                def chat(self, messages):
                    raise RuntimeError("provider unavailable")

            result = run_subagent(FailingClient(), "task", "system")
            assert result.output == ""
            assert result.stop_reason == "error"
            assert result.diagnostic == "provider unavailable"
            """,
        )
        self.run_chapter(
            "15-external-capabilities",
            """
            from web_tools import WebSearchClient, WebSearchResult, WebSource

            client = WebSearchClient(api_key="test")
            calls = []

            def fake_search(query, max_results, max_uses):
                calls.append(query)
                if query == "fail":
                    raise RuntimeError("search failed")
                if query == "alpha":
                    urls = ["https://a/1", "https://shared", "https://a/3"]
                else:
                    urls = ["https://b/1", "https://shared", "https://b/3"]
                return WebSearchResult(
                    sources=tuple(WebSource(title=url, url=url) for url in urls)
                )

            client._search_one = fake_search
            result = client.search(["alpha", "beta", "alpha"], max_results=3)
            assert sorted(calls) == ["alpha", "beta"]
            assert [source.url for source in result.sources] == [
                "https://a/1", "https://b/1", "https://shared"
            ]
            assert result.truncated is True

            try:
                client.search(["same", "same", "same"], max_queries=2)
            except ValueError:
                pass
            else:
                raise AssertionError("query bound was applied after deduplication")

            try:
                client.search(["alpha", "fail"])
            except RuntimeError as error:
                assert str(error) == "search failed"
            else:
                raise AssertionError("partial batch success escaped a failed query")
            """,
        )

    def test_headless_uses_unique_default_logs_and_last_turn_status(self) -> None:
        self.run_chapter(
            "17-headless-capstone",
            """
            import os
            import tempfile
            from pathlib import Path
            import mini_harness.__main__ as entry
            from mini_harness.session import Session

            session = Session()
            session.append("assistant/message", {"content": "final", "tool_calls": []})
            session.append("turn/end", {"turn": 1, "reason": "completed"})
            session.append("turn/end", {"turn": 2, "reason": "error"})

            class FakeAgent:
                def followup(self, task):
                    assert task == "test task"
                def run(self):
                    return session

            entry.build_agent = FakeAgent
            original_cwd = Path.cwd()
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                try:
                    first_text, first_completed = entry.run_task("test task")
                    second_text, second_completed = entry.run_task("test task")
                    logs = sorted(Path(".mini-harness/sessions").glob("*.jsonl"))
                finally:
                    os.chdir(original_cwd)
            assert first_text == second_text == "final"
            assert first_completed is second_completed is False
            assert len(logs) == 2
            assert logs[0].name != logs[1].name
            """,
        )

    def test_headless_without_task_exits_before_loading_credentials(self) -> None:
        env = dict(os.environ)
        env.pop("DEEPSEEK_API_KEY", None)
        env["PYTHONPATH"] = str(ROOT / "chapters" / "17-headless-capstone" / "src")
        completed = subprocess.run(
            [sys.executable, "-m", "mini_harness"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("用法: mini-harness", completed.stderr)
        self.assertNotIn("DEEPSEEK_API_KEY", completed.stderr)


if __name__ == "__main__":
    unittest.main()
