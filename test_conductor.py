"""Focused regression tests; --live runs a small billed desktop integration test."""
import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from conductor_core import (Conductor, DEFAULTS, Store, WorkflowBusy, attachment_paths, packet, task_policy,
                            validate_route, workflow_guard, routed_workspace, highest_codex_model, parse_judgment,
                            runtime_policy, FINAL_EXECUTION_SCHEMA, presentation_text, evidence_audit,
                            audit_judgment_progress, valid_evidence_records)
from codex_bridge import CodexClient, CodexError, RecoveryRequired, Stopped, TransportError, resolve_codex_executable
from harness_bridge import HarnessClient, HarnessError


C_MODELS = {"gpt-6.1-sol": {"supportedReasoningEfforts": [{"reasoningEffort": "low"}]}}
H_MODELS = {("deepseek-official", "deepseek-flash"): {"reasoning": {"efforts": [{"id": "high"}], "defaultEffort": "high"}}}
ROUTE = {"mode": "dual", "use_astra": False, "reason": "test", "codex_thread": "new", "harness_session": "new",
         "codex_confidence": 0, "harness_confidence": 0, "codex_model": "gpt-6.1-sol", "codex_effort": "low",
         "harness_provider": "deepseek-official", "harness_model": "deepseek-flash", "harness_effort": "high"}


def current_acceptance(data):
    return {"limitations": [], "blocker_resolutions": [], **data,
            "checks": [{"required": False, **row} for row in data.get("checks", [])]}


class SettingsTests(unittest.TestCase):
    def test_concurrent_saves_publish_complete_independent_files(self):
        from concurrent.futures import ThreadPoolExecutor
        from conductor_core import save_settings
        barrier, rename_lock = threading.Barrier(2), threading.Lock()
        replace = Path.replace
        def publish(source, target):
            barrier.wait(timeout=5)
            with rename_lock:
                return replace(source, target)
        with tempfile.TemporaryDirectory() as directory, patch("conductor_core.DATA_DIR", Path(directory)):
            settings = [{**DEFAULTS, "default_model": model} for model in ("first", "second")]
            with patch.object(Path, "replace", publish), ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(save_settings, settings))
            saved = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            self.assertIn(saved, settings)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_failed_save_keeps_previous_settings_and_cleans_temporary_file(self):
        from conductor_core import save_settings
        with tempfile.TemporaryDirectory() as directory, patch("conductor_core.DATA_DIR", Path(directory)):
            target = Path(directory) / "settings.json"
            target.write_text('{"default_model":"previous"}', encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    save_settings(DEFAULTS)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["default_model"], "previous")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class EvidenceProtocolTests(unittest.TestCase):
    def record(self, *, source_ref="https://example.test/source", independence_key="source-a",
               excerpt="original line", relation="supports", verification="observed"):
        return {"claim": "the measured result is 3", "source_type": "external_source",
                "source_ref": source_ref, "excerpt": excerpt, "independence_key": independence_key,
                "verification": verification, "relation": relation}

    def test_same_dependency_is_reported_once_and_not_counted_as_independent(self):
        audit = evidence_audit([
            self.record(source_ref="https://mirror.test/a", excerpt="same source, line 1"),
            self.record(source_ref="https://mirror.test/b", excerpt="same source, line 2"),
        ])
        self.assertEqual(audit["independent_source_count"], 1)
        self.assertEqual(audit["new_independent_source_count"], 1)
        self.assertEqual(len(audit["duplicate_source_groups"]), 1)
        self.assertIn("不能按多票", " ".join(audit["warnings"]))

    def test_new_record_from_existing_source_is_visible_without_claiming_independence(self):
        old = [self.record(excerpt="old line")]
        audit = evidence_audit([self.record(excerpt="new line")], old)
        self.assertEqual(audit["new_record_count"], 1)
        self.assertEqual(audit["new_independent_source_count"], 0)

    def test_missing_provenance_cannot_be_treated_as_independent(self):
        audit = evidence_audit([self.record(source_ref="", independence_key="", verification="reported")])
        self.assertEqual(audit["attributed_count"], 0)
        self.assertEqual(audit["missing_source_count"], 1)

    def test_bad_provenance_record_is_rejected_by_audit_validation(self):
        self.assertFalse(valid_evidence_records({"evidence_records": [{"claim": "missing fields"}]}))

    def test_progress_claim_is_downgraded_when_revision_and_evidence_are_unchanged(self):
        action = {"deliverable": "same", "evidence_records": []}
        digest = __import__("hashlib").sha256(b"same").hexdigest()
        decision = {"progress": "improved"}
        result = audit_judgment_progress(decision, action,
                                         [{"deliverable_sha256": digest, "evidence_records": []}], 2)
        self.assertEqual(result["progress"], "none")
        self.assertIn("哈希未变化", result["progress_audit"])

    def test_new_evidence_claim_without_record_is_downgraded(self):
        result = audit_judgment_progress({"progress": "new_evidence"},
                                         {"deliverable": "candidate", "evidence_records": []}, [], 1)
        self.assertEqual(result["progress"], "none")
        self.assertIn("结构化证据", result["progress_audit"])


class ExecutableTests(unittest.TestCase):
    def test_codex_tools_inherit_managed_phase_guard(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.rpc, client._write = Mock(), Mock()
        with patch("codex_bridge.resolve_codex_executable", return_value="codex.exe"), \
                patch("codex_bridge.subprocess.Popen") as spawn, patch("codex_bridge.threading.Thread"), \
                patch.dict(os.environ, {"CONDUCTOR_TEST_PARENT": "preserved"}):
            client.start()
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["CONDUCTOR_MANAGED_PHASE"], "1")
        self.assertEqual(environment["CONDUCTOR_TEST_PARENT"], "preserved")

    def test_nested_cli_model_calls_fail_before_loading_provider_settings(self):
        import contextlib
        import io
        import agent_link
        for args in (("harness", "ask"), ("codex", "ask"), ("workflow", "ask"), ("workflow", "resume", "--job", "9")):
            with self.subTest(args=args), patch.object(sys, "argv", ["agent_link.py", *args]), \
                    patch.dict(os.environ, {"CONDUCTOR_MANAGED_PHASE": "1"}), \
                    patch("agent_link.load_settings") as settings, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as stopped:
                    agent_link.main()
                self.assertEqual(stopped.exception.code, 2)
                settings.assert_not_called()
                self.assertIn("outer coordinator", error.getvalue())

    def test_cli_peer_calls_cannot_bypass_an_active_workflow_without_environment_marker(self):
        import contextlib
        import io
        import agent_link
        with tempfile.TemporaryDirectory() as directory, patch("conductor_core.DATA_DIR", Path(directory)):
            prompt = Path(directory) / "prompt.txt"
            prompt.write_text("test only", encoding="utf-8")
            for provider, client_name in (("harness", "HarnessClient"), ("codex", "CodexClient")):
                with self.subTest(provider=provider), workflow_guard(), \
                        patch.object(sys, "argv", ["agent_link.py", provider, "ask", "--prompt-file", str(prompt)]), \
                        patch.dict(os.environ, {"CONDUCTOR_MANAGED_PHASE": "0"}), \
                        patch("agent_link.load_settings", return_value={**DEFAULTS, "workspace": directory}), \
                        patch("agent_link." + client_name) as constructor, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        agent_link.main()
                    self.assertEqual(stopped.exception.code, 2)
                    constructor.return_value.create.assert_not_called()
                    constructor.return_value.ask.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows desktop installation")
    def test_desktop_launch_without_codex_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "OpenAI" / "Codex" / "bin" / "installed-version" / "codex.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {"PATH": "", "LOCALAPPDATA": directory}):
                self.assertEqual(resolve_codex_executable("codex"), str(binary))

    def test_path_installation_takes_precedence(self):
        with patch("codex_bridge.shutil.which", return_value="custom-codex.exe"):
            self.assertEqual(resolve_codex_executable("codex"), "custom-codex.exe")

    def test_missing_explicit_path_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.exe")
            with self.assertRaisesRegex(CodexError, "Codex 程序"):
                resolve_codex_executable(missing)

    @unittest.skipUnless(os.name == "nt", "Windows desktop installation")
    def test_updated_desktop_installation_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "OpenAI" / "Codex" / "bin"
            for version, modified in (("old", 100), ("new", 200)):
                binary = root / version / "codex.exe"
                binary.parent.mkdir(parents=True)
                binary.touch()
                os.utime(binary, (modified, modified))
            with patch.dict(os.environ, {"PATH": "", "LOCALAPPDATA": directory}):
                self.assertEqual(resolve_codex_executable("codex"), str(root / "new" / "codex.exe"))


class HarnessDiscoveryTests(unittest.TestCase):
    def test_readonly_helper_overrides_full_access_and_verifies_before_prompt(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.full_access = True
        def row(preset):
            return [{"sessionId": "helper", "projections": {"values": {"permissions": {"currentValue": preset}}}}]
        client.list_sessions = Mock(side_effect=[row("danger-full-access"), row("read-only")])
        client.rpc = Mock(return_value={"result": {"kind": "success"}})
        client.follow = Mock(side_effect=HarnessError("test boundary"))
        with self.assertRaisesRegex(HarnessError, "test boundary"):
            client.ask("helper", "check", readonly=True)
        self.assertEqual(client.rpc.call_args.args[1]["line"], "/permission read-only")
        self.assertFalse(any(call.args[0] == "session/prompt" for call in client.rpc.call_args_list))

    def test_unconfirmed_readonly_permission_never_submits_helper(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.full_access = True
        client.list_sessions = Mock(return_value=[{"sessionId": "helper", "projections": {"values": {
            "permissions": {"currentValue": "danger-full-access"}}}}])
        client.rpc = Mock(return_value={"result": {"kind": "success"}})
        client.follow = Mock()
        with self.assertRaisesRegex(HarnessError, "未能确认只读"):
            client.ask("helper", "check", readonly=True)
        client.follow.assert_not_called()

    def setUp(self):
        fallback = patch.object(HarnessClient, "_native_listener_ports", return_value=[])
        fallback.start()
        self.addCleanup(fallback.stop)

    def test_native_discovery_avoids_shell_and_expires_old_grant(self):
        client = HarnessClient()
        client.url, client.cookie = "http://127.0.0.1:11111", "old"
        with patch.object(client, "_native_listener_ports", return_value=[54321]), patch("harness_bridge.subprocess.run") as shell:
            self.assertEqual(client.discover(force=True), "http://127.0.0.1:54321")
            shell.assert_not_called()
        self.assertEqual(client.cookie, "")
    def test_port_discovery_uses_system_powershell_path(self):
        with patch("harness_bridge.shutil.which", return_value=None), patch("harness_bridge.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="54321\n")) as run:
            self.assertEqual(HarnessClient().discover(), "http://127.0.0.1:54321")
        executable = Path(run.call_args.args[0][0])
        self.assertTrue(executable.is_absolute())
        self.assertEqual(executable.name, "powershell.exe")

    def test_discovery_startup_failure_has_actionable_error(self):
        with patch("harness_bridge.subprocess.run", side_effect=FileNotFoundError("missing")):
            with self.assertRaisesRegex(HarnessError, "Harness 自动发现失败"):
                HarnessClient().discover()

    def test_new_auto_discovered_port_invalidates_old_cookie(self):
        client = HarnessClient()
        results = [SimpleNamespace(returncode=0, stdout=str(port)) for port in (54321, 54322)]
        with patch("harness_bridge.subprocess.run", side_effect=results):
            client.discover()
            client.cookie, client.cookie_until = "old-cookie", time.time() + 3000
            self.assertEqual(client.discover(force=True), "http://127.0.0.1:54322")
        self.assertEqual((client.cookie, client.cookie_until), ("", 0.0))

    def test_network_failure_never_replays_a_prompt(self):
        client = HarnessClient()
        client.authenticate = lambda: None
        client.url = "http://127.0.0.1:54321"
        with patch.object(client.opener, "open", side_effect=OSError("response lost")) as request:
            with self.assertRaises(RecoveryRequired):
                client.rpc("session/prompt", {"request": {"text": "test"}})
        self.assertEqual(request.call_count, 1)

    def test_ambiguous_prompt_rpc_preserves_request_identity_and_never_replays(self):
        import io
        import http.client
        import urllib.error
        client = HarnessClient(url="http://127.0.0.1:54321")
        client.authenticate = lambda: None
        args = {"request": {"sessionId": "original-session", "requestId": "original-request"}}
        failures = [OSError("response lost"), TimeoutError("timeout"),
            http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("bad status"),
            urllib.error.HTTPError(client.url, 503, "upstream error", {}, None),
            urllib.error.HTTPError(client.url, 408, "request timeout", {}, None)]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(client.opener, "open", side_effect=failure) as opened:
                with self.assertRaisesRegex(RecoveryRequired, "original-session.*original-request"):
                    client.rpc("session/prompt", args)
                self.assertEqual(opened.call_count, 1)
        for payload in (b"invalid json", b"[]", b'{"rpcId":"wrong","type":"server-response"}',
                        b'{"rpcId":"rpc","type":"server-response","result":{}}'):
            with self.subTest(payload=payload), patch("harness_bridge.uuid.uuid4", return_value="rpc"), \
                    patch.object(client.opener, "open", return_value=io.BytesIO(payload)) as opened:
                with self.assertRaises(RecoveryRequired):
                    client.rpc("session/prompt", args)
                self.assertEqual(opened.call_count, 1)

    def test_explicit_prompt_rejection_remains_ordinary_failure(self):
        import io
        import urllib.error
        client = HarnessClient(url="http://127.0.0.1:54321")
        client.authenticate = lambda: None
        with patch.object(client.opener, "open", side_effect=urllib.error.HTTPError(client.url, 403, "denied", {}, None)) as opened:
            with self.assertRaises(HarnessError):
                client.rpc("session/prompt", {})
            self.assertEqual(opened.call_count, 1)
        body = json.dumps({"rpcId": "rpc", "type": "server-response", "result": {
            "ok": False, "error": {"code": "busy", "message": "rejected"}}}).encode()
        with patch("harness_bridge.uuid.uuid4", return_value="rpc"), patch.object(client.opener, "open", return_value=io.BytesIO(body)):
            with self.assertRaisesRegex(HarnessError, "busy"):
                client.rpc("session/prompt", {})


class RouteTests(unittest.TestCase):
    def validate(self, route=None, crows=None, hrows=None, controls=None):
        return validate_route(route or ROUTE, C_MODELS, H_MODELS, crows or [], hrows or [], controls or {})

    def test_unknown_automatic_id_becomes_new(self):
        self.assertEqual(self.validate({**ROUTE, "codex_thread": "invented", "codex_confidence": 1})["codex_thread"], "new")

    def test_uncertain_match_becomes_new(self):
        route = {**ROUTE, "codex_thread": "old", "codex_confidence": .4}
        self.assertEqual(self.validate(route, [{"id": "old"}])["codex_thread"], "new")

    def test_busy_manual_selection_fails(self):
        with self.assertRaisesRegex(ValueError, "正在运行"):
            self.validate(crows=[{"id": "old", "status": {"type": "active"}}], controls={"codex_thread": "old"})

    def test_manual_selection_is_respected(self):
        result = self.validate(crows=[{"id": "old"}], controls={"codex_thread": "old"})
        self.assertEqual(result["codex_thread"], "old")

    def test_unavailable_model_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "模型"):
            self.validate({**ROUTE, "codex_model": "imaginary"})

    def test_explicit_codex_suppresses_astra(self):
        result = self.validate({**ROUTE, "use_astra": True}, controls={"mode": "codex"})
        self.assertEqual((result["mode"], result["use_astra"]), ("codex", False))

    def test_explicit_important_request_forces_peer_review(self):
        result = self.validate({**ROUTE, "mode": "codex"}, controls={"mode": "codex", "force_review": True})
        self.assertEqual(result["mode"], "dual")

    def test_explicit_astra_request_forces_astra_and_peer_review(self):
        result = self.validate({**ROUTE, "mode": "codex"}, controls={"mode": "codex", "force_astra": True})
        self.assertEqual((result["mode"], result["use_astra"]), ("dual", True))

    def test_task_policy_only_hard_wording_overrides_auto(self):
        self.assertEqual(task_policy("整理这份普通会议纪要"), {"force_review": False, "force_astra": False})
        self.assertEqual(task_policy("这是重要任务，请让DP审核"), {"force_review": True, "force_astra": False})
        self.assertEqual(task_policy("必须让Astra指挥"), {"force_review": True, "force_astra": True})

    def test_task_policy_ignores_attachment_names_and_negative_wording(self):
        self.assertEqual(task_policy("整理文件\n[附件]\n- C:\\重要项目\\不要让DP审核.txt"), {"force_review": False, "force_astra": False})
        self.assertEqual(task_policy("普通任务，不要让DP审核，也不要用Astra"), {"force_review": False, "force_astra": False})
        self.assertEqual(task_policy("必须由Astra审核这个重要方案"), {"force_review": True, "force_astra": True})
        for task in ("不重要，不需要审核一下", "不要让Astra指挥，不要让DP审核"):
            self.assertEqual(task_policy(task), {"force_review": False, "force_astra": False})
        self.assertTrue(task_policy("不要让Astra指挥，但请审核一下")["force_review"])

    def test_codex_mode_does_not_require_harness_catalog(self):
        route = validate_route({**ROUTE, "mode": "codex", "use_astra": False}, C_MODELS, {}, [], [], {"harness_session": "deleted"})
        self.assertEqual(route["mode"], "codex")

    def test_dropped_paths_are_carried_to_agent_packet(self):
        task = "分析这个文件\n\n[附件]\n- C:\\tmp\\a b.txt\n- C:\\tmp\\a b.txt"
        self.assertEqual(attachment_paths(task), ["C:\\tmp\\a b.txt"])
        data = json.loads(packet("independent", task).split("\n", 1)[1])
        self.assertEqual(data["attachments"], ["C:\\tmp\\a b.txt"])


class StopTests(unittest.TestCase):
    def test_full_access_turn_and_approvals_use_unattended_policy(self):
        client = CodexClient()
        client.full_access = True
        requests, replies = [], []
        client.rpc = lambda method, params, **kwargs: (requests.append((method, params)) or {"turn": {"id": "turn"}})
        client._write = replies.append
        client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"type": "agentMessage", "text": "ok"}}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}})
        self.assertEqual(client.ask("thread", "task", "model", "xhigh", readonly=True), "ok")
        self.assertEqual(requests[0][1]["approvalPolicy"], "never")
        self.assertEqual(requests[0][1]["sandboxPolicy"], {"type": "dangerFullAccess"})
        client._server_request({"id": 7, "method": "item/fileChange/requestApproval"}, interactive=True)
        self.assertEqual(replies[-1]["result"], {"decision": "accept"})

    def test_declined_harness_permission_never_submits_model_prompt(self):
        client = HarnessClient()
        client.full_access = True
        client.list_sessions = lambda: [{"sessionId": "test", "running": False}]
        calls = []
        client.rpc = lambda method, args=None: (calls.append(method) or {"result": {"kind": "error"}})
        with self.assertRaisesRegex(HarnessError, "未发送模型任务"):
            client.ask("test", "task")
        self.assertNotIn("session/prompt", calls)

    def test_structured_reply_excludes_progress_commentary(self):
        client = CodexClient()
        client.rpc = lambda *args, **kwargs: {"turn": {"id": "turn"}}
        for text in ("I will verify the evidence.", '{"review":"ok"}'):
            client.events.put({"method": "item/completed", "params": {
                "threadId": "thread", "turnId": "turn", "item": {"type": "agentMessage", "text": text}}})
        client.events.put({"method": "turn/completed", "params": {
            "threadId": "thread", "turn": {"id": "turn", "status": "completed"}}})
        reply = client.ask("thread", "test", "model", "low", schema={"type": "object"}, readonly=True)
        self.assertEqual(json.loads(reply), {"review": "ok"})

    def test_interrupt_is_idempotent_after_server_stops(self):
        client = CodexClient()
        client.active = ("thread", "turn")
        def stopped(*args, **kwargs):
            raise CodexError("turn/interrupt: no active turn to interrupt")
        client.rpc = stopped
        client.cancel()
        self.assertTrue(client.stop_event.is_set())

    def test_unexpected_interrupt_error_is_reported(self):
        client = CodexClient()
        client.active = ("thread", "turn")
        def offline(*args, **kwargs):
            raise CodexError("offline")
        client.rpc = offline
        with self.assertRaisesRegex(CodexError, "offline"):
            client.cancel()


