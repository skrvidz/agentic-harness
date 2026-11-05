import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static, TextArea

from rector_harness.engine import Harness, hydrate_final_summary, parse_review
from rector_harness.model import ExecutionPlan, HarnessState
from rector_harness.planner import PlanHarness, TicketWorkspace, parse_plan
from rector_harness.changes import FileChange, change_summary, collect_changes
from rector_harness.github import inspect_github
from rector_harness.github import _https_remote, open_url
from rector_harness.runtime import ensure_harness_dir
from rector_harness.runner import DryRunEngineer, DryRunPlanner, DryRunReviewer
from rector_harness.runner import ClaudeRunner, CodexEngineerRunner, CodexRunner, _async_subprocess_result, _display_line, _parse_claude, _parse_codex
from rector_harness.cli import REVIEWER_SCHEMA
from rector_harness.prompts import reviewer_question_prompt
from rector_harness.rendering import render_activity_record, render_overview
from rector_harness.workspace import FullResponseScreen, ReviewerQuestionScreen, RunEditorScreen, WorkspaceApp, WorkspaceRegistry
from typer.testing import CliRunner
from rector_harness import cli


class HarnessTests(unittest.TestCase):
    def test_runtime_rejects_symlinked_harness_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".harness").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                ensure_harness_dir(root)
            self.assertFalse((outside / "state.json").exists())

    def test_runtime_directory_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness_dir = ensure_harness_dir(root)
            self.assertEqual(harness_dir.stat().st_mode & 0o777, 0o700)

    def test_github_link_rejects_other_hosts(self):
        self.assertEqual(_https_remote("https://evil.example/github.com/org/repo.git"), "")
        self.assertFalse(open_url("https://evil.example/github.com/org/repo"))

    def test_parse_plan_validates_dependencies(self):
        plan = parse_plan(json.dumps({
            "summary": "Split into independent increments.",
            "tickets": [
                {"id": "T1", "title": "Foundation", "task": "Implement foundation.", "acceptance_criteria": ["tests pass"], "depends_on": []},
                {"id": "T2", "title": "Consumer", "task": "Implement consumer.", "acceptance_criteria": ["consumer works"], "depends_on": ["T1"]},
            ],
        }), "Objective", ["global pass"])
        self.assertEqual([ticket.id for ticket in plan.tickets], ["T1", "T2"])
        self.assertEqual(plan.tickets[1].depends_on, ["T1"])

    def test_parse_plan_rejects_dependency_cycle(self):
        with self.assertRaises(ValueError):
            parse_plan(json.dumps({
                "tickets": [
                    {"id": "T1", "title": "One", "task": "One", "acceptance_criteria": [], "depends_on": ["T2"]},
                    {"id": "T2", "title": "Two", "task": "Two", "acceptance_criteria": [], "depends_on": ["T1"]},
                ],
            }), "Objective", [])

    def test_ticket_ids_cannot_escape_runtime_directory(self):
        for ticket_id in ("../outside", "../../outside", "/tmp/outside", "a/b", "a\\b", "x" * 65):
            with self.subTest(ticket_id=ticket_id), self.assertRaises(ValueError):
                parse_plan(json.dumps({"tickets": [
                    {"id": ticket_id, "title": "One", "task": "One", "depends_on": []}
                ]}), "Objective", [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps({
                "plan_id": "12345678-1234-1234-1234-123456789abc",
                "tickets": [{"id": "../../outside", "title": "One", "task": "One"}],
            }))
            with self.assertRaises(ValueError):
                ExecutionPlan.load(path)

    def test_parallel_ticket_refuses_unisolated_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TicketWorkspace(root, "12345678-1234-1234-1234-123456789abc", "T1")
            with patch("rector_harness.planner._git", return_value=(0, " M existing.py\n")):
                with self.assertRaises(RuntimeError):
                    manager.create(require_isolation=True)
            with patch("rector_harness.planner._git", side_effect=[(0, ""), (1, "worktree failed")]):
                with self.assertRaises(RuntimeError):
                    manager.create(require_isolation=True)

    def test_existing_ticket_path_must_belong_to_expected_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = TicketWorkspace(root, "12345678-1234-1234-1234-123456789abc", "T1")
            manager.path.mkdir()
            with patch("rector_harness.planner._git", side_effect=[
                (0, ""),
                (0, f"worktree {manager.path.resolve()}\nHEAD deadbeef\nbranch refs/heads/other\n"),
            ]):
                with self.assertRaises(RuntimeError):
                    manager.create(require_isolation=True)
            with patch("rector_harness.planner._git", side_effect=[
                (0, ""),
                (0, f"worktree {manager.path.resolve()}\nHEAD deadbeef\nbranch refs/heads/{manager.branch}\n"),
            ]):
                self.assertEqual(manager.create(require_isolation=True), manager.path)

    def test_plan_harness_runs_ticket_and_final_planner(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".harness").mkdir()
                state = HarnessState(run_id="plan-run", objective="Build thing", mode="plan")
                state.save(root / ".harness" / "state.json")
                harness = PlanHarness(
                    root,
                    state,
                    DryRunPlanner(),
                    lambda ticket_root, ticket: DryRunEngineer(),
                    lambda ticket_root, ticket: DryRunReviewer("DONE"),
                )
                result = await harness.run_async()
                self.assertEqual(result, 0)
                self.assertEqual(state.status, "completed")
                self.assertEqual(harness.plan.tickets[0].status, "completed")
                self.assertEqual(harness.plan.final_verdict, "DONE")

        asyncio.run(run())

    def test_activity_renderer_separates_reviewer_verdict_and_summary(self):
        rendered = render_activity_record({
            "at": "2026-08-31T20:00:00+00:00",
            "kind": "review",
            "role": "reviewer",
            "verdict": "REVISE",
            "severity": "major",
            "summary": "Retry handling needs idempotency.",
        })
        self.assertIsNotNone(rendered)
        self.assertIn("REVIEW", str(rendered))
        self.assertIn("REVISE", str(rendered))
        self.assertIn("Retry handling needs idempotency.", str(rendered))

    def test_activity_renderer_summarizes_shell_tooling(self):
        rendered = render_activity_record({
            "at": "2026-08-31T20:00:00+00:00",
            "kind": "agent_output",
            "role": "engineer",
            "message": "[tool] /bin/zsh -lc \"sed -n '1,80p' docs/report/README.md\nrg -n TODO docs/report\"",
        })
        value = str(rendered)
        self.assertIn("ENG", value)
        self.assertIn("BATCH", value)
        self.assertIn("2 steps", value)
        self.assertIn("README.md", value)
        self.assertNotIn("/bin/zsh -lc", value)

    def test_codex_completed_tool_event_is_not_rendered_twice(self):
        item = {"type": "command_execution", "command": "pytest -q"}
        started = _display_line("CODEX · ENGINEER", "stdout", json.dumps({"type": "item.started", "item": item}))
        completed = _display_line("CODEX · ENGINEER", "stdout", json.dumps({"type": "item.completed", "item": item}))
        self.assertEqual(started, "[tool] pytest -q")
        self.assertEqual(completed, "")

    def test_overview_exposes_findable_run_state(self):
        state = HarnessState(
            objective="Build anomaly detector",
            current_task="Implement spectral signature",
            acceptance_criteria=["tests pass", "no duplicates"],
            status="working",
            phase="revision",
            next_actor="reviewer",
            cycle=2,
            max_cycles=10,
            test_command="pytest -q",
            final_summary="Reviewer accepted the detector design.",
        )
        rendered = render_overview(Path("/tmp/project"), state, "Changes: 2 files")
        value = str(rendered)
        self.assertIn("RUN OVERVIEW", value)
        self.assertIn("STATUS WORKING", value)
        self.assertIn("NEXT REVIEWER", value)
        self.assertIn("Build anomaly detector", value)
        self.assertIn("FINAL REVIEW SUMMARY", value)

    def test_parse_review_accepts_fenced_json(self):
        review = parse_review('```json\n{"verdict":"REVISE","severity":"major","summary":"Needs a retry fix.","findings":[]}\n```')
        self.assertEqual(review.verdict, "REVISE")
        self.assertEqual(review.severity, "major")
        self.assertEqual(review.summary, "Needs a retry fix.")

    def test_reviewer_question_prompt_is_read_only_and_contains_question(self):
        prompt = reviewer_question_prompt(
            HarnessState(objective="Inspect objective"),
            "Is this persistence logic safe?",
            {"git_diff": "diff", "tests": "2 passed"},
        )
        self.assertIn("Is this persistence logic safe?", prompt)
        self.assertIn("Do not edit files", prompt)
        self.assertIn("git_diff", prompt)


    def test_dry_run_done_persists_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HarnessState(objective="Build thing", acceptance_criteria=["tests pass"], current_task="Implement it")
            state.save(root / ".harness" / "state.json")
            result = Harness(root, state, DryRunEngineer(), DryRunReviewer("DONE"), output=lambda _: None).run()
            self.assertEqual(result, 0)
            saved = HarnessState.load(root / ".harness" / "state.json")
            self.assertEqual(saved.status, "completed")
            self.assertEqual(saved.final_summary, "Dry-run review completed.")
            self.assertIn("Dry-run review completed.", (root / ".harness" / "final_summary.md").read_text())
            events = (root / ".harness" / "transcript.jsonl").read_text().splitlines()
            self.assertTrue(any(json.loads(event)["kind"] == "review" for event in events))
            completed = [json.loads(event) for event in events if json.loads(event)["kind"] == "completed"][-1]
            self.assertEqual(completed["data"]["summary"], "Dry-run review completed.")

    def test_completed_legacy_run_recovers_reviewer_summary_without_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness_dir = root / ".harness"
            harness_dir.mkdir()
            state = HarnessState(objective="Legacy", status="completed", phase="done", next_actor="done")
            state.save(harness_dir / "state.json")
            (harness_dir / "transcript.jsonl").write_text(
                json.dumps({"kind": "agent", "role": "reviewer", "summary": json.dumps({
                    "verdict": "DONE", "summary": "Legacy task was verified."})}) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(hydrate_final_summary(root, state))
            self.assertEqual(state.final_summary, "Legacy task was verified.")
            self.assertEqual(HarnessState.load(harness_dir / "state.json").final_summary, "Legacy task was verified.")


    def test_dry_run_revise_then_done(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HarnessState(objective="Build thing", current_task="Implement it", max_cycles=2)
            state.save(root / ".harness" / "state.json")
            # Use a reviewer that changes its fixture verdict after the first turn.
            reviewer = DryRunReviewer("REVISE")
            original = reviewer.run

            def alternating(prompt, session_id=None):
                if reviewer.calls >= 1:
                    reviewer.verdict = "DONE"
                return original(prompt, session_id)

            reviewer.run = alternating
            result = Harness(root, state, DryRunEngineer(), reviewer, output=lambda _: None).run()
            self.assertEqual(result, 0)
            self.assertEqual(state.status, "completed")

    def test_resume_pending_review_does_not_restart_engineer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HarnessState(objective="Review existing work", cycle=1, turn=2, max_cycles=1,
                                 next_actor="reviewer", pending_engineer_summary="Engineer completed the change.")
            state.save(root / ".harness" / "state.json")
            engineer = DryRunEngineer()
            result = Harness(root, state, engineer, DryRunReviewer("DONE"), output=lambda _: None).run()
            self.assertEqual(result, 0)
            self.assertEqual(engineer.calls, 0)
            self.assertEqual(state.status, "completed")

    def test_legacy_paused_reviewer_failure_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HarnessState(objective="Recover review", status="paused", cycle=1, turn=2)
            state.save(root / ".harness" / "state.json")
            transcript = root / ".harness" / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"kind": "agent", "role": "engineer", "turn": 1, "returncode": 0,
                            "summary": "Existing engineer result."}) + "\n" +
                json.dumps({"kind": "paused", "message": "Reviewer failed: old CLI error"}) + "\n",
                encoding="utf-8",
            )
            engineer = DryRunEngineer()
            harness = Harness(root, state, engineer, DryRunReviewer("DONE"), output=lambda _: None)
            self.assertEqual(state.next_actor, "reviewer")
            result = harness.run(max_cycles=1)
            self.assertEqual(result, 0)
            self.assertEqual(engineer.calls, 0)

    def test_non_interactive_human_decision_pauses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = HarnessState(objective="Choose safely", current_task="Inspect options")
            state.save(root / ".harness" / "state.json")
            result = Harness(root, state, DryRunEngineer(), DryRunReviewer("NEEDS_HUMAN"), output=lambda _: None).run(non_interactive=True)
            self.assertEqual(result, 2)
            saved = HarnessState.load(root / ".harness" / "state.json")
            self.assertEqual(saved.status, "paused")
            self.assertTrue(saved.open_questions)

    def test_codex_runner_keeps_reviewer_read_only(self):
        completed = type("Completed", (), {"stdout": '{"type":"thread.started","thread_id":"abc"}\n', "stderr": "", "returncode": 0})()
        with patch("rector_harness.runner.subprocess.run", return_value=completed) as run:
            CodexRunner(Path(".")).run("review")
            command = run.call_args.args[0]
            self.assertIn("--sandbox", command)
            self.assertIn("read-only", command)
            self.assertNotIn("--ask-for-approval", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_codex_engineer_uses_write_capable_codex_protocol(self):
        command = CodexEngineerRunner(Path("."), "gpt-5.6-luna")._command("implement")
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("workspace-write", command)
        self.assertNotIn("--output-format", command)
        self.assertNotIn("-p", command)

    def test_codex_schema_flag_precedes_schema_path_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / ".harness" / "reviewer_schema.json"
            schema.parent.mkdir()
            schema.write_text("{}")
            with patch("rector_harness.runner.subprocess.run", return_value=type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()):
                command = CodexRunner(root)._command("review prompt")
            index = command.index("--output-schema")
            self.assertEqual(command[index + 1], str(schema))
            self.assertEqual(command[-1], "review prompt")

    def test_reviewer_schema_is_strict_at_every_object_level(self):
        finding_schema = REVIEWER_SCHEMA["properties"]["findings"]["items"]
        self.assertFalse(finding_schema["additionalProperties"])
        self.assertEqual(
            finding_schema["required"], ["id", "issue", "evidence", "required_change"]
        )
        self.assertIn("summary", REVIEWER_SCHEMA["required"])

    def test_change_summary_includes_file_categories_and_line_totals(self):
        summary = change_summary([
            FileChange("A", "new.py", additions=12, deletions=0),
            FileChange("M", "changed.py", additions=4, deletions=3),
            FileChange("D", "removed.py", additions=0, deletions=8),
        ])
        self.assertEqual(summary, "Changes: 3 files · 27 lines · 1 added · 1 modified · 1 deleted")

    def test_collect_changes_excludes_harness_runtime_files(self):
        with patch("rector_harness.changes._git", side_effect=[
            (0, " M app.py\n?? .harness/state.json\n?? new.py\n"),
            (0, "4\t2\tapp.py\n"),
        ]):
            changes = collect_changes(Path("."))
        self.assertEqual([change.path for change in changes], ["app.py", "new.py"])

    def test_github_inspection_resolves_remote_branch_and_pull_request(self):
        def fake_run(args, root, timeout=10):
            if args[:3] == ["git", "config", "--get"]:
                return 0, "git@github.com:skrvidz/rector.git", ""
            if args[:3] == ["git", "branch", "--show-current"]:
                return 0, "feature/demo", ""
            return 0, '{"url":"https://github.com/skrvidz/rector/pull/7","title":"Demo","state":"OPEN"}', ""

        with patch("rector_harness.github._run", side_effect=fake_run):
            info = inspect_github(Path("."))
        self.assertEqual(info.repository_url, "https://github.com/skrvidz/rector")
        self.assertEqual(info.branch, "feature/demo")
        self.assertEqual(info.pull_request_url, "https://github.com/skrvidz/rector/pull/7")

    def test_workspace_registry_round_trips_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            registry = WorkspaceRegistry(base / "workspace.json")
            registry.theme = "nord"
            registry.add(project)
            loaded = WorkspaceRegistry(base / "workspace.json")
            self.assertEqual(loaded.paths, [str(project.resolve())])
            self.assertEqual(loaded.theme, "nord")
            loaded.remove(project)
            self.assertEqual(WorkspaceRegistry(base / "workspace.json").paths, [])

    def test_new_run_editor_mounts_with_blank_presets(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                tui = WorkspaceApp(registry=WorkspaceRegistry(Path(directory) / "workspace.json"))
                async with tui.run_test() as pilot:
                    tui.push_screen(RunEditorScreen(title="NEW RUN"))
                    await pilot.pause()
                    self.assertEqual(tui.screen.__class__.__name__, "RunEditorScreen")
                    tui.exit()

        asyncio.run(run())

    def test_run_editor_wizard_navigates_to_rich_review(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "project"
                root.mkdir()
                tui = WorkspaceApp(registry=WorkspaceRegistry(Path(directory) / "workspace.json"))
                async with tui.run_test() as pilot:
                    tui.push_screen(RunEditorScreen({
                        "root": str(root),
                        "objective": "Build a reliable pipeline",
                        "mode": "plan",
                    }, "NEW RUN"))
                    await pilot.pause()
                    screen = tui.screen
                    self.assertEqual(screen.step, 0)
                    for expected in range(1, 5):
                        screen.on_button_pressed(Button.Pressed(screen.query_one("#next", Button)))
                        await pilot.pause()
                        self.assertEqual(screen.step, expected, str(screen.query_one("#editor-validation", Static).render()))
                    self.assertEqual(screen.step, 4)
                    review = screen.query_one("#review-summary", Static).render()
                    self.assertIn("Build a reliable pipeline", str(review))
                    self.assertIn("Planner DAG", str(review))
                    self.assertEqual(screen.query_one("#save", Button).label, "Create run")
                    tui.exit()

        asyncio.run(run())

    def test_reviewer_question_screen_mounts(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                tui = WorkspaceApp(registry=WorkspaceRegistry(Path(directory) / "workspace.json"))
                async with tui.run_test() as pilot:
                    tui.push_screen(ReviewerQuestionScreen())
                    await pilot.pause()
                    self.assertEqual(tui.screen.__class__.__name__, "ReviewerQuestionScreen")
                    tui.exit()

        asyncio.run(run())

    def test_full_response_screen_shows_complete_artifact(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifacts = root / ".harness" / "artifacts"
                artifacts.mkdir(parents=True)
                complete = "first line\n" + ("complete response " * 500) + "\nlast line"
                (artifacts / "turn-0001-engineer.txt").write_text(complete, encoding="utf-8")
                tui = WorkspaceApp(registry=WorkspaceRegistry(root / "workspace.json"))
                async with tui.run_test() as pilot:
                    tui.push_screen(FullResponseScreen(root))
                    await pilot.pause()
                    content = tui.screen.query_one("#response-content", TextArea).text
                    self.assertEqual(content, complete)
                    tui.exit()

        asyncio.run(run())

    def test_full_response_screen_parses_codex_jsonl_and_resolves_inline_image(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifacts = root / ".harness" / "artifacts"
                artifacts.mkdir(parents=True)
                image = root / "figure.png"
                image.write_bytes(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ))
                answer = json.dumps({"summary": "Review complete", "figure": "figure.png"})
                raw = "\n".join([
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}),
                ])
                (artifacts / "turn-0001-reviewer.txt").write_text(raw, encoding="utf-8")
                tui = WorkspaceApp(registry=WorkspaceRegistry(root / "workspace.json"))
                async with tui.run_test() as pilot:
                    tui.push_screen(FullResponseScreen(root))
                    await pilot.pause()
                    screen = tui.screen
                    content = screen.query_one("#response-content", TextArea).text
                    self.assertNotIn("thread.started", content)
                    self.assertIn('"summary": "Review complete"', content)
                    self.assertEqual(screen.current_images, [image.resolve()])
                    tui.exit()

        asyncio.run(run())

    def test_workspace_creates_run_with_sqlite_run_id(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                project = base / "project"
                project.mkdir()
                registry = WorkspaceRegistry(base / "workspace.json")
                tui = WorkspaceApp(registry=registry)
                async with tui.run_test() as pilot:
                    tui._save_editor_result({
                        "root": str(project),
                        "objective": "Create from workspace",
                        "acceptance": "tests pass",
                        "task": "Implement it",
                        "engineer_command": "claude",
                        "engineer_model": "opus",
                        "reviewer_command": "codex",
                        "reviewer_model": "gpt-5.6-sol",
                        "test_command": "pytest -q",
                        "max_cycles": "4",
                        "max_engineer_turns": "30",
                    })
                    await pilot.pause()
                    state = HarnessState.load(project / ".harness" / "state.json")
                    self.assertTrue(state.run_id)
                    self.assertEqual(state.objective, "Create from workspace")
                    self.assertTrue((project / ".harness" / "harness.db").exists())
                    tui.exit()

        asyncio.run(run())

    def test_workspace_tui_renders_registered_run(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                project = base / "project"
                (project / ".harness").mkdir(parents=True)
                HarnessState(objective="Workspace objective", acceptance_criteria=["tests pass"]).save(
                    project / ".harness" / "state.json"
                )
                (project / ".harness" / "transcript.jsonl").write_text(
                    json.dumps({"at": "2026-08-27T20:00:00+00:00", "kind": "agent_output",
                                "role": "engineer", "message": "[tool] pytest -q"}) + "\n",
                    encoding="utf-8",
                )
                registry = WorkspaceRegistry(base / "workspace.json")
                registry.theme = "nord"
                registry.add(project)
                tui = WorkspaceApp(registry=registry)
                async with tui.run_test() as pilot:
                    await pilot.pause()
                    self.assertEqual(tui.theme, "nord")
                    self.assertEqual(len(tui.query_one("#runs").children), 1)
                    self.assertIn("Workspace objective", str(tui.query_one("#details").render()))
                    self.assertEqual(len(tui.query_one("#activity").lines), 1)
                    self.assertIn("ENG", str(tui.query_one("#activity").lines[0]))
                    self.assertIn("TEST", str(tui.query_one("#activity").lines[0]))
                    self.assertIn("pytest -q", str(tui.query_one("#activity").lines[0]))
                    tui.exit()

        asyncio.run(run())

    def test_workspace_continue_authorizes_revision_after_cycle_limit(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                project = base / "project"
                (project / ".harness").mkdir(parents=True)
                state = HarnessState(objective="Revision", phase="revision", next_actor="engineer",
                                     cycle=10, max_cycles=10, status="paused", current_task="Fix review findings")
                state.save(project / ".harness" / "state.json")
                registry = WorkspaceRegistry(base / "workspace.json")
                registry.add(project)
                tui = WorkspaceApp(registry=registry)
                async with tui.run_test() as pilot:
                    with patch.object(tui, "action_run_selected") as run_selected:
                        tui.action_continue_flow()
                        saved = HarnessState.load(project / ".harness" / "state.json")
                        self.assertEqual(saved.max_cycles, 11)
                        self.assertEqual(saved.status, "working")
                        run_selected.assert_called_once()
                    tui.exit()

        asyncio.run(run())

    def test_completed_run_can_prepare_follow_up_without_resetting_history(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                project = base / "project"
                (project / ".harness").mkdir(parents=True)
                state = HarnessState(objective="Follow up", run_id="run-follow-up", status="completed",
                                     phase="done", next_actor="done", cycle=2, max_cycles=2,
                                     final_summary="First task completed.")
                state.save(project / ".harness" / "state.json")
                (project / ".harness" / "transcript.jsonl").write_text("old history\n", encoding="utf-8")
                registry = WorkspaceRegistry(base / "workspace.json")
                registry.add(project)
                tui = WorkspaceApp(registry=registry)
                async with tui.run_test() as pilot:
                    tui._prepare_follow_up(project, state, "Validate the next increment")
                    await pilot.pause()
                    saved = HarnessState.load(project / ".harness" / "state.json")
                    self.assertEqual(saved.status, "idle")
                    self.assertEqual(saved.next_actor, "engineer")
                    self.assertEqual(saved.current_task, "Validate the next increment")
                    self.assertEqual(saved.max_cycles, 12)
                    self.assertIn("old history", (project / ".harness" / "transcript.jsonl").read_text())
                    tui.exit()

        asyncio.run(run())

    def test_workspace_receives_live_events_and_rebuilds_agent_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                project = base / "project"
                (project / ".harness").mkdir(parents=True)
                HarnessState(objective="Live events", run_id="run-live").save(project / ".harness" / "state.json")
                registry = WorkspaceRegistry(base / "workspace.json")
                registry.add(project)
                tui = WorkspaceApp(registry=registry)
                async with tui.run_test() as pilot:
                    with patch("rector_harness.workspace.ClaudeRunner", return_value=DryRunEngineer()), \
                         patch("rector_harness.workspace.CodexRunner", return_value=DryRunReviewer("DONE")):
                        tui.action_run_selected()
                        self.assertEqual(len(tui.active_harness.bus._listeners), 1)
                        for _ in range(5):
                            await pilot.pause()
                    self.assertEqual(str(tui.query_one("#engineer-status").render()), "✓ finished")
                    self.assertEqual(str(tui.query_one("#reviewer-status").render()), "✓ finished")
                    self.assertTrue(len(tui.query_one("#activity").lines) > 1)
                    tui.exit()

        asyncio.run(run())

    def test_empty_cli_command_opens_workspace(self):
        with patch("rector_harness.workspace.WorkspaceApp.run") as workspace_run:
            result = CliRunner().invoke(cli.app, [])
        self.assertEqual(result.exit_code, 0, result.stdout)
        workspace_run.assert_called_once()

    def test_async_subprocess_captures_output_without_shell(self):
        async def run():
            return await _async_subprocess_result(
                "TEST", [sys.executable, "-c", "print('{\"type\":\"message\",\"text\":\"ok\"}')"],
                Path("."), 5, _parse_codex
            )

        result = asyncio.run(run())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.text, "ok")

    def test_async_subprocess_accepts_large_jsonl_event(self):
        async def run():
            code = "import json; print(json.dumps({'type':'message','text':'x' * 100000}))"
            return await _async_subprocess_result(
                "TEST", [sys.executable, "-c", code], Path("."), 5, _parse_codex
            )

        result = asyncio.run(run())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.text), 100000)

    def test_claude_stream_is_decoded_for_live_output(self):
        raw = '\n'.join([
            '{"type":"system","session_id":"abc-123"}',
            '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"hello "}}}',
            '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"world"}}}',
            '{"type":"result","result":"hello world","session_id":"abc-123"}',
        ])
        text, session_id = _parse_claude(raw)
        self.assertEqual(text, "hello world")
        self.assertEqual(session_id, "abc-123")
        command = ClaudeRunner(Path("."))._command("work")
        self.assertIn("stream-json", command)
        self.assertIn("--include-partial-messages", command)


if __name__ == "__main__":
    unittest.main()