class RecoveryTests(unittest.TestCase):
    def test_reconnected_quota_failure_never_submits_another_turn(self):
        from unittest.mock import Mock
        from codex_bridge import TerminalStreamError
        for cause in ("用户额度不足", "余额不足", "insufficient balance", "quota exceeded"):
            with self.subTest(cause=cause):
                client = CodexClient()
                client.proc = SimpleNamespace(poll=lambda: None)
                client.rpc = Mock(return_value={"data": [{"id": "original", "status": "failed",
                                                         "error": {"message": cause}}]})
                def disconnected(*args, **kwargs):
                    client.active = ("thread", "original")
                    raise TerminalStreamError("stream disconnected")
                client._ask_once = Mock(side_effect=disconnected)
                with self.assertRaisesRegex(RecoveryRequired, "余额|账号"):
                    client.ask("thread", "task", "model", "low", readonly=True)
                self.assertEqual(client._ask_once.call_count, 1)
                self.assertEqual([call.args[0] for call in client.rpc.call_args_list], ["thread/turns/list"])

    def test_provider_retry_notices_are_correlated_deduplicated_and_do_not_replay(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.rpc = Mock(return_value={"turn": {"id": "original"}})
        notices = []
        client.notice = notices.append
        for thread, turn in (("foreign", "original"), ("thread", "old"), (None, None),
                             ("thread", "original"), ("thread", "original")):
            client.events.put({"method": "error", "params": {
                "threadId": thread, "turnId": turn, "willRetry": True,
                "error": {"message": "Reconnecting... 1/5 sk-do-not-display", "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 503}}}}})
        client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "original",
            "item": {"type": "agentMessage", "text": "DONE"}}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
            "turn": {"id": "original", "status": "completed"}}})
        self.assertEqual(client.ask("thread", "task", "model", "low"), "DONE")
        self.assertEqual(notices, ["Codex 服务端正在重连原回合（1/5）（服务端原因：HTTP 503）"])
        self.assertEqual([call.args[0] for call in client.rpc.call_args_list], ["turn/start"])

    def test_provider_context_only_exposes_whitelisted_exact_turn_cause(self):
        client = CodexClient()
        client.active = ("thread", "original")
        client.provider_error = (client.active, 503)
        for row in ([], {"target": "other", "fields": {"turn_id": "original"}},
                    {"target": "codex_core::responses_retry", "fields": None},
                    {"target": "codex_core::responses_retry", "fields": {
                        "turn_id": "old", "sampling_error": "No available channel for model WRONG"}},
                    {"target": "codex_core::responses_retry", "fields": {
                        "turn_id": "original", "sampling_error": "No available channel for model gpt-6-luna; sk-secret url: private"}}):
            client.stderr_tail.append(json.dumps(row))
        client.stderr_tail.append("invalid JSON")
        self.assertEqual(client._provider_context(), "（服务端原因：HTTP 503，模型通道不可用：gpt-6-luna）")
        client.active = ("thread", "next")
        self.assertEqual(client._provider_context(), "")

    def test_provider_cause_survives_recovery_without_replaying_full_access(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.full_access = True
        client.proc = SimpleNamespace(poll=lambda: None)
        client.rpc = Mock(side_effect=[{"turn": {"id": "original"}},
            {"data": [{"id": "original", "status": "interrupted"}]}])
        client.events.put({"method": "error", "params": {"threadId": "thread", "turnId": "original",
            "willRetry": True, "error": {"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}}}}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
            "turn": {"id": "original", "status": "failed", "error": {"message": "stream disconnected"}}}})
        with self.assertRaisesRegex(RecoveryRequired, "HTTP 503"):
            client.ask("thread", "task", "model", "low", readonly=True)
        self.assertEqual([call.args[0] for call in client.rpc.call_args_list], ["turn/start", "thread/turns/list"])

    def test_failed_handshake_closes_process_and_allows_fresh_start(self):
        from unittest.mock import Mock
        from io import StringIO
        for failure in ("initialize", "initialized"):
            with self.subTest(failure=failure):
                client = CodexClient()
                processes = [Mock(stdout=StringIO(), stderr=StringIO()),
                             Mock(stdout=StringIO(), stderr=StringIO())]
                for proc in processes:
                    proc.poll.return_value = None
                client.rpc = Mock(side_effect=[TimeoutError("init timeout"), {}] if failure == "initialize" else [{}, {}])
                client._write = Mock(side_effect=[TransportError("closed"), None] if failure == "initialized" else None)
                with patch("codex_bridge.resolve_codex_executable", return_value="codex.exe"), \
                     patch("codex_bridge.subprocess.Popen", side_effect=processes), \
                     patch("codex_bridge.threading.Thread"):
                    with self.assertRaises(CodexError):
                        client.start()
                    self.assertIsNone(client.proc)
                    processes[0].terminate.assert_called_once()
                    client.start()
                    self.assertIs(client.proc, processes[1])

    def test_closed_stdin_is_transport_error(self):
        from io import StringIO
        client = CodexClient()
        closed = StringIO()
        closed.close()
        client.proc = SimpleNamespace(poll=lambda: None, stdin=closed)
        with self.assertRaises(TransportError):
            client._write({"method": "initialized"})

    def test_recovery_pages_to_exact_old_turn_without_new_submission(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.rpc = Mock(side_effect=[
            {"data": [{"id": f"new-{i}", "status": "completed"} for i in range(20)], "nextCursor": "page-2"},
            {"data": [{"id": "original", "status": "completed", "items": [{"type": "agentMessage", "text": "OLD"}]}]},
        ])
        self.assertEqual(client._recover_turn("thread", "original", None, 10, False), ("OLD", True))
        self.assertEqual([call.args[0] for call in client.rpc.call_args_list], ["thread/turns/list"] * 2)
        self.assertEqual(client.rpc.call_args_list[1].args[1]["cursor"], "page-2")

    def test_recovery_cursor_cycle_requires_attention(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.rpc = Mock(return_value={"data": [], "nextCursor": "same"})
        with self.assertRaisesRegex(RecoveryRequired, "分页重复"):
            client._read_turn("thread", "original")
        self.assertEqual(client.rpc.call_count, 2)

    def test_recovery_pagination_obeys_overall_lookup_deadline(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.rpc = Mock(return_value={"data": [], "nextCursor": "next"})
        with patch("codex_bridge.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RecoveryRequired, "历史查询超时"):
                client._read_turn("thread", "original", deadline=1)
        self.assertEqual(client.rpc.call_count, 1)
        self.assertEqual(client.rpc.call_args.kwargs["timeout"], 1)

    def test_missing_original_turn_does_not_rescan_history_indefinitely(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.stop_event.wait = lambda delay: False
        client.rpc = Mock(return_value={"data": []})
        with self.assertRaisesRegex(RecoveryRequired, "两次完整查询"):
            client._recover_turn("thread", "original", None, 3600, False)
        self.assertEqual(client.rpc.call_count, 2)

    def test_recovery_pagination_obeys_stop_before_next_page(self):
        from unittest.mock import Mock
        client = CodexClient()
        def page(*args, **kwargs):
            client.stop_event.set()
            return {"data": [], "nextCursor": "next"}
        client.rpc = Mock(side_effect=page)
        with self.assertRaises(Stopped):
            client._read_turn("thread", "original")
        self.assertEqual(client.rpc.call_count, 1)

    def client(self, statuses):
        client = CodexClient()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.stop_event.wait = lambda delay: False
        calls = []
        states = iter(statuses)
        def rpc(method, params, **kwargs):
            calls.append((method, params))
            if method == "thread/turns/list":
                status = next(states)
                return {"data": [{"id": "original", "status": status,
                    "items": [{"type": "agentMessage", "text": "RECOVERED"}]}]}
            return {}
        client.rpc = rpc
        def attempt(*args, **kwargs):
            client.active = ("thread", "original")
            raise TimeoutError("deadline")
        client._ask_once = attempt
        return client, calls

    def test_lost_completion_is_recovered_without_resubmission(self):
        client, calls = self.client(["completed"])
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=True), "RECOVERED")
        self.assertEqual([method for method, _ in calls], ["thread/turns/list"])

    def test_timeout_confirms_interruption_before_readonly_retry(self):
        client, calls = self.client(["inProgress", "interrupted"])
        first = client._ask_once
        attempts = []
        def run(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return first(*args, **kwargs)
            return "RETRIED"
        client._ask_once = run
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=True), "RETRIED")
        self.assertEqual([method for method, _ in calls], ["thread/turns/list", "turn/interrupt", "thread/turns/list", "thread/resume"])
        self.assertEqual(calls[1][1], {"threadId": "thread", "turnId": "original"})

    def test_mutating_turn_is_never_replayed(self):
        client, calls = self.client(["interrupted"])
        with self.assertRaisesRegex(RecoveryRequired, "未重复执行"):
            client.ask("thread", "task", "model", "low", readonly=False)
        self.assertEqual([method for method, _ in calls], ["thread/turns/list"])

    def test_write_completion_can_be_recovered_without_executing_again(self):
        client, calls = self.client(["completed"])
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=False), "RECOVERED")
        self.assertNotIn("turn/start", [method for method, _ in calls])

    def test_full_access_readonly_prompt_cannot_replay_after_interruption(self):
        client, calls = self.client(["interrupted"])
        client.full_access = True
        with self.assertRaises(RecoveryRequired):
            client.ask("thread", "task", "model", "low", readonly=True)
        self.assertEqual([method for method, _ in calls], ["thread/turns/list"])

    def test_full_access_completed_result_can_still_be_recovered(self):
        client, calls = self.client(["completed"])
        client.full_access = True
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=True), "RECOVERED")
        self.assertEqual([method for method, _ in calls], ["thread/turns/list"])

    def test_start_ack_timeout_never_resubmits(self):
        client = CodexClient()
        starts = []
        def rpc(*args, **kwargs):
            starts.append(args[0])
            raise TimeoutError("ack")
        client.rpc = rpc
        with self.assertRaisesRegex(RecoveryRequired, "接收状态不明"):
            client.ask("thread", "task", "model", "low", readonly=True)
        self.assertEqual(starts, ["turn/start"])

    def test_retry_exhaustion_retains_attention_status(self):
        client, calls = self.client(["interrupted", "interrupted"])
        with self.assertRaisesRegex(RecoveryRequired, "仍未完成"):
            client.ask("thread", "task", "model", "low", readonly=True)
        self.assertEqual([method for method, _ in calls].count("thread/resume"), 1)

    def test_user_stop_prevents_recovery_and_retry(self):
        client, calls = self.client([])
        first = client._ask_once
        def stopped(*args, **kwargs):
            client.stop_event.set()
            return first(*args, **kwargs)
        client._ask_once = stopped
        with self.assertRaises(Stopped):
            client.ask("thread", "task", "model", "low", readonly=True)
        self.assertFalse(calls)

    def test_recovery_matches_exact_turn_not_newer_completion(self):
        client = CodexClient()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.rpc = lambda *args, **kwargs: {"data": [
            {"id": "newer", "status": "completed", "items": [{"type": "agentMessage", "text": "WRONG"}]},
            {"id": "original", "status": "interrupted"}]}
        reply, terminal = client._recover_turn("thread", "original", None, 1, True)
        self.assertIsNone(reply)
        self.assertTrue(terminal)

    def test_old_reader_cannot_poison_new_connection(self):
        from io import StringIO
        import queue
        client = CodexClient()
        client.proc = object()
        waiter = queue.Queue()
        client.pending[1] = waiter
        old = SimpleNamespace(stdout=StringIO('{"id":1,"result":{"wrong":true}}\n'))
        client._read(old)
        self.assertTrue(waiter.empty())
        self.assertTrue(client.events.empty())

    def test_transport_failure_restarts_connection_then_recovers(self):
        client, calls = self.client(["completed"])
        lifecycle = []
        client.close = lambda: lifecycle.append("close")
        client.start = lambda: lifecycle.append("start")
        def disconnected(*args, **kwargs):
            client.active = ("thread", "original")
            raise TransportError("disconnected")
        client._ask_once = disconnected
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=True), "RECOVERED")
        self.assertEqual(lifecycle, ["close", "start"])

    def test_terminal_stream_failure_keeps_ephemeral_thread_loaded(self):
        from codex_bridge import TerminalStreamError
        client, calls = self.client(["completed"])
        client.close = lambda: self.fail("must not discard the loaded ephemeral thread")
        def failed(*args, **kwargs):
            client.active = ("thread", "original")
            raise TerminalStreamError("stream disconnected")
        client._ask_once = failed
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=True), "RECOVERED")

    def test_restart_reloads_persisted_thread_before_reading_original_turn(self):
        client, calls = self.client(["completed"])
        original = client.rpc
        missing = [True]
        def rpc(method, params, **kwargs):
            if method == "thread/turns/list" and missing:
                missing.pop()
                calls.append((method, params))
                raise CodexError("thread not loaded: thread")
            return original(method, params, **kwargs)
        client.rpc = rpc
        self.assertEqual(client.ask("thread", "task", "model", "low", readonly=False), "RECOVERED")
        self.assertEqual([method for method, _ in calls], ["thread/turns/list", "thread/resume", "thread/turns/list"])

    def test_missing_thread_reload_is_bounded_and_does_not_replay(self):
        client, _ = self.client([])
        methods = []
        def rpc(method, params, **kwargs):
            methods.append(method)
            if method == "thread/turns/list":
                raise CodexError("thread not loaded: thread")
            return {}
        client.rpc = rpc
        with self.assertRaises(RecoveryRequired):
            client.ask("thread", "task", "model", "low", readonly=False)
        self.assertEqual(methods, ["thread/turns/list", "thread/resume", "thread/turns/list"])

    def test_acknowledged_overload_remains_a_terminal_provider_error(self):
        client = CodexClient()
        client.full_access = True
        client.rpc = lambda *args, **kwargs: {"turn": {"id": "original"}}
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
            "id": "original", "status": "failed", "error": {
                "message": "stream disconnected before completion: Our servers are currently overloaded"}}}})
        client.close = lambda: self.fail("a provider overload must not restart app-server")
        with self.assertRaisesRegex(CodexError, "overloaded") as raised:
            client.ask("thread", "task", "model", "low", readonly=False)
        self.assertNotIsInstance(raised.exception, RecoveryRequired)
        self.assertNotIsInstance(raised.exception, TransportError)

    def test_progressing_turn_can_exceed_idle_timeout(self):
        import itertools
        client = CodexClient()
        client.rpc = lambda *args, **kwargs: {"turn": {"id": "original"}}
        for text in ("working", "finished"):
            client.events.put({"method": "item/completed", "params": {
                "threadId": "thread", "turnId": "original", "item": {"type": "agentMessage", "text": text}}})
        client.events.put({"method": "turn/completed", "params": {
            "threadId": "thread", "turn": {"id": "original", "status": "completed"}}})
        with patch("codex_bridge.time.monotonic", side_effect=itertools.count(0, .6)):
            self.assertEqual(client.ask("thread", "task", "model", "low", timeout=1), "working\n\nfinished")

    def test_harness_stream_recovery_follows_original_request_once(self):
        import websocket
        class Stream:
            def recv(self):
                raise websocket.WebSocketConnectionClosedException("offline")
            def close(self):
                pass
            def send(self, value):
                pass
        events = [
            {"type": "turn/start", "data": {"turn": "turn"}},
            {"type": "user/message", "data": {"source": {"rpcId": "request"}}},
            {"type": "assistant/message", "data": {"turn": "turn", "message": {"content": [{"type": "text", "text": "H_RECOVERED"}]}}},
            {"type": "turn/end", "data": {"turn": "turn", "reason": {"kind": "completed"}}},
        ]
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        follows, prompts = [], []
        client.rpc = lambda method, args: prompts.append(method)
        def follow(session):
            follows.append(session)
            records = [] if len(follows) == 1 else [{"type": "event", "event": event} for event in events]
            return Stream(), "stream", {"records": records}
        client.follow = follow
        with patch("harness_bridge.uuid.uuid4", return_value="request"):
            self.assertEqual(client.ask("session", "task"), "H_RECOVERED")
        self.assertEqual(prompts, ["session/prompt"])
        self.assertEqual(len(follows), 2)

    def test_harness_failed_stream_recovery_retains_original_request_identity(self):
        import websocket
        from unittest.mock import Mock
        client = HarnessClient()
        stream = Mock()
        stream.recv.side_effect = websocket.WebSocketConnectionClosedException("lost stream")
        stream.close.side_effect = OSError("broken connection cleanup")
        client.list_sessions = lambda: [{"sessionId": "original-session", "running": False}]
        client.rpc = Mock()
        client.cancel_event.wait = lambda delay: False
        client.follow = Mock(side_effect=[(stream, "stream", {"records": []}), *[OSError("offline") for _ in range(3)]])
        with patch("harness_bridge.uuid.uuid4", return_value="original-request"):
            with self.assertRaisesRegex(RecoveryRequired, "sessionId=original-session requestId=original-request"):
                client.ask("original-session", "task")
        self.assertEqual(client.rpc.call_count, 1)
        self.assertEqual(client.rpc.call_args.args[0], "session/prompt")
        self.assertEqual(client.follow.call_count, 4)

    def test_harness_recovers_repeated_live_drops_and_temporary_follow_failures(self):
        import websocket
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        client.cancel_event.wait = lambda delay: False
        client.rpc = Mock()
        def record(seq, kind, data):
            return {"type": "event", "event": {"seq": seq, "type": kind, "data": data}}
        start = record(1, "turn/start", {"turn": 3})
        admitted = record(2, "user/message", {"source": {"rpcId": "request"}})
        answer = record(3, "assistant/message", {"turn": 3, "usage": {"total": 4},
            "message": {"content": [{"type": "text", "text": "SAVED"}]}})
        end = record(4, "turn/end", {"turn": 3, "reason": {"kind": "completed"}})
        first, second, third = Mock(), Mock(), Mock()
        first.recv.side_effect = [json.dumps({"streamId": "stream", "value": row}) for row in (start, admitted)] + [OSError("drop 1")]
        second.recv.side_effect = websocket.WebSocketConnectionClosedException("drop 2")
        client.follow = Mock(side_effect=[(first, "stream", {"records": []}), OSError("open failed"),
            (second, "stream", {"records": [start, admitted, answer]}),
            (third, "stream", {"records": [answer, end]})])
        progress = []
        notices = []
        client.notice = notices.append
        with patch("harness_bridge.uuid.uuid4", return_value="request"):
            self.assertEqual(client.ask("session", "task", progress=progress.append), "SAVED")
        self.assertEqual(progress, ["SAVED"])
        self.assertEqual(client.last_usage, [{"total": 4}])
        self.assertEqual(client.follow.call_count, 4)
        self.assertEqual(client.rpc.call_count, 1)
        self.assertEqual([notice.split("第 ")[1].split(" 次")[0] for notice in notices], ["1/3", "2/3", "1/3"])

    def test_harness_nonprogressing_reopens_are_bounded(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        client.cancel_event.wait = lambda delay: False
        client.rpc = Mock()
        stream = Mock()
        stream.recv.side_effect = OSError("always drops before progress")
        client.follow = Mock(return_value=(stream, "stream", {"records": []}))
        with self.assertRaises(RecoveryRequired):
            client.ask("session", "task")
        self.assertEqual(client.follow.call_count, 4)
        self.assertEqual(client.rpc.call_count, 1)

    def test_harness_stop_during_reconnect_does_not_open_or_resubmit(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        client.cancel_event.wait = lambda delay: True
        client.rpc = Mock()
        stream = Mock()
        stream.recv.side_effect = OSError("lost")
        client.follow = Mock(return_value=(stream, "stream", {"records": []}))
        with self.assertRaisesRegex(HarnessError, "已停止"):
            client.ask("session", "task")
        self.assertEqual(client.follow.call_count, 1)
        self.assertEqual(client.rpc.call_count, 1)

    def test_harness_follow_closes_socket_when_open_send_fails(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.authenticate = Mock()
        stream = Mock()
        stream.send.side_effect = OSError("send failed")
        with patch("harness_bridge.websocket.create_connection", return_value=stream):
            with self.assertRaises(OSError):
                client.follow("session")
        stream.close.assert_called_once()

    def test_harness_recovery_preserves_admission_turn_and_truncated_answer(self):
        import websocket
        from unittest.mock import Mock
        for replay_answer in (False, True):
            with self.subTest(replay_answer=replay_answer):
                client = HarnessClient()
                client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
                client.rpc = Mock()
                def event(seq, kind, data):
                    return {"type": "event", "event": {"seq": seq, "type": kind, "data": data}}
                start = event(10, "turn/start", {"turn": 3})
                admission = event(11, "user/message", {"source": {"rpcId": "request"}})
                answer = event(12, "assistant/message", {"turn": 3, "usage": {"total": 4},
                    "message": {"content": [{"type": "text", "text": "SAVED"}]}})
                end = event(14, "turn/end", {"turn": 3, "reason": {"kind": "completed"}})
                first, second = Mock(), Mock()
                first.recv.side_effect = [json.dumps({"streamId": "stream", "value": record})
                    for record in (start, admission, answer)] + [websocket.WebSocketConnectionClosedException("lost")]
                old_input = event(2, "user/message", {"source": {"kind": "user", "rpcId": "old"}})
                unrelated = event(13, "assistant/message", {"turn": 99,
                    "message": {"content": [{"type": "text", "text": "UNRELATED"}]}})
                records = [old_input, *([answer] if replay_answer else []), unrelated, end]
                client.follow = Mock(side_effect=[(first, "stream", {"records": []}),
                    (second, "stream", {"records": records})])
                progress = []
                with patch("harness_bridge.uuid.uuid4", return_value="request"):
                    self.assertEqual(client.ask("session", "task", progress=progress.append, timeout=1), "SAVED")
                self.assertEqual(progress, ["SAVED"])
                self.assertEqual(client.last_usage, [{"total": 4}])
                self.assertEqual(client.rpc.call_count, 1)

    def test_harness_recovery_rejects_new_foreign_user_input(self):
        import websocket
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        client.rpc = Mock()
        stream = Mock()
        stream.recv.side_effect = [json.dumps({"streamId": "stream", "value": {"type": "event", "event": {
            "seq": 9, "type": "turn/start", "data": {"turn": 3}}}}),
            json.dumps({"streamId": "stream", "value": {"type": "event", "event": {
            "seq": 10, "type": "user/message", "data": {"source": {"rpcId": "request"}}}}}),
            websocket.WebSocketConnectionClosedException("lost")]
        records = [{"type": "event", "event": {"seq": 11, "type": "user/message",
            "data": {"source": {"kind": "user", "rpcId": "other"}}}}]
        client.follow = Mock(side_effect=[(stream, "stream", {"records": []}),
            (Mock(), "stream", {"records": records})])
        with patch("harness_bridge.uuid.uuid4", return_value="request"):
            with self.assertRaisesRegex(RecoveryRequired, "其他输入"):
                client.ask("session", "task", timeout=1)
        self.assertEqual(client.rpc.call_count, 1)

    def test_harness_cannot_adopt_first_answer_or_later_start_as_request_turn(self):
        from unittest.mock import Mock
        for foreign_kind in ("assistant/message", "turn/start"):
            with self.subTest(foreign_kind=foreign_kind):
                client = HarnessClient()
                client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
                client.rpc = Mock()
                stream = Mock()
                events = [
                    {"type": "user/message", "data": {"source": {"rpcId": "request"}}},
                    {"type": foreign_kind, "data": {"turn": 99,
                        "message": {"content": [{"type": "text", "text": "FOREIGN"}]}}},
                    {"type": "turn/end", "data": {"turn": 99, "reason": {"kind": "completed"}}},
                ]
                stream.recv.side_effect = [json.dumps({"streamId": "stream", "value":
                    {"type": "event", "event": event}}) for event in events]
                client.follow = lambda sid: (stream, "stream", {"records": []})
                with patch("harness_bridge.uuid.uuid4", return_value="request"):
                    with self.assertRaisesRegex(RecoveryRequired, "关联.*sessionId=session requestId=request"):
                        client.ask("session", "task", timeout=1)
                self.assertEqual(client.rpc.call_count, 1)

    def test_harness_binds_admission_to_enclosing_host_turn_and_ignores_foreign_answer(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
        client.rpc = Mock()
        stream = Mock()
        events = [
            {"type": "turn/start", "data": {"turn": 3}},
            {"type": "user/message", "data": {"source": {"kind": "user", "rpcId": "request"}}},
            {"type": "assistant/message", "data": {"turn": 99,
                "message": {"content": [{"type": "text", "text": "FOREIGN"}]}}},
            {"type": "turn/end", "data": {"turn": 99, "reason": {"kind": "completed"}}},
            {"type": "assistant/message", "data": {"turn": 3,
                "message": {"content": [{"type": "text", "text": "RIGHT"}]}}},
            {"type": "turn/end", "data": {"turn": 3, "reason": {"kind": "completed"}}},
        ]
        stream.recv.side_effect = [json.dumps({"streamId": "stream", "value":
            {"type": "event", "event": event}}) for event in events]
        client.follow = lambda sid: (stream, "stream", {"records": []})
        with patch("harness_bridge.uuid.uuid4", return_value="request"):
            self.assertEqual(client.ask("session", "task", timeout=1), "RIGHT")
        self.assertEqual(client.rpc.call_count, 1)

    def test_harness_rejects_foreign_user_without_rpc_id_before_or_after_admission(self):
        from unittest.mock import Mock
        for admitted in (False, True):
            with self.subTest(admitted=admitted):
                client = HarnessClient()
                client.list_sessions = lambda: [{"sessionId": "session", "running": False}]
                client.rpc = Mock()
                events = [{"type": "turn/start", "data": {"turn": 3}}]
                if admitted:
                    events.append({"type": "user/message", "data": {"source": {"rpcId": "request"}}})
                events.append({"type": "user/message", "data": {"source": {"kind": "user"}}})
                stream = Mock()
                stream.recv.side_effect = [json.dumps({"streamId": "stream", "value":
                    {"type": "event", "event": event}}) for event in events]
                client.follow = lambda sid: (stream, "stream", {"records": []})
                with patch("harness_bridge.uuid.uuid4", return_value="request"):
                    with self.assertRaisesRegex(RecoveryRequired, "其他输入"):
                        client.ask("session", "task", timeout=1)
                self.assertEqual(client.rpc.call_count, 1)

    def test_harness_invalid_post_submit_stream_keeps_attention_and_identity(self):
        from unittest.mock import Mock
        client = HarnessClient()
        client.list_sessions = lambda: [{"sessionId": "original-session", "running": False}]
        client.rpc = Mock()
        stream = Mock()
        stream.recv.return_value = "invalid json"
        client.follow = lambda sid: (stream, "stream", {"records": []})
        with patch("harness_bridge.uuid.uuid4", return_value="original-request"):
            with self.assertRaisesRegex(RecoveryRequired, "original-session.*original-request"):
                client.ask("original-session", "task")
        self.assertEqual(client.rpc.call_count, 1)


class RuntimeTests(unittest.TestCase):
    def test_routing_never_sends_other_local_jobs_to_the_model(self):
        store = Store(":memory:")
        try:
            prior_job = store.create("PRIVATE: unrelated running task prompt")
            store.update(prior_job, codex_id="private-codex-session", harness_id="private-harness-session")
            current_job = store.create("current task")
            engine = Conductor(dict(DEFAULTS), store)
            engine.job_id = current_job
            engine.codex_models, engine.harness_models = C_MODELS, H_MODELS
            prompts = []

            def capture(prompt, *args):
                prompts.append(prompt)
                return {**copy.deepcopy(ROUTE), "mode": "codex"}

            engine._structured = capture
            engine.route("current task", {"mode": "codex"})
            self.assertEqual(len(prompts), 1)
            for private_value in ("PRIVATE: unrelated running task prompt", "private-codex-session", "private-harness-session"):
                self.assertNotIn(private_value, prompts[0])
        finally:
            store.conn.close()

    def test_structured_calls_persist_recoverable_thread_and_archive_only_success(self):
        from unittest.mock import Mock
        client = CodexClient()
        client.create = Mock(return_value="internal")
        client.ask = Mock(return_value='{"ok": true}')
        client.rpc = Mock()
        self.assertEqual(client.structured("route", {}, "model", "low", "."), {"ok": True})
        self.assertNotEqual(client.create.call_args.kwargs.get("ephemeral"), True)
        self.assertEqual(client.rpc.call_args.args, ("thread/archive", {"threadId": "internal"}))
        client.rpc.side_effect = TimeoutError("archive response lost")
        self.assertEqual(client.structured("route", {}, "model", "low", "."), {"ok": True})
        client.rpc.reset_mock()
        client.ask.side_effect = RecoveryRequired("uncertain original turn")
        with self.assertRaises(RecoveryRequired):
            client.structured("route", {}, "model", "low", ".")
        client.rpc.assert_not_called()

    def test_explicit_harness_effort_overrides_incompatible_saved_default(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models, engine.harness_models = C_MODELS, H_MODELS
        engine._structured = lambda *args: copy.deepcopy(ROUTE)
        route = engine.route("task", {"mode": "dual", "harness_effort": "high"})
        self.assertEqual(route["harness_effort"], "high")
        with self.assertRaises(ValueError):
            engine.route("task", {"mode": "dual", "harness_effort": "invalid"})

    def test_presets_and_explicit_overrides(self):
        fast = runtime_policy({"runtime_mode": "fast"}, DEFAULTS)
        self.assertEqual((fast["minutes"], fast["rounds"], fast["effort"]), (5, 1, "low"))
        deep = runtime_policy({}, {**DEFAULTS, "max_discussion_rounds": 18})
        self.assertEqual((deep["minutes"], deep["rounds"]), (0, 18))
        custom = runtime_policy({"runtime_mode": "balanced", "time_minutes": "7", "round_limit": "2"}, DEFAULTS)
        self.assertEqual((custom["minutes"], custom["rounds"]), (7, 2))

    def test_invalid_runtime_controls_are_rejected(self):
        for controls in ({"round_limit": 0}, {"time_minutes": -1}, {"round_limit": 51}, {"time_minutes": "1.5"}, {"runtime_mode": "unknown"}):
            with self.subTest(controls=controls), self.assertRaises(ValueError):
                runtime_policy(controls, DEFAULTS)

    def test_fast_auto_efforts_and_explicit_choices_take_precedence(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = {"gpt-6.1-sol": {"supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "high", "xhigh")]}}
        engine.harness_models = {("deepseek-official", "deepseek-flash"): {"reasoning": {"efforts": [{"id": value} for value in ("low", "high", "max")]}}}
        engine._structured = lambda *args: {**ROUTE, "use_astra": True}
        engine.runtime = runtime_policy({"runtime_mode": "fast"}, DEFAULTS)
        route = engine.route("task", {"mode": "dual"})
        self.assertEqual((route["codex_effort"], route["harness_effort"]), ("low", "low"))
        self.assertFalse(route["use_astra"])
        route = engine.route("task", {"mode": "dual", "effort": "xhigh", "harness_effort": "max"})
        self.assertEqual((route["codex_effort"], route["harness_effort"]), ("xhigh", "max"))
        route = engine.route("重要，必须审核", {"mode": "codex"})
        self.assertEqual(route["mode"], "dual")
        self.assertTrue(route["use_discussion"])


class RoutingRetryTests(unittest.TestCase):
    def test_harness_default_uses_configured_flash_catalog_name_and_max(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = C_MODELS
        engine.harness_models = {
            ("deepseek-official", "deepseek-flash"): {"name": "DeepSeek-V4.1-Flash", "reasoning": {"efforts": [{"id": "max"}]}},
            ("deepseek-official", "deepseek-v4-pro"): {"name": "DeepSeek-V4-Pro", "reasoning": {"efforts": [{"id": "max"}]}},
        }
        engine._structured = lambda *args: {**copy.deepcopy(ROUTE), "harness_model": "deepseek-v4-pro"}
        route = engine.route("important analysis", {"mode": "dual"})
        self.assertEqual(route["harness_model"], "deepseek-flash")
        self.assertEqual(route["harness_effort"], "max")
        self.assertEqual(route["harness_model_name"], "DeepSeek-V4.1-Flash")
        self.assertEqual(route["harness_selection_source"], "settings")
        manual = engine.route("important analysis", {"mode": "dual", "harness_model": "deepseek-official/deepseek-v4-pro"})
        self.assertEqual(manual["harness_model"], "deepseek-v4-pro")
        self.assertEqual(manual["harness_selection_source"], "manual")

    def test_recovery_required_never_triggers_channel_or_overload_fallback(self):
        from unittest.mock import Mock
        for full_access in (False, True):
            for message in ("No available channel for model gpt-6.1-sol", "servers overloaded"):
                with self.subTest(full_access=full_access, message=message):
                    engine = Conductor(dict(DEFAULTS), Store(":memory:"))
                    engine.codex_models = {**C_MODELS, "alternate": {}}
                    engine.codex.full_access = full_access
                    error = RecoveryRequired(message)
                    engine.codex.structured = Mock(side_effect=error)
                    self.assertFalse(engine._model_unavailable(error))
                    with self.assertRaises(RecoveryRequired):
                        engine._structured("route", {})
                    self.assertEqual(engine.codex.structured.call_count, 1)
                    self.assertEqual(engine.model_failures, {})

    def test_full_access_structured_overload_is_not_replayed(self):
        from unittest.mock import Mock
        from codex_bridge import ProviderBusy
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = C_MODELS
        engine.codex.full_access = True
        engine.codex.structured = Mock(side_effect=[ProviderBusy("servers overloaded"), {"ok": True}])
        with self.assertRaises(RecoveryRequired):
            engine._structured("route", {})
        self.assertEqual(engine.codex.structured.call_count, 1)

    def test_arbiter_prefers_frontier_description_to_version_number(self):
        models = {
            "gpt-6.1-sol": {"description": "Latest workhorse model for coding and everyday work."},
            "gpt-6-astra": {"description": "Frontier intelligence for the most demanding work."},
            "gpt-7-luna": {"description": "Fast and affordable model for easier tasks."},
        }
        self.assertEqual(highest_codex_model(models), "gpt-6-astra")

    def test_routing_uses_a_non_cooled_model(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = {**C_MODELS, "alternate": {"supportedReasoningEfforts": [{"reasoningEffort": "low"}]}}
        engine.model_failures["gpt-6.1-sol"] = time.time()
        engine.codex.structured = lambda prompt, schema, model, effort, cwd: {"model": model}
        self.assertEqual(engine._structured("route", {}), {"model": "alternate"})

    def test_routing_falls_back_after_explicit_channel_failure(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = {**C_MODELS, "alternate": {"supportedReasoningEfforts": [{"reasoningEffort": "low"}]}}
        def structured(prompt, schema, model, effort, cwd):
            if model == "gpt-6.1-sol":
                raise CodexError("No available channel for model gpt-6.1-sol")
            return {"model": model}
        engine.codex.structured = structured
        self.assertEqual(engine._structured("route", {}), {"model": "alternate"})
    def test_auto_uses_account_default_and_supported_effort(self):
        engine = Conductor({**DEFAULTS, "default_model": "auto"}, Store(":memory:"))
        engine.codex_models = {**C_MODELS, "account-default": {
            "isDefault": True, "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
        }}
        engine._align_default_model()
        self.assertEqual(engine.default_model, "account-default")
        self.assertEqual(engine.default_effort, "medium")
        self.assertEqual(engine.settings["default_model"], "auto")

    def test_auto_default_survives_model_catalog_replacement(self):
        engine = Conductor({**DEFAULTS, "default_model": "auto"}, Store(":memory:"))
        engine.codex_models = C_MODELS
        engine._align_default_model()
        engine.codex_models = {"replacement": {"isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}}
        engine._align_default_model()
        self.assertEqual(engine.default_model, "replacement")
        self.assertEqual(engine.settings["default_model"], "auto")

    def test_missing_custom_default_reports_configuration_problem(self):
        engine = Conductor({**DEFAULTS, "default_model": "missing"}, Store(":memory:"))
        engine.codex_models = C_MODELS
        with self.assertRaisesRegex(ValueError, "auto"):
            engine._align_default_model()

    def test_explicit_model_unavailability_is_temporarily_excluded(self):
        store = Store(":memory:")
        engine = Conductor(dict(DEFAULTS), store)
        engine.codex_models = {**C_MODELS, "gpt-6-luna": {}}
        jid = store.create("test")
        store.update(jid, status="failed", error="No available channel for model gpt-6-luna")
        self.assertEqual(list(engine.eligible_models()), ["gpt-6.1-sol"])

    def test_generic_overload_does_not_exclude_model(self):
        store = Store(":memory:")
        engine = Conductor(dict(DEFAULTS), store)
        engine.codex_models = C_MODELS
        jid = store.create("test")
        store.update(jid, status="failed", error="gpt-6.1-sol: servers overloaded")
        self.assertEqual(engine.eligible_models(), C_MODELS)

    def test_model_cooldown_uses_failure_time(self):
        store = Store(":memory:")
        engine = Conductor(dict(DEFAULTS), store)
        engine.codex_models = {**C_MODELS, "gpt-6-luna": {"supportedReasoningEfforts": [{"reasoningEffort": "low"}]}}
        jid = store.create("test")
        store.conn.execute("UPDATE jobs SET created=?, status='failed', error=?, failed_at=?", (time.time() - 3600, "No available channel for model gpt-6-luna", time.time()))
        store.conn.commit()
        self.assertNotIn("gpt-6-luna", engine.eligible_models())

    def test_routing_overload_retries_only_once(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = C_MODELS
        attempts = []
        def structured(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise CodexError("Our servers are currently overloaded")
            return {"ok": True}
        engine.codex.structured = structured
        engine.cancelled.wait = lambda timeout: False
        self.assertEqual(engine._structured("route", {}), {"ok": True})
        self.assertEqual(len(attempts), 2)

    def test_routing_authentication_error_is_not_retried(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex_models = C_MODELS
        attempts = []
        def structured(*args):
            attempts.append(args)
            raise CodexError("unauthorized")
        engine.codex.structured = structured
        with self.assertRaisesRegex(CodexError, "unauthorized"):
            engine._structured("route", {})
        self.assertEqual(len(attempts), 1)


class JudgmentTests(unittest.TestCase):
    def decision(self, **changes):
        return parse_judgment(json.dumps({"status": "continue", "response": "finding", "reason": "evidence",
                                         "issue_severity": "minor", "worthwhile": True,
                                         "next_step": "verify the calculation", **changes}))

    def test_major_issue_cannot_be_stopped_to_save_calls(self):
        result = self.decision(issue_severity="major", worthwhile=False, status="resolved")
        self.assertEqual(result["status"], "continue")

    def test_minor_issue_below_value_line_stops_honestly(self):
        result = self.decision(worthwhile=False)
        self.assertEqual((result["status"], result["stop_reason"]), ("resolved", "diminishing_returns"))
        self.assertEqual(result["issue_severity"], "minor")

    def test_cheap_useful_minor_improvement_continues(self):
        self.assertEqual(self.decision()["status"], "continue")

    def test_empty_next_step_cannot_drive_repeated_rounds(self):
        with self.assertRaisesRegex(ValueError, "核验步骤"):
            self.decision(next_step="")

    def test_prior_major_omission_cannot_finish_but_keeps_valid_improvement(self):
        previous = {"open_issues": [{"id": "BUG-1", "summary": "incorrect result", "severity": "major", "status": "open"}]}
        for action in ("improve", "peer_improve", "verify", "finish"):
            with self.subTest(action=action):
                current = {"status": "resolved", "response": "ok", "reason": "", "issue_severity": "none",
                    "worthwhile": False, "next_step": "repair the output", "next_action": action,
                    "progress": "improved", "open_issues": []}
                result = parse_judgment(json.dumps(current), previous)
                self.assertEqual((result["status"], result["issue_severity"]), ("continue", "major"))
                self.assertEqual(result["next_action"], "verify" if action == "finish" else action)
                self.assertEqual(result["progress"], "improved")
                self.assertIn("BUG-1", result["next_step"])
                self.assertEqual(previous["open_issues"][0]["status"], "open")

    def test_prior_major_requires_explicit_resolution_before_severity_drops(self):
        previous = {"open_issues": [{"id": "BUG-1", "summary": "bad output", "severity": "major", "status": "open"}]}
        for status, expected in (("open", "major"), ("resolved", "none")):
            with self.subTest(status=status):
                current = {"status": "resolved", "response": "checked", "reason": "", "issue_severity": "none",
                    "worthwhile": False, "next_step": "check the output", "next_action": "finish", "open_issues": [
                        {"id": "BUG-1", "summary": "updated", "severity": "minor", "status": status}]}
                self.assertEqual(parse_judgment(json.dumps(current), previous)["issue_severity"], expected)

    def test_invalid_issue_ledger_is_rejected(self):
        for issues in ([{}], "text", [{"id": "a", "summary": "bad", "severity": "major", "status": "unknown"}]):
            with self.subTest(issues=issues), self.assertRaises(ValueError):
                self.decision(open_issues=issues)

    def test_prior_major_cannot_hide_an_empty_blocked_reason(self):
        previous = {"open_issues": [{"id": "BUG-1", "summary": "bad output", "severity": "major", "status": "open"}]}
        current = {"status": "continue", "response": "", "reason": "", "issue_severity": "major",
            "worthwhile": True, "next_step": "", "next_action": "blocked", "open_issues": []}
        with self.assertRaisesRegex(ValueError, "阻塞裁决缺少具体原因"):
            parse_judgment(json.dumps(current), previous)


class JudgmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        self.addCleanup(self.engine.store.conn.close)
        self.engine.codex.full_access = True
        self.valid = json.dumps({"status": "resolved", "response": "Report complete; source flaws remain disclosed",
            "reason": "review-only task", "issue_severity": "none", "worthwhile": False,
            "next_step": "", "next_action": "finish", "open_issues": []})
        self.invalid = json.dumps({"status": "resolved", "response": "Report complete",
            "reason": "", "issue_severity": "none", "worthwhile": False, "next_step": "",
            "next_action": "finish", "open_issues": [{"id": "source-bug", "summary": "review finding",
                "severity": "major", "status": "open"}]})

    def test_valid_judgment_never_requests_another_model(self):
        def forbidden(*args):
            self.fail("Valid judgment was unnecessarily regenerated")
        self.assertEqual(self.engine._validated_judgment(self.valid, {}, forbidden)["status"], "resolved")
        self.assertTrue(self.engine.codex.full_access)

    def test_contradictory_scope_is_repaired_once_with_write_permission_disabled(self):
        calls = []
        approval = lambda *args: self.fail("Metadata correction requested user approval")
        self.engine.codex.approval = approval
        def repair(raw, error):
            self.assertFalse(self.engine.codex.full_access)
            self.assertIsNone(self.engine.codex.approval)
            self.assertEqual(raw, self.invalid)
            self.assertIn("核验步骤", error)
            calls.append(raw)
            return self.valid
        result = self.engine._validated_judgment(self.invalid, {}, repair)
        self.assertEqual((result["status"], result["next_action"]), ("resolved", "finish"))
        self.assertIn("source flaws", result["response"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.engine.codex.full_access)
        self.assertIs(self.engine.codex.approval, approval)

    def test_repair_cannot_hide_an_established_major_issue(self):
        previous = {"open_issues": [{"id": "actual-output-bug", "summary": "wrong result",
            "severity": "major", "status": "open"}]}
        result = self.engine._validated_judgment("not JSON", previous, lambda *args: self.valid)
        self.assertEqual((result["status"], result["issue_severity"]), ("continue", "major"))
        self.assertEqual(result["next_action"], "verify")
        self.assertIn("actual-output-bug", result["next_step"])

    def test_invalid_repair_stops_for_attention_and_restores_access(self):
        calls = []
        def repair(*args):
            calls.append(1)
            return self.invalid
        with self.assertRaises(RecoveryRequired):
            self.engine._validated_judgment(self.invalid, {}, repair)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.engine.codex.full_access)

    def test_stop_during_repair_is_propagated_and_access_restored(self):
        def stopped(*args):
            self.assertFalse(self.engine.codex.full_access)
            raise Stopped("stop")
        with self.assertRaises(Stopped):
            self.engine._validated_judgment(self.invalid, {}, stopped)
        self.assertTrue(self.engine.codex.full_access)


class AcceptanceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.task = "桌面许可受阻时，允许改用官方 API 完成保存复核，不计为桌面操作通过。"
        self.blocked = "SketchUp 桌面停在登录/许可欢迎页，未进入模型工作区；候选改后互审待外层调度。"
        self.initial = {"blocked": self.blocked, "evidence": "登录页截图；尚未互审", "checks": []}
        self.final = {"answer": "已完成 API 保存和同行复核", "blocked": "", "structured": True,
            "checks": [self.check("API 保存复核"), self.check("保存后同行复核")],
            "limitations": ["桌面登录受限，未验证 GUI"],
            "blocker_resolutions": [{"criterion": self.blocked, "evidence": "当前哈希与同行复核一致；API 读回成功",
                "required_checks": ["API 保存复核", "保存后同行复核"],
                "limitations": [{"description": "桌面登录受限，未验证 GUI", "task_quote": self.task}]}]}

    def check(self, criterion, status="passed", **extra):
        return {"criterion": criterion, "status": status, "evidence": "当前文件读回证据", "required": True, **extra}

    def gate(self, initial=None, final=None, task=None):
        from conductor_core import final_acceptance_gate, resolution_id
        return final_acceptance_gate(initial or self.initial, final or self.final, task=self.task if task is None else task,
            approved_resolutions={resolution_id(row) for row in self.final["blocker_resolutions"]})

    def test_job10_composite_block_closes_with_atomic_checks_and_authorized_limit(self):
        self.assertEqual(self.gate(), (True, []))

    def test_omitting_a_historical_block_still_does_not_close_it(self):
        self.final["blocker_resolutions"] = []
        self.assertFalse(self.gate()[0])

    def test_executor_cannot_approve_its_own_scope_change(self):
        from conductor_core import final_acceptance_gate
        self.assertFalse(final_acceptance_gate(self.initial, self.final, task=self.task)[0])

    def test_approval_is_tied_to_the_entire_exact_resolution(self):
        from conductor_core import final_acceptance_gate, resolution_id
        approved = {resolution_id(self.final["blocker_resolutions"][0])}
        self.final["blocker_resolutions"][0]["required_checks"] = []
        self.assertFalse(final_acceptance_gate(self.initial, self.final, task=self.task, approved_resolutions=approved)[0])

    def test_a_scope_quote_must_come_from_the_original_task(self):
        for task in ("必须实际进入桌面软件完成，不能用 API 替代", ""):
            with self.subTest(task=task):
                self.assertFalse(self.gate(task=task)[0])

    def test_unverified_or_missing_atomic_check_does_not_close_composite(self):
        for status in ("unverified", "failed"):
            self.final["checks"][1]["status"] = status
            self.assertFalse(self.gate()[0])
        self.final["checks"].pop()
        self.assertFalse(self.gate()[0])

    def test_same_record_conflicting_atomic_check_cannot_close_block(self):
        self.final["checks"].append(self.check("保存后同行复核", "unverified"))
        self.assertFalse(self.gate()[0])

    def test_resolution_cannot_waive_failed_check_even_with_user_scope_quote(self):
        self.initial["checks"] = [self.check(self.blocked, "failed")]
        self.assertFalse(self.gate()[0])

    def test_required_unverified_cannot_be_waived_or_dropped(self):
        self.initial = {"checks": [self.check(self.blocked, "unverified")]}
        self.assertFalse(self.gate()[0])

    def test_required_unverified_closes_with_fresh_direct_pass(self):
        from conductor_core import final_acceptance_gate
        initial = {"checks": [self.check("保存后同行复核", "unverified", evidence="尚未执行")]}
        self.assertTrue(final_acceptance_gate(initial, self.final)[0])

    def test_blank_or_reused_resolution_evidence_does_not_close_block(self):
        for evidence in (" ", self.initial["evidence"]):
            self.final["blocker_resolutions"][0]["evidence"] = evidence
            self.assertFalse(self.gate()[0])

    def test_resolution_cannot_erase_a_new_block_in_the_same_record(self):
        self.final["blocked"] = self.blocked
        self.assertFalse(self.gate()[0])

    def test_explicit_required_check_remains_pending_without_final_record(self):
        from conductor_core import pending_acceptance
        initial = {"checks": [self.check("同行复核", "unverified", evidence="未调度")]}
        self.assertEqual(pending_acceptance(initial)[0]["kind"], "unverified")

    def test_malformed_boundary_fields_fail_closed(self):
        from conductor_core import parse_final_execution
        for field, value in (("limitations", "text"), ("blocker_resolutions", [{}]),
                             ("checks", [self.check("a", required="false")])):
            with self.subTest(field=field):
                data = {**self.final, field: value}
                self.assertFalse(parse_final_execution(json.dumps(data))["structured"])

    def test_limitations_are_visible_in_final_ui(self):
        self.assertIn("未验证 GUI", presentation_text("Codex 汇总执行", json.dumps(self.final)))

    def test_direct_pass_cannot_bypass_scope_approval(self):
        from conductor_core import acceptance_resolutions, final_acceptance_gate
        final = {"structured": True, "answer": "Done", "blocked": "", "checks": [
            self.check(self.blocked, evidence="API成功且同行核查，但桌面仍不可用")]}
        self.assertFalse(final_acceptance_gate(self.initial, final, task=self.task)[0])
        self.assertTrue(acceptance_resolutions(self.initial, [], final)[0]["direct_pass"])

    def test_block_scope_survives_same_named_failure_and_requires_both_obligations(self):
        from conductor_core import acceptance_resolutions, final_acceptance_gate, resolution_id
        for kind in ("failed", "unverified"):
            with self.subTest(kind=kind):
                failed = {"checks": [self.check(self.blocked, kind, evidence="still pending")]}
                passed = {"structured": True, "answer": "done", "blocked": "", "checks": [
                    self.check(self.blocked, evidence="new full verification")]}
                for history in ([self.initial, failed], [failed, self.initial]):
                    self.assertFalse(final_acceptance_gate(None, passed, history)[0])
                    proposals = acceptance_resolutions(None, history, passed)
                    self.assertTrue(final_acceptance_gate(None, passed, history,
                        approved_resolutions={row["id"] for row in proposals})[0])
                approved = {resolution_id(row) for row in self.final["blocker_resolutions"]}
                self.assertFalse(final_acceptance_gate(None, self.final, [self.initial, failed],
                    task=self.task, approved_resolutions=approved)[0])
                completed = {**self.final, "checks": [*self.final["checks"], passed["checks"][0]]}
                self.assertTrue(final_acceptance_gate(None, completed, [self.initial, failed],
                    task=self.task, approved_resolutions=approved)[0])

    def test_disclosures_survive_omission_in_final_reply(self):
        from conductor_core import final_answer
        final = {"answer": "Done", "limitations": [], "blocker_resolutions": []}
        self.assertIn("未验证 GUI", final_answer(final, [self.final]))

    def test_final_disclosures_distinguish_current_limits_from_past_stage_status(self):
        from conductor_core import final_answer
        past = {"limitations": ["Word本轮尚未渲染", "GUI未验证"]}
        final = {"answer": "Word已渲染并核验一页", "limitations": ["GUI未验证"]}
        rendered = final_answer(final, [past])
        current, historical = rendered.split("历史阶段说明（不代表当前验收状态）：", 1)
        self.assertIn("GUI未验证", current)
        self.assertNotIn("尚未渲染", current)
        self.assertIn("尚未渲染", historical)
        self.assertEqual(rendered.count("GUI未验证"), 1)

    def test_resolution_cannot_reuse_an_earlier_atomic_check(self):
        self.initial["checks"] = [self.check("API 保存复核")]
        self.assertFalse(self.gate()[0])

    def test_new_records_require_boundary_fields_but_legacy_stays_readable(self):
        from conductor_core import parse_final_execution
        raw = json.dumps({"answer": "Done", "blocked": "", "checks": [
            {"criterion": "required work", "status": "unverified", "evidence": "not run"}]})
        self.assertTrue(parse_final_execution(raw)["structured"])
        self.assertFalse(parse_final_execution(raw, strict=True)["structured"])
        data = {"answer": "Done", "blocked": "", "limitations": [], "blocker_resolutions": [],
                "checks": [{"criterion": "required work", "status": "unverified", "evidence": "not run"}]}
        self.assertFalse(parse_final_execution(json.dumps(data), strict=True)["structured"])
        data["checks"][0]["required"] = True
        self.assertTrue(parse_final_execution(json.dumps(data), strict=True)["structured"])

    def test_new_action_rejects_omitted_fields_while_cached_action_remains_compatible(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        action = {"deliverable": "Saved candidate", "changes": "", "evidence": "", "blocked": "", "checks": []}
        with self.assertRaisesRegex(ValueError, "验收列表无效"):
            engine._accept_action(json.dumps(action), "new")
        self.assertEqual(engine._accept_action(json.dumps(action), "cached", replayed=True)["deliverable"], "Saved candidate")

    def test_legacy_unverified_is_pending_until_reclassified_or_verified(self):
        from conductor_core import final_acceptance_gate
        initial = {"checks": [{"criterion": "GUI", "status": "unverified", "evidence": "not run"}]}
        final = {"answer": "done", "blocked": "", "checks": [], "structured": True}
        self.assertFalse(final_acceptance_gate(initial, final)[0])
        final["checks"] = [self.check("GUI", "unverified", required=False)]
        self.assertFalse(final_acceptance_gate(initial, final)[0])

    def test_legacy_unknown_scope_can_be_explicitly_reviewed_without_waiving_required(self):
        from conductor_core import final_acceptance_gate, resolution_id
        initial = {"checks": [{"criterion": self.blocked, "status": "unverified", "evidence": "not run"}]}
        approved = {resolution_id(row) for row in self.final["blocker_resolutions"]}
        self.assertTrue(final_acceptance_gate(initial, self.final, task=self.task, approved_resolutions=approved)[0])
        initial["checks"][0]["required"] = True
        self.assertFalse(final_acceptance_gate(initial, self.final, task=self.task, approved_resolutions=approved)[0])

    def test_legacy_direct_pass_requires_independent_whole_criterion_review(self):
        from conductor_core import final_acceptance_gate, acceptance_resolutions
        initial = {"checks": [{"criterion": "GUI", "status": "unverified", "evidence": "not run"}]}
        final = {"answer": "done", "blocked": "", "checks": [self.check("GUI")], "structured": True}
        self.assertFalse(final_acceptance_gate(initial, final)[0])
        proposals = acceptance_resolutions(initial, [], final)
        self.assertTrue(final_acceptance_gate(initial, final, approved_resolutions={proposals[0]["id"]})[0])

    def test_legacy_record_cannot_downgrade_an_explicit_required_check(self):
        from conductor_core import pending_acceptance, final_acceptance_gate, resolution_id
        initial = {"checks": [self.check(self.blocked, "unverified", evidence="must verify")]}
        legacy = {"checks": [{"criterion": self.blocked, "status": "unverified", "evidence": "not run"}]}
        self.assertEqual(pending_acceptance(legacy, [initial])[0]["kind"], "unverified")
        approved = {resolution_id(row) for row in self.final["blocker_resolutions"]}
        self.assertFalse(final_acceptance_gate(legacy, self.final, [initial], task=self.task,
                                              approved_resolutions=approved)[0])

    def test_required_upgrade_survives_later_legacy_record_and_allows_actual_pass(self):
        from conductor_core import pending_acceptance, final_acceptance_gate
        legacy = {"checks": [{"criterion": "required work", "status": "unverified", "evidence": "not run"}]}
        required = {"checks": [self.check("required work", "unverified", evidence="explicit requirement")]}
        history = [legacy, required, legacy]
        self.assertEqual(pending_acceptance(history=history)[0]["kind"], "unverified")
        final = {"answer": "done", "blocked": "", "checks": [self.check("required work", evidence="actual new verification")],
                 "structured": True}
        self.assertTrue(final_acceptance_gate(None, final, history)[0])


class FakeCodex:
    def __init__(self, log, judge_statuses=None):
        self.full_access = False
        self.log, self.stop_event, self.last_usage = log, threading.Event(), {}
        self.judge_statuses = list(judge_statuses or ["resolved"])
        self.judge_calls = 0
        self.action_calls = 0

    def create(self, *args, **kwargs):
        return "codex-test"

    def archive_completed(self, thread_id):
        pass

    def ask(self, thread_id, text, *args, **kwargs):
        data = json.loads(text.split("\n", 1)[1])
        self.log.append(("codex", data))
        if data["phase"] == "independent":
            assert "peer_output" not in data
            return "C_FIRST"
        if data["phase"] == "peer_review":
            assert data["peer_output"] == "H_FIRST"
            return json.dumps({"review": "C_REVIEW", "needs_astra": False, "reason": ""})
        if data["phase"] == "discussion":
            return json.dumps({"status": "resolved", "response": "C_DISCUSSION_RESOLVED", "reason": ""})
        if data["phase"] == "discussion_judge":
            status = self.judge_statuses[min(self.judge_calls, len(self.judge_statuses) - 1)]
            self.judge_calls += 1
            return json.dumps({"status": status, "response": "JUDGE_" + status, "reason": "",
                               "issue_severity": "none" if status == "resolved" else "major",
                               "next_action": "verify" if status == "escalate" else "discuss",
                               "progress": "new_evidence", "open_issues": [],
                               "worthwhile": status != "resolved", "next_step": "verify disputed evidence" if status != "resolved" else ""})
        if data["phase"] == "acceptance_scope_review":
            return json.dumps({"approved_ids": [row["id"] for row in data["resolutions"]], "reason": "controlled test approval"})
        if data["phase"] in ("deliverable_build", "deliverable_improve", "discussion_evidence"):
            self.action_calls += 1
            return json.dumps(current_acceptance({"deliverable": "CANDIDATE", "changes": "draft", "evidence": "checked", "blocked": "", "checks": [],
                "evidence_records": [{"claim": "fixture evidence", "source_type": "test",
                    "source_ref": "fixture://action/" + str(self.action_calls), "excerpt": "fixture",
                    "independence_key": "fixture://action/" + str(self.action_calls),
                    "verification": "reproduced", "relation": "supports"}]}))
        if data["phase"] == "final_execution" and kwargs.get("schema") == FINAL_EXECUTION_SCHEMA:
            return json.dumps(current_acceptance({"answer": "FINAL", "blocked": "", "checks": []}))
        return "FINAL"


class FakeHarness:
    def __init__(self, log):
        self.log, self.cancel_event, self.last_usage = log, threading.Event(), []

    def create(self, *args):
        return "harness-test"

    def ask(self, session_id, text, *args, **kwargs):
        data = json.loads(text.split("\n", 1)[1])
        self.log.append(("harness", data))
        if data["phase"] == "independent":
            assert "C_FIRST" not in text
            assert "peer_output" not in data
            return "H_FIRST"
        if data["phase"] == "discussion":
            return json.dumps({"status": "resolved", "response": "H_DISCUSSION_RESOLVED", "reason": ""})
        assert data["peer_output"] == "C_FIRST"
        return "H_REVIEW"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_data.cleanup)
        data_patch = patch("conductor_core.DATA_DIR", Path(self.temp_data.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.workspace = patch.dict(DEFAULTS, {"workspace": str(Path(__file__).resolve().parent)})
        self.workspace.start()
        self.addCleanup(self.workspace.stop)

    def discussion_engine(self, log, statuses):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex, engine.harness = FakeCodex(log, statuses), FakeHarness(log)
        engine.codex_models = C_MODELS
        engine.refresh = lambda: ""
        engine.route = lambda *args: {**copy.deepcopy(ROUTE), "use_discussion": True}
        return engine

    def test_inconsistent_judgment_repairs_metadata_without_replaying_work(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.codex.full_access = True
        original = engine.codex.ask
        repairs = []
        def ask(thread, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "discussion_judge":
                log.append(("codex", data))
                return json.dumps({"status":"resolved", "response":"report complete", "reason":"",
                    "issue_severity":"none", "worthwhile":False, "next_step":"", "next_action":"finish",
                    "open_issues":[{"id":"source-finding", "summary":"reviewed code has a bug",
                                    "severity":"major", "status":"open"}]})
            if data["phase"] == "judgment_repair":
                repairs.append(data)
                self.assertTrue(kwargs["readonly"])
                self.assertFalse(engine.codex.full_access)
                self.assertIsNone(engine.codex.approval)
                return json.dumps({"status":"resolved", "response":"JUDGE_resolved", "reason":"scope corrected",
                    "issue_severity":"none", "worthwhile":False, "next_step":"", "next_action":"finish",
                    "open_issues":[]})
            return original(thread, text, *args, **kwargs)
        engine.codex.ask = ask
        self.assertEqual(engine.run("Produce a review report only"), "FINAL")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(sum(1 for who,data in log if who=="codex" and data["phase"]=="independent"), 1)
        self.assertEqual(sum(1 for who,data in log if who=="codex" and data["phase"]=="deliverable_build"), 1)
        self.assertEqual(sum(1 for who,data in log if who=="codex" and data["phase"]=="final_execution"), 1)
        self.assertTrue(engine.codex.full_access)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_helper_runs_alongside_candidate_and_evidence_reaches_both_reviewers(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.route = lambda *args: {**copy.deepcopy(ROUTE), "use_discussion": True,
                                     "harness_helper_task": "Independently check the supplied table sum"}
        candidate_started, helper_started = threading.Event(), threading.Event()
        original_c, original_h = engine.codex.ask, engine.harness.ask
        def codex_ask(thread, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "deliverable_build":
                candidate_started.set()
                self.assertTrue(helper_started.wait(2), "helper did not run in parallel")
            return original_c(thread, text, *args, **kwargs)
        def harness_ask(session, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "read_only_helper":
                self.assertTrue(kwargs["readonly"])
                helper_started.set()
                self.assertTrue(candidate_started.wait(2))
                log.append(("harness", data))
                self.assertIn("只读", data["instruction"])
                return "INDEPENDENT SUM = 378"
            return original_h(session, text, *args, **kwargs)
        engine.codex.ask, engine.harness.ask = codex_ask, harness_ask
        self.assertEqual(engine.run("Check the supplied immutable input and write a recommendation"), "FINAL")
        discussions = [data for _, data in log if data["phase"] == "discussion"]
        self.assertEqual(len(discussions), 2)
        self.assertTrue(all("INDEPENDENT SUM = 378" in data["helper_evidence"] for data in discussions))
        self.assertEqual(sum(row["stage"] == "Harness 并行核证" for row in engine.store.steps(engine.job_id)), 1)

    def test_no_helper_is_started_when_work_cannot_be_split(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.run("simple recommendation")
        self.assertFalse(any(data["phase"] == "read_only_helper" for _, data in log))

    def test_helper_failure_preserves_candidate_and_still_delivers(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.route = lambda *args: {**copy.deepcopy(ROUTE), "use_discussion": True, "harness_helper_task": "check a sum"}
        original = engine.harness.ask
        def ask(session, text, *args, **kwargs):
            if json.loads(text.split("\n", 1)[1])["phase"] == "read_only_helper":
                raise HarnessError("helper unavailable")
            return original(session, text, *args, **kwargs)
        engine.harness.ask = ask
        self.assertEqual(engine.run("calculate and deliver"), "FINAL")
        self.assertTrue((engine.artifacts / "revision-000.md").is_file())
        self.assertTrue((engine.artifacts / "actions.json").is_file())
        self.assertIn("helper unavailable", (engine.artifacts / "helper-evidence.md").read_text(encoding="utf-8"))
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_helper_evidence_survives_improvement_and_final_execution(self):
        log = []
        engine = self.discussion_engine(log, ["escalate", "resolved"])
        engine.route = lambda *args: {**copy.deepcopy(ROUTE), "use_discussion": True, "harness_helper_task": "check source"}
        original = engine.harness.ask
        def ask(session, text, *args, **kwargs):
            if json.loads(text.split("\n", 1)[1])["phase"] == "read_only_helper":
                return "PERSISTENT HELPER PROOF"
            return original(session, text, *args, **kwargs)
        engine.harness.ask = ask
        engine.run("important research")
        stages = [data for _, data in log if data["phase"] in ("discussion", "discussion_evidence", "final_execution")]
        self.assertGreater(len(stages), 4)
        self.assertTrue(all(data.get("helper_evidence") == "PERSISTENT HELPER PROOF" for data in stages))

    def completion_engine(self, initial, final, repair=False):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        target = Path(self.temp_data.name) / "completion-answer.txt"
        original = engine.codex.ask
        def ask(thread_id, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "deliverable_build":
                target.write_text("379", encoding="utf-8")
                return json.dumps(current_acceptance({**json.loads(original(thread_id, text, *args, **kwargs)), **initial}))
            if data["phase"] == "final_execution":
                self.assertEqual(kwargs.get("schema"), FINAL_EXECUTION_SCHEMA)
                original(thread_id, text, *args, **kwargs)
                if repair:
                    target.write_text("378", encoding="utf-8")
                return final if isinstance(final, str) else json.dumps(current_acceptance(final))
            return original(thread_id, text, *args, **kwargs)
        engine.codex.ask = ask
        return engine, target, log

    def test_codex_only_final_reply_is_structured_and_readable(self):
        engine = self.discussion_engine([], ["resolved"])
        engine.route = lambda *unused: {**copy.deepcopy(ROUTE), "mode": "codex"}
        self.assertEqual(engine.run("ordinary task"), "FINAL")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")
        self.assertEqual((engine.artifacts / "final.md").read_text(encoding="utf-8"), "FINAL")
        record = json.loads((engine.artifacts / "final_acceptance.json").read_text(encoding="utf-8"))
        self.assertTrue(record["accepted"])
        self.assertIn('"answer"', engine.store.messages(engine.job_id)[-1]["text"])
        self.assertNotIn('"answer"', engine.store.export_markdown(engine.job_id))

    def test_rejected_scope_review_cannot_complete_blocked_failed_pass_history(self):
        fixture = AcceptanceBoundaryTests()
        fixture.setUp()
        engine, _, log = self.completion_engine(fixture.initial, {
            "answer": "Done", "blocked": "", "checks": [fixture.check(fixture.blocked, evidence="final proof")]})
        engine.codex.judge_statuses = ["continue", "resolved"]
        original = engine.codex.ask
        def ask(thread_id, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "acceptance_scope_review":
                log.append(("codex", data))
                return json.dumps({"approved_ids": [], "reason": "whole criterion remains unverified"})
            raw = original(thread_id, text, *args, **kwargs)
            if data["phase"] == "deliverable_improve":
                action = json.loads(raw)
                action["checks"] = [fixture.check(fixture.blocked, "failed", evidence="still failed")]
                return json.dumps(action)
            return raw
        engine.codex.ask = ask
        engine.run(fixture.task)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
        record = json.loads((engine.artifacts / "final_acceptance.json").read_text(encoding="utf-8"))
        self.assertFalse(record["accepted"])
        self.assertTrue(any(row["criterion"] == fixture.blocked and row["kind"] == "blocked" for row in record["pending"]))
        self.assertEqual(sum(data["phase"] == "acceptance_scope_review" for _, data in log), 1)

    def test_judgment_threads_are_recoverable_and_successfully_archived(self):
        from unittest.mock import Mock
        engine = self.discussion_engine([], ["resolved"])
        engine.codex.create = Mock(return_value="test-thread")
        engine.codex.archive_completed = Mock()
        engine.run("task")
        creates = engine.codex.create.call_args_list
        internal = [call for call in creates if len(call.args) > 2 and call.args[2].startswith("Conductor internal:")]
        self.assertTrue(internal)
        self.assertTrue(all(call.kwargs.get("ephemeral") is not True for call in internal))
        self.assertEqual(engine.codex.archive_completed.call_count, len(internal))

    def test_scope_split_requires_separate_judge_and_retains_disclosed_limit(self):
        from conductor_core import resolution_id
        fixture = AcceptanceBoundaryTests()
        fixture.setUp()
        for approve in (False, True):
            with self.subTest(approve=approve):
                engine, _, log = self.completion_engine(fixture.initial, fixture.final)
                original = engine.codex.ask
                def ask(thread, text, *args, **kwargs):
                    data = json.loads(text.split("\n", 1)[1])
                    if data["phase"] == "acceptance_scope_review":
                        log.append(("codex", data))
                        return json.dumps({"approved_ids": [resolution_id(fixture.final["blocker_resolutions"][0])] if approve else [],
                                           "reason": "approved" if approve else "insufficient evidence"})
                    return original(thread, text, *args, **kwargs)
                engine.codex.ask = ask
                answer = engine.run(fixture.task)
                self.assertEqual(engine.store.job(engine.job_id)["status"], "completed" if approve else "needs_attention")
                self.assertIn("未验证 GUI", answer)
                self.assertEqual(sum(data["phase"] == "acceptance_scope_review" for _, data in log), 1)
                self.assertTrue((engine.artifacts / "scope_review.json").is_file())

    def test_historical_limit_is_retained_in_final_artifact_ui_notice_and_export(self):
        initial = {"limitations": ["专业软件界面未验证，已按授权使用 API"]}
        final = {"answer": "Done", "blocked": "", "checks": []}
        engine, _, _ = self.completion_engine(initial, final)
        answer = engine.run("task")
        self.assertIn(initial["limitations"][0], answer)
        self.assertIn(initial["limitations"][0], (engine.artifacts / "final.md").read_text(encoding="utf-8"))
        self.assertIn(initial["limitations"][0], engine.store.export_markdown(engine.job_id))
        self.assertTrue(any(row["stage"] == "交付限制" for row in engine.store.messages(engine.job_id)))

    def test_acknowledged_readonly_overload_continues_same_session_once(self):
        from codex_bridge import ProviderBusy
        engine = self.discussion_engine([], ["resolved"])
        original = engine.codex.ask
        attempts = []
        def ask(thread, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "independent":
                attempts.append((thread, data))
                if len(attempts) == 1:
                    raise ProviderBusy("servers overloaded")
            return original(thread, text, *args, **kwargs)
        engine.codex.ask = ask
        engine.cancelled.wait = lambda unused: False
        self.assertEqual(engine.run("task"), "FINAL")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempts[1][0])
        self.assertIn("continuation_instruction", attempts[1][1])

    def test_provider_overload_in_write_phase_is_not_replayed(self):
        from codex_bridge import ProviderBusy
        engine = self.discussion_engine([], ["resolved"])
        original = engine.codex.ask
        attempts = []
        def ask(thread, text, *args, **kwargs):
            if json.loads(text.split("\n", 1)[1])["phase"] == "deliverable_build":
                attempts.append(1)
                raise ProviderBusy("servers overloaded")
            return original(thread, text, *args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine.run("task")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_full_access_readonly_overload_preserves_progress_without_replay(self):
        from codex_bridge import ProviderBusy
        engine = self.discussion_engine([], ["resolved"])
        engine.codex.full_access = True
        attempts = []
        def ask(*args, **kwargs):
            attempts.append(1)
            raise ProviderBusy("servers overloaded")
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine.run("task")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_readonly_overload_continuation_is_bounded(self):
        from codex_bridge import ProviderBusy
        engine = self.discussion_engine([], ["resolved"])
        attempts = []
        def ask(*args, **kwargs):
            attempts.append(1)
            raise ProviderBusy("servers overloaded")
        engine.codex.ask = ask
        engine.cancelled.wait = lambda unused: False
        with self.assertRaises(RecoveryRequired):
            engine.run("task")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_dual_without_discussion_can_finish_without_a_candidate(self):
        engine = self.discussion_engine([], ["resolved"])
        engine.route = lambda *unused: {**copy.deepcopy(ROUTE), "use_discussion": False}
        self.assertEqual(engine.run("ordinary task"), "FINAL")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_unrepaired_failed_or_blocked_action_cannot_finish(self):
        cases = [{"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]},
                 {"blocked": "calculation cannot be verified"}]
        for initial in cases:
            with self.subTest(initial=initial):
                engine, target, _ = self.completion_engine(initial, {"answer": "Done", "blocked": "", "checks": []})
                result = engine.run("task")
                self.assertEqual(target.read_text(), "379")
                self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
                self.assertIn("待处理", result)
                self.assertTrue(json.loads((engine.artifacts / "final_acceptance.json").read_text(encoding="utf-8"))["missing"])

    def test_actual_repair_with_matching_fresh_evidence_can_finish(self):
        for initial in ({"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]},
                        {"blocked": "calculation"}):
            with self.subTest(initial=initial):
                final = {"answer": "Corrected", "blocked": "", "checks": [
                    {"criterion": "calculation", "status": "passed", "evidence": "read-back actual=378"}]}
                engine, target, _ = self.completion_engine(initial, final, repair=True)
                self.assertEqual(engine.run("task"), "Corrected")
                self.assertEqual(target.read_text(), "378")
                self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_unverified_only_does_not_block_completion(self):
        initial = {"checks": [{"criterion": "external service", "status": "unverified", "evidence": "not run"}]}
        engine, _, _ = self.completion_engine(initial, {"answer": "Bounded result", "blocked": "", "checks": initial["checks"]})
        engine.run("task")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_fresh_failures_or_blocks_cannot_hide_behind_a_prior_pass(self):
        initial = {"checks": [{"criterion": "draft", "status": "passed", "evidence": "checked"}]}
        cases = [{"answer": "Missing output", "blocked": "output missing", "checks": []},
                 {"answer": "Failed conversion", "blocked": "", "checks": [
                     {"criterion": "conversion", "status": "failed", "evidence": "exit=1"}]}]
        for final in cases:
            with self.subTest(final=final):
                engine, _, _ = self.completion_engine(initial, final)
                engine.run("task")
                self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_blank_wrong_or_unverified_evidence_does_not_clear_failure(self):
        initial = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]}
        cases = [{"criterion": "other", "status": "passed", "evidence": "checked"},
                 {"criterion": "calculation", "status": "passed", "evidence": " "},
                 {"criterion": "calculation", "status": "unverified", "evidence": "not run"}]
        for check in cases:
            with self.subTest(check=check):
                engine, _, _ = self.completion_engine(initial, {"answer": "Not verified", "blocked": "", "checks": [check]})
                engine.run("task")
                self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_malformed_final_reply_is_preserved_without_automatic_replay(self):
        for raw in ("FINAL", '{"answer":"Done","blocked":"","checks":{}}'):
            with self.subTest(raw=raw):
                engine, _, log = self.completion_engine({}, raw)
                engine.run("task")
                self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
                self.assertEqual(sum(data["phase"] == "final_execution" for _, data in log), 1)
                self.assertFalse(json.loads((engine.artifacts / "final_acceptance.json").read_text(encoding="utf-8"))["accepted"])

    def test_new_malformed_cached_reply_cannot_be_downgraded_to_legacy(self):
        engine, _, log = self.completion_engine({}, "INVALID_NEW_REPLY")
        save = engine._save_artifact
        def stop_before_final(name, text):
            if name == "final.md":
                raise OSError("simulated interruption before final artifact")
            return save(name, text)
        engine._save_artifact = stop_before_final
        with self.assertRaises(OSError):
            engine.run("task")
        before = len(log)
        engine._save_artifact = save
        engine.codex.resume = lambda cid: None
        engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
        engine.resume_run(engine.job_id)
        self.assertEqual(len(log), before)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_legacy_cached_final_reply_remains_compatible(self):
        engine, _, log = self.completion_engine({}, "LEGACY_FINAL")
        save = engine._save_artifact
        def stop_before_final(name, text):
            if name == "final.md":
                raise OSError("simulated old-version interruption")
            return save(name, text)
        engine._save_artifact = stop_before_final
        with self.assertRaises(OSError):
            engine.run("task")
        route = json.loads(engine.store.job(engine.job_id)["route"])
        route.pop("final_reply_schema")
        engine.store.update(engine.job_id, route=json.dumps(route))
        before = len(log)
        engine._save_artifact = save
        engine.codex.resume = lambda cid: None
        engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
        self.assertEqual(engine.resume_run(engine.job_id), "LEGACY_FINAL")
        self.assertEqual(len(log), before)
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_final_presentation_preserves_legacy_and_nonfinal_json(self):
        raw = json.dumps({"answer": "Readable answer", "blocked": "", "checks": []})
        self.assertEqual(presentation_text("Codex 汇总执行", raw), "Readable answer")
        self.assertEqual(presentation_text("Codex 独立分析", raw), raw)
        self.assertEqual(presentation_text("Codex 执行", "LEGACY"), "LEGACY")

    def history_engine(self, initial, correction=None, final=None, repair_in_action=False):
        log = []
        engine = self.discussion_engine(log, ["continue", "resolved"])
        target = Path(self.temp_data.name) / "historical-answer.txt"
        original = engine.codex.ask
        def ask(thread_id, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            raw = original(thread_id, text, *args, **kwargs)
            if data["phase"] == "discussion_judge":
                judgment = json.loads(raw)
                if judgment["status"] != "resolved":
                    judgment["next_action"] = "improve"
                return json.dumps(judgment)
            if data["phase"] == "deliverable_build":
                target.write_text("379", encoding="utf-8")
                return json.dumps(current_acceptance({**json.loads(raw), **initial}))
            if data["phase"] == "deliverable_improve":
                if repair_in_action:
                    target.write_text("378", encoding="utf-8")
                return json.dumps(current_acceptance({**json.loads(raw), **(correction or {})}))
            if data["phase"] == "final_execution" and final is not None:
                return json.dumps(current_acceptance(final))
            return raw
        engine.codex.ask = ask
        return engine, target, log

    def test_historical_failure_cannot_disappear_when_later_action_omits_it(self):
        initial = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]}
        engine, target, log = self.history_engine(initial)
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
        final = next(data for _, data in log if data["phase"] == "final_execution")
        self.assertIn("calculation", final["required_acceptance"])
        reviews = [data for _, data in log if data["phase"] in ("discussion", "discussion_judge")]
        self.assertTrue(all("calculation" in data["required_acceptance"] for data in reviews))
        improvement = next(data for _, data in log if data["phase"] == "deliverable_improve")
        self.assertIn("calculation", improvement["required_acceptance"])

    def test_historical_block_cannot_disappear_when_later_action_omits_it(self):
        engine, target, _ = self.history_engine({"blocked": "readback unavailable"})
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_later_action_can_repair_and_revalidate_historical_failure(self):
        initial = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]}
        correction = {"checks": [{"criterion": "calculation", "status": "passed", "evidence": "readback actual=378"}]}
        engine, target, _ = self.history_engine(initial, correction, repair_in_action=True)
        engine.run("task")
        self.assertEqual(target.read_text(), "378")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_stale_action_evidence_cannot_clear_historical_failure(self):
        initial = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]}
        correction = {"checks": [{"criterion": "calculation", "status": "passed", "evidence": "actual=379"}]}
        engine, target, _ = self.history_engine(initial, correction)
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_reintroduced_failure_after_a_pass_still_blocks(self):
        initial = {"checks": [{"criterion": "calculation", "status": "passed", "evidence": "old pass"}]}
        correction = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "actual=379"}]}
        engine, target, _ = self.history_engine(initial, correction)
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_old_pass_cannot_clear_a_later_failure_in_final_reply(self):
        initial = {"checks": [{"criterion": "calculation", "status": "passed", "evidence": "old readback=378"}]}
        correction = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "current readback=379"}]}
        final = {"answer": "done", "blocked": "", "checks": initial["checks"]}
        engine, target, _ = self.history_engine(initial, correction, final)
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")

    def test_old_pass_cannot_clear_a_later_failure_in_action_history(self):
        from conductor_core import final_acceptance_gate
        passed = {"checks": [{"criterion": "calculation", "status": "passed", "evidence": "old readback=378"}]}
        failed = {"checks": [{"criterion": "calculation", "status": "failed", "evidence": "current readback=379"}]}
        accepted, missing = final_acceptance_gate(passed,
            {"answer": "done", "blocked": "", "checks": [], "structured": True}, [passed, failed, passed])
        self.assertFalse(accepted)
        self.assertIn("calculation", missing)

    def test_unnamed_failures_retain_distinct_sources(self):
        initial = {"checks": [{"criterion": "", "status": "failed", "evidence": "first file is wrong"}]}
        correction = {"checks": [{"criterion": "  ", "status": "failed", "evidence": "second file is wrong"}]}
        engine, target, _ = self.history_engine(initial, correction)
        engine.run("task")
        self.assertEqual(target.read_text(), "379")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
        acceptance = json.loads((engine.artifacts / "final_acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(len(acceptance["missing"]), 2)
        self.assertEqual({row["source"] for row in acceptance["pending"]},
                         {"actions[0].checks[0]", "actions[1].checks[0]"})

    def test_resume_reuses_finished_work_and_reviews_saved_candidate(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        original = engine.codex.ask
        failures = [True]
        def ask(*args, **kwargs):
            phase = json.loads(args[1].split("\n", 1)[1])["phase"]
            if phase == "discussion" and failures:
                failures.pop()
                raise RecoveryRequired("interrupted readonly review")
            return original(*args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("important task")
        job_id = engine.job_id
        revision = engine.artifacts / "revision-000.md"
        before = (revision.read_bytes(), revision.stat().st_mtime_ns)
        engine.codex.resume = lambda cid: None
        engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
        engine.route = lambda *args: self.fail("resume must keep the original route")
        engine.resume_run(job_id)
        self.assertEqual(engine.job_id, job_id)
        self.assertEqual(len(engine.store.recent()), 1)
        self.assertEqual(engine.store.job(job_id)["status"], "completed")
        self.assertEqual((revision.read_bytes(), revision.stat().st_mtime_ns), before)
        phases = [(speaker, data["phase"]) for speaker, data in log]
        for key in (("codex", "independent"), ("harness", "independent"), ("codex", "deliverable_build"), ("harness", "discussion")):
            self.assertEqual(phases.count(key), 1)
        self.assertIn(("codex", "discussion_judge"), phases)

    def test_resume_workspace_conflict_is_marked_for_attention(self):
        engine = self.discussion_engine([], ["resolved"])
        store = engine.store
        job_id = store.create("resume an older dual-agent task")
        saved_workspace = Path(self.temp_data.name) / "saved-workspace"
        saved_workspace.mkdir()
        codex_workspace = Path(self.temp_data.name) / "codex-workspace"
        harness_workspace = Path(self.temp_data.name) / "harness-workspace"
        route = {
            "mode": "dual",
            "workspace": str(saved_workspace),
            "codex_thread": "codex-session",
            "harness_session": "harness-session",
        }
        store.update(job_id, status="interrupted", route=json.dumps(route),
                     codex_id="codex-session", harness_id="harness-session")
        artifacts = Path(self.temp_data.name) / "tasks" / str(job_id)
        artifacts.mkdir(parents=True)
        (artifacts / "run-context.json").write_text(json.dumps({"facts": "", "controls": {}}), encoding="utf-8")
        engine.codex_rows = [{"id": "codex-session", "cwd": str(codex_workspace)}]
        engine.harness_rows = [{"sessionId": "harness-session", "cwd": str(harness_workspace)}]

        with self.assertRaisesRegex(RecoveryRequired, "工作目录与所选对话不一致"):
            engine.resume_run(job_id)

        self.assertEqual(store.job(job_id)["status"], "needs_attention")

    def test_resume_never_replays_unknown_write(self):
        engine = self.discussion_engine([], ["resolved"])
        original = engine.codex.ask
        def ask(*args, **kwargs):
            if json.loads(args[1].split("\n", 1)[1])["phase"] == "deliverable_build":
                raise RecoveryRequired("unknown file changes")
            return original(*args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("task")
        with self.assertRaisesRegex(RecoveryRequired, "修改过文件"):
            engine._resume_checkpoint(engine.job_id)

    def test_completed_write_without_saved_reply_is_not_replayed(self):
        engine = self.discussion_engine([], ["resolved"])
        engine._run("task")
        (engine.artifacts / "final.md").unlink()
        engine.store.update(engine.job_id, status="interrupted")
        engine.store.conn.execute("DELETE FROM job_messages WHERE stage='Codex 形成候选成果'")
        engine.store.conn.commit()
        with self.assertRaisesRegex(RecoveryRequired, "修改过文件"):
            engine._resume_checkpoint(engine.job_id)

    def test_resume_does_not_resubmit_when_original_peer_is_busy(self):
        engine = self.discussion_engine([], ["resolved"])
        original = engine.codex.ask
        def ask(*args, **kwargs):
            if json.loads(args[1].split("\n", 1)[1])["phase"] == "discussion":
                raise RecoveryRequired("review interrupted")
            return original(*args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("task")
        engine.codex.resume = lambda cid: None
        engine.codex.ask = lambda *args, **kwargs: self.fail("must check busy peer before submitting")
        engine.harness_rows = [{"sessionId": "harness-test", "running": True}]
        with self.assertRaisesRegex(RecoveryRequired, "仍在运行"):
            engine.resume_run(engine.job_id)

    def test_completed_deliverable_cannot_be_replayed_as_resume(self):
        engine = self.discussion_engine([], ["resolved"])
        engine._run("task")
        engine.store.update(engine.job_id, status="needs_attention")
        with self.assertRaisesRegex(RecoveryRequired, "最终交付"):
            engine._resume_checkpoint(engine.job_id)

    def test_keep_awake_request_is_released_on_failure(self):
        from unittest.mock import Mock
        engine = self.discussion_engine([], ["resolved"])
        request = Mock(return_value=1)
        with patch("ctypes.windll.kernel32.SetThreadExecutionState", request):
            with self.assertRaises(ValueError):
                with engine._keep_awake():
                    raise ValueError("test")
        self.assertEqual([call.args[0] for call in request.call_args_list], [0x80000001, 0x80000000])

    def editing_engine(self, log):
        engine = self.discussion_engine(log, ["continue", "resolved"])
        # Windows CI may expose the temp directory through an 8.3 path alias.
        target = Path(self.temp_data.name).resolve() / "shared-draft.txt"
        target.write_text("Codex draft", encoding="utf-8")
        engine.route = lambda *args: {**copy.deepcopy(ROUTE), "use_discussion": True, "peer_edit": True}
        codex_ask, harness_ask = engine.codex.ask, engine.harness.ask
        def ask_codex(*args, **kwargs):
            data = json.loads(args[1].split("\n", 1)[1])
            raw = codex_ask(*args, **kwargs)
            if data["phase"] == "discussion_judge" and engine.codex.judge_calls == 1:
                decision = json.loads(raw)
                decision.update(next_action="peer_improve", edit_targets=[str(target)], next_step="Improve the saved draft")
                return json.dumps(decision)
            if data["phase"] == "deliverable_build":
                action = json.loads(raw)
                action["files"] = [str(target)]
                return json.dumps(action)
            if data["phase"] == "discussion_evidence" and data.get("handoff_record"):
                self.assertEqual(target.read_text(encoding="utf-8"), "Peer improved draft")
                self.assertTrue(kwargs["readonly"])
            return raw
        def ask_harness(*args, **kwargs):
            data = json.loads(args[1].split("\n", 1)[1])
            if data["phase"] != "peer_revision":
                return harness_ask(*args, **kwargs)
            self.assertEqual(data["edit_targets"], [str(target)])
            log.append(("harness", data))
            target.write_text("Peer improved draft", encoding="utf-8")
            return json.dumps(current_acceptance({"deliverable": "Updated saved draft", "files": [str(target)],
                "changes": "Improved draft", "evidence": "Saved file", "blocked": "", "checks": []}))
        engine.codex.ask, engine.harness.ask = ask_codex, ask_harness
        return engine, target

    def test_peer_edits_saved_file_then_lead_checks_before_next_round(self):
        log = []
        engine, target = self.editing_engine(log)
        engine._run("Create and improve the shared draft")
        phases = [(speaker, data["phase"]) for speaker, data in log]
        peer_index = phases.index(("harness", "peer_revision"))
        self.assertEqual(phases[peer_index + 1], ("codex", "discussion_evidence"))
        self.assertEqual(target.read_text(encoding="utf-8"), "Peer improved draft")
        handoff = json.loads((engine.artifacts / "handoff-001.json").read_text(encoding="utf-8"))
        entry = handoff["files"][0]
        self.assertTrue(handoff["completed"])
        self.assertEqual(Path(entry["backup"]).read_text(encoding="utf-8"), "Codex draft")
        self.assertNotEqual(entry["before_sha256"], entry["after_sha256"])
        history = json.loads((engine.artifacts / "actions.json").read_text(encoding="utf-8"))
        self.assertEqual([row["action"] for row in history], ["build", "peer_improve", "verify_peer"])
        self.assertEqual(history[-1]["files"], [str(target)])

    def test_peer_edit_setting_and_readonly_setting_block_handoff(self):
        for setting in ("peer_edit", "allow_write"):
            with self.subTest(setting=setting):
                log = []
                engine, target = self.editing_engine(log)
                engine.settings[setting] = False
                engine._run("Create draft")
                self.assertNotIn("peer_revision", [data["phase"] for _, data in log])
                self.assertEqual(target.read_text(encoding="utf-8"), "Codex draft")

    def test_peer_handoff_rejects_unknown_relative_directory_and_ledger_targets(self):
        engine, target = self.editing_engine([])
        engine.artifacts = Path(self.temp_data.name) / "ledger"
        engine.artifacts.mkdir()
        ledger = engine.artifacts / "task.md"
        ledger.write_text("task", encoding="utf-8")
        for targets in ([str(target.parent / "unrelated.txt")], [target.name], [str(target.parent)], [str(ledger)]):
            with self.subTest(targets=targets):
                self.assertEqual(engine._peer_targets({"files": [str(target), str(ledger)]}, targets), [])

    def test_interrupted_peer_write_is_not_replayed(self):
        engine, target = self.editing_engine([])
        original = engine.harness.ask
        def ask(*args, **kwargs):
            raw = original(*args, **kwargs)
            if json.loads(args[1].split("\n", 1)[1])["phase"] == "peer_revision":
                raise RecoveryRequired("lost peer completion")
            return raw
        engine.harness.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("Create draft")
        self.assertEqual(target.read_text(encoding="utf-8"), "Peer improved draft")
        with self.assertRaisesRegex(RecoveryRequired, "修改过文件"):
            engine._resume_checkpoint(engine.job_id)

    def test_malformed_peer_acceptance_does_not_claim_completed_work(self):
        engine, target = self.editing_engine([])
        original = engine.harness.ask
        def ask(*args, **kwargs):
            raw = original(*args, **kwargs)
            if json.loads(args[1].split("\n", 1)[1])["phase"] == "peer_revision":
                data = json.loads(raw)
                data["checks"] = "claimed success"
                return json.dumps(data)
            return raw
        engine.harness.ask = ask
        with self.assertRaisesRegex(RecoveryRequired, "格式无法确认"):
            engine._run("Create draft")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
        self.assertTrue((engine.artifacts / "handoff-001.json").is_file())

    def test_peer_tool_block_is_not_sent_to_peer_repeatedly(self):
        log = []
        engine, target = self.editing_engine(log)
        original_c, original_h = engine.codex.ask, engine.harness.ask
        def ask_codex(*args, **kwargs):
            raw = original_c(*args, **kwargs)
            data = json.loads(args[1].split("\n", 1)[1])
            if data["phase"] == "discussion_judge" and engine.codex.judge_calls == 2:
                decision = json.loads(raw)
                decision.update(status="continue", issue_severity="major", worthwhile=True,
                    next_action="peer_improve", edit_targets=[str(target)], next_step="Try unavailable tool again")
                return json.dumps(decision)
            return raw
        def ask_harness(*args, **kwargs):
            raw = original_h(*args, **kwargs)
            if json.loads(args[1].split("\n", 1)[1])["phase"] == "peer_revision":
                data = json.loads(raw)
                data["blocked"] = "Required app tool unavailable; Codex should use its own tools"
                return json.dumps(data)
            return raw
        engine.codex.ask, engine.harness.ask = ask_codex, ask_harness
        engine._run("Create draft")
        phases = [data["phase"] for _, data in log]
        self.assertEqual(phases.count("peer_revision"), 1)
        self.assertIn("deliverable_improve", phases)
        second_judge = [data for _, data in log if data["phase"] == "discussion_judge"][1]
        self.assertFalse(second_judge["peer_edit_allowed"])

    def test_ambiguous_harness_rpc_records_attention_and_only_one_submission(self):
        from unittest.mock import Mock
        engine = self.discussion_engine([], ["resolved"])
        client = HarnessClient(url="http://127.0.0.1:54321")
        client.authenticate = lambda: None
        client.create = lambda *args: "harness-test"
        selection = {"provider": ROUTE["harness_provider"], "model": ROUTE["harness_model"], "reasoningEffort": ROUTE["harness_effort"]}
        client.list_sessions = lambda: [{"sessionId": "harness-test", "running": False,
            "projections": {"values": {"modelSelection": selection}}}]
        client.follow = lambda sid: (Mock(), "stream", {"records": []})
        engine.harness = client
        with patch.object(client.opener, "open", side_effect=OSError("response lost")) as opened:
            with self.assertRaises(RecoveryRequired):
                engine._run("task")
            self.assertEqual(opened.call_count, 1)
            self.assertIn('"requestId"', opened.call_args.args[0].data.decode())
        job = engine.store.job(engine.job_id)
        self.assertEqual(job["status"], "needs_attention")
        self.assertIn("sessionId=harness-test", job["error"])

    def test_later_round_resume_reuses_completed_peer_edit_and_backup(self):
        log = []
        engine, target = self.editing_engine(log)
        original = engine.codex.ask
        failures = [True]
        def ask(*args, **kwargs):
            data = json.loads(args[1].split("\n", 1)[1])
            if data["phase"] == "discussion" and data["round"] == 2 and failures:
                failures.pop()
                raise RecoveryRequired("second review interrupted")
            return original(*args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("Create draft")
        handoff_path = engine.artifacts / "handoff-001.json"
        before = handoff_path.read_bytes(), handoff_path.stat().st_mtime_ns
        engine.codex.resume = lambda cid: None
        engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
        engine.resume_run(engine.job_id)
        self.assertEqual((handoff_path.read_bytes(), handoff_path.stat().st_mtime_ns), before)
        self.assertEqual([data["phase"] for _, data in log].count("peer_revision"), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "Peer improved draft")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_partial_peer_backup_failure_can_resume_before_single_submission(self):
        log = []
        engine, target = self.editing_engine(log)
        def partial_copy(source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("backup interrupted")
        with patch("conductor_core.shutil.copy2", side_effect=partial_copy):
            with self.assertRaisesRegex(OSError, "backup interrupted"):
                engine._run("Create draft")
        self.assertEqual(target.read_text(encoding="utf-8"), "Codex draft")
        self.assertNotIn("peer_revision", [data["phase"] for _, data in log])
        self.assertFalse((engine.artifacts / "handoff-001-before").exists())
        engine.codex.resume = lambda cid: None
        engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
        engine.resume_run(engine.job_id)
        self.assertEqual([data["phase"] for _, data in log].count("peer_revision"), 1)
        self.assertEqual((engine.artifacts / "handoff-001-before" / f"000-{target.name}").read_text(encoding="utf-8"), "Codex draft")
        self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")

    def test_completed_peer_resume_rejects_changed_missing_or_unverifiable_files(self):
        for damage in ("changed", "deleted", "legacy_hashes", "missing_actions", "empty_actions", "invalid_actions"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory, \
                    patch("conductor_core.DATA_DIR", Path(directory)):
                log = []
                engine, target = self.editing_engine(log)
                original = engine.codex.ask
                def interrupt(*args, **kwargs):
                    data = json.loads(args[1].split("\n", 1)[1])
                    if data["phase"] == "discussion" and data["round"] == 2:
                        raise RecoveryRequired("review interrupted")
                    return original(*args, **kwargs)
                engine.codex.ask = interrupt
                with self.assertRaises(RecoveryRequired):
                    engine._run("Create draft")
                backup = engine.artifacts / "handoff-001-before" / f"000-{target.name}"
                before = backup.read_bytes(), backup.stat().st_mtime_ns
                actions_path = engine.artifacts / "actions.json"
                if damage == "changed":
                    target.write_text("EXTERNAL EDIT", encoding="utf-8")
                elif damage == "deleted":
                    target.unlink()
                elif damage == "legacy_hashes":
                    actions = json.loads(actions_path.read_text(encoding="utf-8"))
                    actions[-1].pop("file_sha256")
                    actions_path.write_text(json.dumps(actions), encoding="utf-8")
                elif damage in ("empty_actions", "invalid_actions"):
                    actions_path.write_text("[]" if damage == "empty_actions" else "null", encoding="utf-8")
                    target.write_text("EXTERNAL EDIT", encoding="utf-8")
                else:
                    actions_path.unlink()
                calls = len(log)
                with self.assertRaisesRegex(RecoveryRequired, "检查点"):
                    engine.resume_run(engine.job_id)
                self.assertEqual(engine.store.job(engine.job_id)["status"], "needs_attention")
                self.assertEqual(len(log), calls)
                self.assertEqual((backup.read_bytes(), backup.stat().st_mtime_ns), before)
                if damage == "changed":
                    self.assertEqual(target.read_text(encoding="utf-8"), "EXTERNAL EDIT")
                self.assertEqual([data["phase"] for _, data in log].count("peer_revision"), 1)

    def test_prepared_backup_is_reused_after_ledger_failure_only_if_targets_unchanged(self):
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory, \
                    patch("conductor_core.DATA_DIR", Path(directory)):
                log = []
                engine, target = self.editing_engine(log)
                save = engine._save_artifact
                def fail_handoff(name, text):
                    if name == "handoff-001.json":
                        raise OSError("ledger temporarily unavailable")
                    return save(name, text)
                engine._save_artifact = fail_handoff
                with self.assertRaises(OSError):
                    engine._run("Create draft")
                backup = engine.artifacts / "handoff-001-before" / f"000-{target.name}"
                original = backup.read_bytes(), backup.stat().st_mtime_ns
                engine._save_artifact = save
                engine.codex.resume = lambda cid: None
                engine.harness_rows = [{"sessionId": "harness-test", "running": False}]
                if changed:
                    target.write_text("External edit", encoding="utf-8")
                    with self.assertRaisesRegex(RecoveryRequired, "备份"):
                        engine.resume_run(engine.job_id)
                    self.assertEqual(target.read_text(encoding="utf-8"), "External edit")
                else:
                    engine.resume_run(engine.job_id)
                    self.assertEqual(engine.store.job(engine.job_id)["status"], "completed")
                self.assertEqual((backup.read_bytes(), backup.stat().st_mtime_ns), original)
                self.assertEqual([data["phase"] for _, data in log].count("peer_revision"), 0 if changed else 1)

    def test_time_target_finishes_round_and_continues_for_major_issue(self):
        log = []
        engine = self.discussion_engine(log, ["continue", "resolved"])
        original = engine.codex.ask
        def ask(*args, **kwargs):
            result = original(*args, **kwargs)
            phase = log[-1][1]["phase"]
            if phase == "discussion":
                engine.started_at -= 120
            return result
        engine.codex.ask = ask
        engine._run("重要，必须双方审核", {"time_minutes": 1})
        self.assertEqual(engine.codex.judge_calls, 2)
        self.assertEqual(engine.store.recent()[0]["status"], "completed")
        self.assertEqual(log[-1][1]["phase"], "final_execution")
        self.assertEqual(log[-1][1]["discussion_rounds"], 2)
        self.assertIn(("harness", "discussion"), [(who, data["phase"]) for who, data in log])
        self.assertTrue((engine.artifacts / "final.md").is_file())

    def test_time_target_does_not_downgrade_a_resolved_round(self):
        engine = self.discussion_engine([], ["resolved"])
        engine.time_due = lambda: True
        engine._run("重要，必须双方审核", {"time_minutes": 1})
        self.assertEqual(engine.codex.judge_calls, 1)
        self.assertEqual(engine.store.recent()[0]["status"], "completed")

    def test_time_target_after_action_still_checks_changed_deliverable(self):
        log = []
        engine = self.discussion_engine(log, ["escalate", "resolved"])
        original = engine.codex.ask
        def ask(*args, **kwargs):
            data = json.loads(args[1].split("\n", 1)[1])
            reply = original(*args, **kwargs)
            if data["phase"] == "discussion_evidence":
                engine.started_at -= 120
            return reply
        engine.codex.ask = ask
        engine._run("task", {"time_minutes": 1})
        self.assertEqual(engine.codex.judge_calls, 2)
        self.assertEqual(engine.store.recent()[0]["status"], "completed")

    def test_fast_target_allows_necessary_work_but_retains_loop_guard(self):
        log = []
        engine = self.discussion_engine(log, ["continue"])
        engine._run("task", {"runtime_mode": "fast"})
        self.assertEqual(engine.codex.judge_calls, 12)
        self.assertEqual(engine.store.recent()[0]["status"], "needs_attention")

    def test_unconfirmed_operation_keeps_completed_peer_and_requires_attention(self):
        engine = self.discussion_engine([], ["resolved"])
        original = engine.codex.ask
        def ask(*args, **kwargs):
            data = json.loads(args[1].split("\n", 1)[1])
            if data["phase"] == "independent":
                raise RecoveryRequired("original turn uncertain")
            return original(*args, **kwargs)
        engine.codex.ask = ask
        with self.assertRaises(RecoveryRequired):
            engine._run("task")
        self.assertEqual(engine.store.recent()[0]["status"], "needs_attention")
        self.assertTrue(any(msg["text"] == "H_FIRST" for msg in engine.store.messages(engine.job_id)))

    def test_stop_before_worker_start_is_not_erased(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.prepare_run()
        targets = engine.request_stop()
        engine.finish_stop(targets)
        with self.assertRaises(Stopped):
            engine.run("task")
        self.assertFalse(log)
        self.assertEqual(engine.store.recent()[0]["status"], "stopped")

    def test_old_stop_must_finish_before_another_task_can_prepare(self):
        engine = self.discussion_engine([], ["resolved"])
        engine.codex.active = ("A-thread", "A-turn")
        engine.harness.active_session = "A-session"
        interrupted = []
        engine.codex._interrupt = lambda *target: interrupted.append(target)
        engine.harness.rpc = lambda method, args: interrupted.append(args["request"]["sessionId"])
        targets = engine.request_stop()
        engine.codex.active = ("B-thread", "B-turn")
        engine.harness.active_session = "B-session"
        with self.assertRaisesRegex(RuntimeError, "停止请求"):
            engine.prepare_run()
        engine.finish_stop(targets)
        self.assertEqual(interrupted, [("A-thread", "A-turn"), "A-session"])
        engine.prepare_run()
        self.assertFalse(engine.cancelled.is_set())

    def test_independent_work_and_reviews_and_discussion_really_overlap(self):
        engine = self.discussion_engine([], ["resolved"])
        barrier = threading.Barrier(2, timeout=3)
        original_c, original_h = engine.codex.ask, engine.harness.ask
        def synchronized(original):
            def ask(thread_id, text, *args, **kwargs):
                phase = json.loads(text.split("\n", 1)[1])["phase"]
                if phase in ("independent", "peer_review", "discussion"):
                    barrier.wait()
                return original(thread_id, text, *args, **kwargs)
            return ask
        engine.codex.ask, engine.harness.ask = synchronized(original_c), synchronized(original_h)
        engine._run("task")
        self.assertEqual(engine.store.recent()[0]["status"], "completed")

    def test_judge_next_step_reaches_action_and_shared_files(self):
        log = []
        engine = self.discussion_engine(log, ["escalate", "resolved"])
        engine._run("task")
        action = next(data for _, data in log if data["phase"] == "discussion_evidence")
        self.assertEqual(action["next_step"], "verify disputed evidence")
        self.assertEqual((engine.artifacts / "deliverable.md").read_text(encoding="utf-8"), "CANDIDATE")
        history = json.loads((engine.artifacts / "actions.json").read_text(encoding="utf-8"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["next_step"], action["next_step"])
        self.assertTrue(Path(history[0]["revision"]).is_file())

    def test_repeated_discussion_switches_to_evidence_action(self):
        log = []
        engine = self.discussion_engine(log, ["continue"] * 3 + ["resolved"])
        engine._run("task")
        actions = [data for _, data in log if data["phase"] == "discussion_evidence"]
        self.assertEqual(len(actions), 1)
        self.assertGreaterEqual(actions[0]["stalled_rounds"], 2)
        self.assertTrue(actions[0]["recovery"])

    def test_blocked_major_issue_is_visible_and_never_completed(self):
        log = []
        engine = self.discussion_engine(log, ["escalate"])
        original = engine.codex.ask
        def blocked(thread_id, text, *args, **kwargs):
            data = json.loads(text.split("\n", 1)[1])
            if data["phase"] == "discussion_judge":
                result = json.loads(original(thread_id, text, *args, **kwargs))
                result.update(next_action="blocked", next_step="Missing user dataset")
                return json.dumps(result)
            return original(thread_id, text, *args, **kwargs)
        engine.codex.ask = blocked
        engine._run("task")
        self.assertEqual(engine.store.recent()[0]["status"], "needs_attention")
        self.assertFalse(log[-1][1]["discussion_resolved"])
        self.assertEqual(log[-1][1]["discussion_rounds"], 1)

    def test_new_peer_uses_existing_project_directory(self):
        log = []
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex, engine.harness = FakeCodex(log), FakeHarness(log)
        engine.refresh = lambda: ""
        engine.codex_rows = [{"id": "existing", "cwd": str(Path(__file__).resolve().parent)}]
        engine.route = lambda *args: {**ROUTE, "codex_thread": "existing"}
        engine._common_facts = lambda *args: ""
        engine.codex.resume = lambda sid: None
        directories = []
        def create(cwd, title):
            directories.append(cwd)
            return "harness-test"
        engine.harness.create = create
        engine._run("task", {"mode": "dual"})
        self.assertEqual(directories, [str(Path(__file__).resolve().parent)])

    def test_independent_first_then_reciprocal_review(self):
        log = []
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex, engine.harness = FakeCodex(log), FakeHarness(log)
        engine.refresh = lambda: ""
        engine.route = lambda *args: copy.deepcopy(ROUTE)
        self.assertEqual(engine._run("task", {"mode": "dual"}), "FINAL")
        self.assertCountEqual([(p, d["phase"]) for p, d in log[:2]], [("codex", "independent"), ("harness", "independent")])
        self.assertCountEqual([(p, d["phase"]) for p, d in log[2:4]], [("codex", "peer_review"), ("harness", "peer_review")])
        self.assertEqual(log[-1][1]["phase"], "final_execution")
        final = log[-1][1]
        self.assertEqual(final["codex_review"], "C_REVIEW")
        self.assertEqual(final["harness_review"], "H_REVIEW")
        self.assertEqual(engine.store.recent()[0]["status"], "completed")
        steps = engine.store.steps(engine.job_id)
        self.assertTrue(all(row["status"] == "completed" for row in steps))
        self.assertCountEqual([s["stage"] for s in steps[-5:]], [
            "Codex 独立分析", "Harness 独立分析", "Codex 审核 Harness", "Harness 审核 Codex", "Codex 汇总执行"])

    def test_failure_preserves_partial_work(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        log = []
        engine.codex, engine.harness = FakeCodex(log), FakeHarness(log)
        engine.refresh = lambda: ""
        engine.route = lambda *args: copy.deepcopy(ROUTE)
        def fail(*args, **kwargs):
            raise RuntimeError("Harness offline")
        engine.harness.ask = fail
        with self.assertRaisesRegex(RuntimeError, "offline"):
            engine._run("task")
        self.assertEqual(engine.store.recent()[0]["status"], "failed")
        self.assertEqual(len(engine.store.messages(engine.job_id)), 1)
        failed = next(s for s in engine.store.steps(engine.job_id) if s["status"] == "failed")
        self.assertEqual((failed["stage"], failed["status"]), ("Harness 独立分析", "failed"))

    def test_discussion_stops_when_both_peers_mark_small_residual(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        self.assertEqual(engine._run("task", {"mode": "dual"}), "FINAL")
        self.assertEqual(log[4][1]["phase"], "deliverable_build")
        self.assertCountEqual([(p, d["phase"]) for p, d in log[5:7]], [("codex", "discussion"), ("harness", "discussion")])
        self.assertEqual(log[-2][1]["phase"], "discussion_judge")
        self.assertEqual(log[-1][1]["discussion_rounds"], 1)
        self.assertEqual(log[-1][1]["codex_discussion"]["status"], "resolved")
        self.assertEqual(log[-1][1]["discussion_judge"]["status"], "resolved")

    def test_discussion_judge_can_require_another_round(self):
        log = []
        engine = self.discussion_engine(log, ["continue", "resolved"])
        self.assertEqual(engine._run("task", {"mode": "dual"}), "FINAL")
        phases = [d["phase"] for _, d in log]
        self.assertEqual(phases.count("discussion"), 4)
        self.assertEqual(phases.count("discussion_judge"), 2)
        final = log[-1][1]
        self.assertEqual(final["discussion_rounds"], 2)
        self.assertEqual(final["discussion_judge"]["status"], "resolved")
        second_round = next(data for _, data in log if data["phase"] == "discussion" and data["round"] == 2)
        self.assertEqual(second_round["previous_judgment"]["status"], "continue")

    def test_discussion_can_continue_past_round_three(self):
        log = []
        engine = self.discussion_engine(log, ["continue"] * 3 + ["resolved"])
        engine._run("task")
        self.assertEqual(log[-1][1]["discussion_rounds"], 4)
        self.assertTrue(log[-1][1]["discussion_resolved"])

    def test_evidence_gap_is_checked_before_next_round(self):
        log = []
        engine = self.discussion_engine(log, ["escalate", "resolved"])
        engine._run("task")
        self.assertIn("discussion_evidence", [data["phase"] for _, data in log])
        self.assertTrue(log[-1][1]["additional_evidence"])

    def test_cost_cap_keeps_unresolved_status(self):
        log = []
        engine = self.discussion_engine(log, ["continue"])
        engine.settings["max_discussion_rounds"] = 6
        engine._run("task")
        self.assertEqual(log[-1][1]["discussion_rounds"], 6)
        self.assertFalse(log[-1][1]["discussion_resolved"])
        self.assertEqual(engine.store.recent()[0]["status"], "needs_attention")

    def test_quality_priority_can_continue_past_round_six(self):
        log = []
        engine = self.discussion_engine(log, ["continue"] * 6 + ["resolved"])
        engine._run("task")
        self.assertEqual(log[-1][1]["discussion_rounds"], 7)
        self.assertTrue(log[-1][1]["discussion_resolved"])

    def test_arbiter_model_creation_failure_can_fall_back(self):
        log = []
        engine = self.discussion_engine(log, ["resolved"])
        engine.codex_models = {**C_MODELS, "strong": {"description": "Frontier intelligence", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}}
        def create(model, *args, **kwargs):
            if model == "strong":
                raise CodexError("No available channel for model strong")
            return "codex-test"
        engine.codex.create = create
        engine._run("task")
        self.assertTrue(log[-1][1]["discussion_resolved"])
        self.assertNotIn("strong", engine.eligible_models())

    def test_stopped_stage_is_not_recorded_as_completed(self):
        engine = Conductor(dict(DEFAULTS), Store(":memory:"))
        engine.codex, engine.harness = FakeCodex([]), FakeHarness([])
        engine.refresh = lambda: ""
        engine.route = lambda *args: copy.deepcopy(ROUTE)
        def stop(*args, **kwargs):
            raise Stopped("Stopped")
        engine.harness.ask = stop
        with self.assertRaises(Stopped):
            engine._run("task")
        self.assertEqual(engine.store.recent()[0]["status"], "stopped")
        self.assertEqual(engine.store.steps(engine.job_id)[-1]["status"], "stopped")


class PersistenceTests(unittest.TestCase):
    def test_running_job_query_tracks_only_active_workflows(self):
        store = Store(":memory:")
        job = store.create("active")
        try:
            self.assertTrue(store.has_running_jobs())
            store.update(job, status="completed")
            self.assertFalse(store.has_running_jobs())
        finally:
            store.conn.close()

    def test_dual_reused_conversations_must_share_the_same_workspace(self):
        route = {"mode": "dual", "codex_thread": "codex-a", "harness_session": "harness-b"}
        codex_rows = [{"id": "codex-a", "cwd": r"C:\Projects\thesis"}]
        harness_rows = [{"sessionId": "harness-b", "cwd": r"C:\Projects\lesson"}]
        with self.assertRaisesRegex(ValueError, "不同工作目录"):
            routed_workspace(route, codex_rows, harness_rows, r"C:\Projects\default")

    def test_dual_reused_conversations_in_same_workspace_are_normalized(self):
        route = {"mode": "dual", "codex_thread": "codex-a", "harness_session": "harness-b"}
        codex_rows = [{"id": "codex-a", "cwd": r"C:\Projects\shared\."}]
        harness_rows = [{"sessionId": "harness-b", "cwd": r"C:\Projects\shared"}]
        self.assertEqual(routed_workspace(route, codex_rows, harness_rows, r"C:\Projects\default"),
                         str(Path(r"C:\Projects\shared").resolve()))

    def test_reused_conversation_without_known_workspace_fails_closed(self):
        route = {"mode": "codex", "codex_thread": "codex-a", "harness_session": "new"}
        with self.assertRaisesRegex(ValueError, "无法确认复用对话"):
            routed_workspace(route, [], [], r"C:\Projects\default")

    def test_resume_reserves_workspace_before_checkpoint_and_claims_job_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            store = Store(root / "history.db")
            job_id = store.create("resume project")
            route = {"mode": "codex", "workspace": str(workspace), "codex_thread": "new"}
            store.update(job_id, status="interrupted", route=json.dumps(route), codex_id="codex-thread")
            engine = Conductor(dict(DEFAULTS), store)
            engine._run = lambda task, controls: store.update(job_id, status="completed")
            with patch("conductor_core.DATA_DIR", root), \
                    patch.object(Conductor, "_keep_awake", lambda self: nullcontext()):
                with workflow_guard(workspace=workspace):
                    with self.assertRaises(WorkflowBusy):
                        engine.resume_run(job_id)
                    self.assertEqual(store.job(job_id)["status"], "interrupted")
                def run_with_checkpoint_lock(task, controls):
                    self.assertEqual(store.job(job_id)["status"], "running")
                    with self.assertRaises(WorkflowBusy):
                        with workflow_guard(workspace=workspace):
                            pass
                    self.assertEqual(store.recover_interrupted(), [])
                    store.update(job_id, status="completed")
                engine._run = run_with_checkpoint_lock
                engine.resume_run(job_id)
            self.assertEqual(store.job(job_id)["status"], "completed")
            store.conn.close()

    def test_resume_claim_rejects_a_second_owner_without_changing_active_job(self):
        store = Store(":memory:")
        job_id = store.create("resume race")
        store.update(job_id, status="interrupted")
        try:
            store.claim_resume(job_id)
            with self.assertRaises(WorkflowBusy):
                store.claim_resume(job_id)
            self.assertEqual(store.job(job_id)["status"], "running")
            self.assertTrue(store.has_running_jobs())
        finally:
            store.conn.close()

    def test_restart_preserves_answers_and_marks_only_incomplete_work(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            store = Store(database)
            completed = store.create("completed task")
            store.update(completed, status="completed")
            job = store.create("interrupted task")
            step = store.start_step(job, "Codex 独立分析", "codex")
            store.message(job, "Codex 独立分析", "codex", "SAVED_ANSWER")
            store.finish_step(step, "completed")
            store.start_step(job, "Harness 独立分析", "harness")
            store.conn.close()
            reopened = Store(database)
            try:
                with patch("conductor_core.DATA_DIR", Path(directory)):
                    self.assertEqual(reopened.recover_interrupted(), [job])
                    self.assertEqual(reopened.recover_interrupted(), [])
                self.assertEqual(reopened.messages(job)[0]["text"], "SAVED_ANSWER")
                self.assertEqual([s["status"] for s in reopened.steps(job)], ["completed", "interrupted"])
                self.assertEqual([r["status"] for r in reopened.recent()], ["interrupted", "completed"])
                report = reopened.export_markdown(job)
                self.assertIn("SAVED_ANSWER", report)
                self.assertIn("已中断：Harness 独立分析", report)
                self.assertIn("不会自动重发", report)
            finally:
                reopened.conn.close()

    def test_second_instance_does_not_mark_an_active_workflow_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(":memory:")
            job = store.create("active task")
            with patch("conductor_core.DATA_DIR", Path(directory)):
                with workflow_guard():
                    self.assertEqual(store.recover_interrupted(), [])
                    self.assertEqual(store.recent()[0]["status"], "running")
                self.assertEqual(store.recover_interrupted(), [job])

    def test_recovery_marks_only_jobs_without_a_live_job_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "history.db")
            active, stale = store.create("active"), store.create("stale")
            with patch("conductor_core.DATA_DIR", root):
                with workflow_guard(job_id=active):
                    self.assertEqual(store.recover_interrupted(), [stale])
                    self.assertEqual(store.job(active)["status"], "running")
                    self.assertEqual(store.job(stale)["status"], "interrupted")
            store.conn.close()

    def test_workspace_and_conversation_locks_only_reject_shared_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("conductor_core.DATA_DIR", root):
                with workflow_guard(workspace=root / "thesis"):
                    with self.assertRaises(WorkflowBusy):
                        with workflow_guard(workspace=root / "thesis"):
                            pass
                    with workflow_guard(workspace=root / "lesson"):
                        pass
                    with workflow_guard(session=("codex", "shared-thread")):
                        with self.assertRaises(WorkflowBusy):
                            with workflow_guard(session=("codex", "shared-thread")):
                                pass

    def test_workspace_locks_reject_parent_child_overlap_but_allow_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child, sibling = root / "project", root / "project" / "lesson", root / "course"
            with patch("conductor_core.DATA_DIR", root / "data"):
                with workflow_guard(workspace=parent):
                    with self.assertRaises(WorkflowBusy):
                        with workflow_guard(workspace=child):
                            pass
                    with workflow_guard(workspace=sibling):
                        pass
                with workflow_guard(workspace=child):
                    with self.assertRaises(WorkflowBusy):
                        with workflow_guard(workspace=parent):
                            pass

    def test_parallel_shared_compatibility_lock_excludes_legacy_exclusive_runs(self):
        with tempfile.TemporaryDirectory() as directory, patch("conductor_core.DATA_DIR", Path(directory)):
            with workflow_guard(shared_legacy=True):
                with workflow_guard(shared_legacy=True):
                    pass
                with self.assertRaises(WorkflowBusy):
                    with workflow_guard():
                        pass
            with workflow_guard():
                with self.assertRaises(WorkflowBusy):
                    with workflow_guard(shared_legacy=True):
                        pass

    def test_workspace_lock_is_shared_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            workspace = root / "thesis"
            other_workspace = root / "lesson"
            ready, release = root / "ready", root / "release"
            env = {**os.environ, "CONDUCTOR_DATA_DIR": str(data)}
            source = "\n".join((
                "from pathlib import Path",
                "import sys, time",
                "from conductor_core import workflow_guard",
                "ready, release = Path(sys.argv[2]), Path(sys.argv[3])",
                "with workflow_guard(workspace=sys.argv[1]):",
                "    ready.write_text('ready')",
                "    deadline = time.monotonic() + 10",
                "    while not release.exists() and time.monotonic() < deadline:",
                "        time.sleep(.01)",
            ))
            child = subprocess.Popen(
                [sys.executable, "-c", source, str(workspace), str(ready), str(release)],
                cwd=str(Path(__file__).resolve().parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), child.stderr.read() if child.poll() is not None else "child did not acquire its workspace lock")
                with patch("conductor_core.DATA_DIR", data):
                    with self.assertRaises(WorkflowBusy):
                        with workflow_guard(workspace=workspace):
                            pass
                    with workflow_guard(workspace=other_workspace):
                        pass
            finally:
                release.write_text("release", encoding="ascii")
                stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr or stdout)

    def test_terminated_process_releases_workspace_lock_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, workspace, ready = root / "data", root / "project", root / "ready"
            env = {**os.environ, "CONDUCTOR_DATA_DIR": str(data)}
            source = "\n".join((
                "from pathlib import Path",
                "import sys, time",
                "from conductor_core import workflow_guard",
                "ready = Path(sys.argv[2])",
                "with workflow_guard(workspace=sys.argv[1]):",
                "    ready.write_text('ready')",
                "    while True: time.sleep(1)",
            ))
            child = subprocess.Popen(
                [sys.executable, "-c", source, str(workspace), str(ready)],
                cwd=str(Path(__file__).resolve().parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), child.stderr.read() if child.poll() is not None else "child did not acquire its workspace lock")
                child.kill()
                child.wait(timeout=5)
                with patch("conductor_core.DATA_DIR", data):
                    with workflow_guard(workspace=workspace):
                        pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()

    def test_recovery_after_one_process_dies_preserves_another_live_job_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            workspaces = [root / "project-a", root / "project-b"]
            for workspace in workspaces:
                workspace.mkdir()

            first_ready, second_ready = root / "first-ready", root / "second-ready"
            first_release, second_release = root / "first-release", root / "second-release"
            env = {**os.environ, "CONDUCTOR_DATA_DIR": str(data)}
            source = "\n".join((
                "from contextlib import nullcontext",
                "from pathlib import Path",
                "import json, sys, time",
                "from conductor_core import Conductor, DEFAULTS, Store, atomic_write_text",
                "from test_conductor import C_MODELS, FakeCodex, FakeHarness, H_MODELS, ROUTE",
                "workspace, ready, release = map(Path, sys.argv[1:4])",
                "tag = sys.argv[4]",
                "log = []",
                "engine = Conductor({**DEFAULTS, 'workspace': str(workspace)}, Store())",
                "engine.codex, engine.harness = FakeCodex(log), FakeHarness(log)",
                "engine.codex_models, engine.harness_models = C_MODELS, H_MODELS",
                "engine.refresh = lambda: ''",
                "engine.route = lambda *args: {**ROUTE, 'use_discussion': True}",
                "engine._keep_awake = lambda: nullcontext()",
                "engine.codex.create = lambda *args, **kwargs: 'codex-' + tag",
                "engine.harness.create = lambda *args: 'harness-' + tag",
                "original_ask = engine.codex.ask",
                "def wait_in_active_stage(*args, **kwargs):",
                "    data = json.loads(args[1].split('\\n', 1)[1])",
                "    if data['phase'] == 'independent':",
                "        atomic_write_text(ready, str(engine.job_id))",
                "        deadline = time.monotonic() + 30",
                "        while not release.exists() and time.monotonic() < deadline:",
                "            time.sleep(.02)",
                "    return original_ask(*args, **kwargs)",
                "engine.codex.ask = wait_in_active_stage",
                "engine.run('parallel fake workflow ' + tag, {'mode': 'dual'})",
                "engine.store.conn.close()",
            ))
            children = []
            observer = None

            def start_child(workspace, ready, release, tag):
                return subprocess.Popen(
                    [sys.executable, "-c", source, str(workspace), str(ready), str(release), tag],
                    cwd=str(Path(__file__).resolve().parent), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )

            def wait_until_ready(child, marker, label):
                deadline = time.monotonic() + 15
                while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                if not marker.exists():
                    stderr = child.communicate(timeout=2)[1] if child.poll() is not None else ""
                    self.fail(f"{label} Conductor process did not reach its active stage (exit={child.poll()}): {stderr}")

            try:
                first = start_child(workspaces[0], first_ready, first_release, "a")
                children.append(first)
                second = start_child(workspaces[1], second_ready, second_release, "b")
                children.append(second)
                wait_until_ready(first, first_ready, "first")
                wait_until_ready(second, second_ready, "second")
                first_job, second_job = int(first_ready.read_text()), int(second_ready.read_text())
                self.assertNotEqual(first_job, second_job)

                with patch("conductor_core.DATA_DIR", data):
                    observer = Store()
                    with workflow_guard(shared_legacy=True):
                        with workflow_guard(registration=True):
                            recovered = observer._mark_interrupted()
                    self.assertEqual(recovered, [])
                first.kill()
                first.communicate(timeout=5)
                with patch("conductor_core.DATA_DIR", data):
                    with workflow_guard(shared_legacy=True):
                        with workflow_guard(registration=True):
                            recovered = observer._mark_interrupted()
                self.assertEqual(recovered, [first_job])
                self.assertEqual(observer.job(first_job)["status"], "interrupted")
                self.assertEqual(observer.job(second_job)["status"], "running")
                self.assertTrue(any(step["stage"] == "Codex 独立分析" and step["status"] == "interrupted"
                                    for step in observer.steps(first_job)))
                self.assertTrue(observer.messages(first_job))
                self.assertTrue(observer.messages(second_job))

                second_release.write_text("complete", encoding="ascii")
                _, stderr = second.communicate(timeout=10)
                self.assertEqual(second.returncode, 0, stderr)
                self.assertEqual(observer.job(second_job)["status"], "completed")
                self.assertTrue(observer.steps(second_job))
                self.assertTrue(all(row["status"] == "completed" for row in observer.steps(second_job)))
            finally:
                first_release.write_text("release", encoding="ascii")
                second_release.write_text("release", encoding="ascii")
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    if child.stdout:
                        child.stdout.close()
                    if child.stderr:
                        child.stderr.close()
                if observer:
                    observer.conn.close()

    def test_independent_conductor_jobs_run_together_without_false_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "history.db"
            workspaces = [root / "thesis", root / "lesson"]
            for workspace in workspaces:
                workspace.mkdir()
            engines = [Conductor({**DEFAULTS, "workspace": str(workspace)}, Store(database))
                       for workspace in workspaces]
            rendezvous = threading.Barrier(2)
            entered = threading.Event()
            release = threading.Event()
            errors = []

            def worker_body(engine, workspace, task, controls):
                engine._active_guards.enter_context(workflow_guard(workspace=workspace))
                rendezvous.wait(timeout=8)
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("parallel test release was not signaled")
                engine.store.update(engine.job_id, status="completed")
                return task

            for engine, workspace in zip(engines, workspaces):
                engine._run = lambda task, controls, e=engine, w=workspace: worker_body(e, w, task, controls)

            def run(engine, task):
                try:
                    engine.run(task)
                except Exception as exc:
                    errors.append(exc)

            with patch("conductor_core.DATA_DIR", root), \
                    patch.object(Conductor, "_keep_awake", lambda self: nullcontext()):
                threads = [threading.Thread(target=run, args=(engine, task))
                           for engine, task in zip(engines, ("write thesis", "prepare lesson"))]
                for thread in threads:
                    thread.start()
                try:
                    self.assertTrue(entered.wait(8), "two independent jobs did not reach their parallel phase")
                    observer = Store(database)
                    try:
                        self.assertEqual(observer.recover_interrupted(), [])
                    finally:
                        observer.conn.close()
                finally:
                    release.set()
                    for thread in threads:
                        thread.join(8)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual({job["status"] for job in engines[0].store.recent()}, {"completed"})
            for engine in engines:
                engine.store.conn.close()

    def test_stopping_one_conductor_does_not_cancel_another(self):
        first = Conductor(dict(DEFAULTS), Store(":memory:"))
        second = Conductor(dict(DEFAULTS), Store(":memory:"))
        try:
            targets = first.request_stop()
            first.finish_stop(targets)
            self.assertTrue(first.cancelled.is_set())
            self.assertTrue(first.codex.stop_event.is_set())
            self.assertTrue(first.harness.cancel_event.is_set())
            self.assertFalse(second.cancelled.is_set())
            self.assertFalse(second.codex.stop_event.is_set())
            self.assertFalse(second.harness.cancel_event.is_set())
        finally:
            first.store.conn.close()
            second.store.conn.close()

    def test_old_database_gets_progress_table_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.db"
            old = Store(database)
            job = old.create("legacy task")
            old.message(job, "answer", "codex", "LEGACY_ANSWER")
            old.conn.execute("DROP TABLE job_steps")
            old.conn.execute("ALTER TABLE jobs DROP COLUMN failed_at")
            old.conn.commit()
            old.conn.close()
            upgraded = Store(database)
            try:
                self.assertEqual(upgraded.messages(job)[0]["text"], "LEGACY_ANSWER")
                upgraded.update(job, status="failed", error="test failure")
                self.assertGreater(upgraded.recent()[0]["failed_at"], 0)
                step = upgraded.start_step(job, "new stage", "codex")
                upgraded.finish_step(step, "completed")
                self.assertEqual(upgraded.steps(job)[0]["status"], "completed")
            finally:
                upgraded.conn.close()


def live():
    workspace = Path(__file__).resolve().parents[2] / "work" / "conductor-test"
    workspace.mkdir(exist_ok=True)
    settings = {**DEFAULTS, "workspace": str(workspace), "allow_write": False, "astra_effort": "low"}
    events = []
    def emit(kind, value):
        events.append((kind, value))
        if kind in ("status", "route", "failed", "done", "sessions"):
            print(kind, json.dumps(value, ensure_ascii=True), flush=True)
    store = Store(workspace / "live-test.db")
    engine = Conductor(settings, store, emit)
    try:
        result = engine.run(
            "CONDUCTOR TEST: 独立复核 27*14 是否等于 378。每个阶段答复不超过50字；禁止使用工具或操作文件。双方分别作答、互审，Astra 给验收标准并仲裁，Codex 最终确认。",
            {"mode": "astra", "codex_thread": "new", "harness_session": "new", "codex_model": "gpt-6.1-sol", "effort": "low", "harness_model": "deepseek-official/deepseek-flash"},
        )
        assert "378" in result, result
        stages = [v["stage"] for k, v in events if k == "message"]
        initial = ("Codex 独立分析", "Harness 独立分析")
        reviews = ("Codex 审核 Harness", "Harness 审核 Codex")
        assert all(stage in stages for stage in ("裁决指挥", *initial, *reviews, "Codex 汇总执行")), stages
        assert stages.index("裁决指挥") < min(stages.index(stage) for stage in initial), stages
        assert max(stages.index(stage) for stage in initial) < min(stages.index(stage) for stage in reviews), stages
        assert stages[-1] == "Codex 汇总执行", stages
        print("LIVE_PASS", json.dumps({"stages": stages, "answer": result}, ensure_ascii=True), flush=True)
    finally:
        engine.codex.close()


if __name__ == "__main__":
    if "--live" in sys.argv:
        live()
    else:
        unittest.main()
