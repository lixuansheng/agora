"""Deterministic transport faults; no real model, user file, or live process is touched."""
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_bridge import CodexClient, CodexError, RecoveryRequired, Stopped


class Clock:
    value = 0
    def monotonic(self):
        return self.value


class Events:
    def __init__(self, clock, noise=False, finish_at=None):
        self.clock, self.noise, self.finish_at = clock, noise, finish_at
    def get(self, timeout):
        self.clock.value += 1
        if self.clock.value > 100:
            raise AssertionError("Retry notifications kept a terminal turn running indefinitely")
        if self.finish_at and self.clock.value >= self.finish_at:
            return {"method": "turn/completed", "params": {"threadId": "thread",
                "turn": {"id": "original", "status": "completed"}}}
        if self.noise:
            return {"method": "error", "params": {"threadId": "thread", "turnId": "original",
                "willRetry": True, "error": {"message": "Reconnecting 1/5"}}}
        raise queue.Empty


class LivenessTests(unittest.TestCase):
    def client(self, status="completed", *, noise=False, empty=False, first_probe_error=False):
        clock = Clock()
        client = CodexClient()
        client.full_access = True
        client.proc = SimpleNamespace(poll=lambda: None)
        client.events = Events(clock, noise)
        methods = []
        probes = []
        def rpc(method, params=None, **kwargs):
            methods.append(method)
            if method == "turn/start":
                return {"turn": {"id": "original"}}
            if method == "thread/turns/list":
                probes.append(clock.value)
                if first_probe_error and len(probes) == 1:
                    raise TimeoutError("temporary history RPC failure")
                items = [] if empty else [{"type": "agentMessage", "phase": "final_answer", "text": "EXACT RESULT"}]
                return {"data": [{"id": "original", "status": status, "items": items}]}
            if method == "turn/interrupt":
                raise AssertionError("Passive status checks must not interrupt the original turn")
            raise AssertionError(method)
        client.rpc = rpc
        return client, clock, methods, probes

    def test_lost_completion_is_found_before_the_hour_long_idle_limit(self):
        client, clock, methods, probes = self.client()
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertLess(clock.value, 40)
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertEqual(len(probes), 1)

    def test_retry_noise_does_not_hide_a_completed_turn(self):
        client, clock, methods, _ = self.client(noise=True)
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertLess(clock.value, 40)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_interrupted_text_is_never_accepted_or_replayed_with_full_access(self):
        client, clock, methods, _ = self.client(status="interrupted", noise=True)
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(RecoveryRequired):
                client.ask("thread", "task", "model", "high", readonly=True, timeout=90)
        self.assertLess(clock.value, 80)
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/interrupt", methods)

    def test_completed_without_text_is_attention_not_success_or_replay(self):
        client, clock, methods, _ = self.client(empty=True)
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(RecoveryRequired):
                client.ask("thread", "task", "model", "high", timeout=90)
        self.assertLess(clock.value, 80)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_temporary_status_query_failure_keeps_waiting_for_original_turn(self):
        client, clock, methods, probes = self.client(first_probe_error=True)
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertLess(clock.value, 80)
        self.assertGreaterEqual(len(probes), 2)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_polled_failure_is_not_swallowed_as_a_metadata_error(self):
        client, clock, methods, _ = self.client(status="failed", noise=True)
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(CodexError):
                client.ask("thread", "task", "model", "high", timeout=90)
        self.assertLess(clock.value, 80)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_long_running_original_turn_is_not_interrupted_by_polling(self):
        client, clock, methods, probes = self.client(status="inProgress")
        client.events = Events(clock, finish_at=70)
        # Existing streamed text is delivered before its late completion notification.
        original_get = client.events.get
        first = True
        def get(timeout):
            nonlocal first
            if first:
                first = False
                return {"method": "item/completed", "params": {"threadId": "thread", "turnId": "original",
                    "item": {"type": "agentMessage", "text": "EXACT RESULT"}}}
            return original_get(timeout)
        client.events.get = get
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertGreaterEqual(len(probes), 2)
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertNotIn("turn/interrupt", methods)

    def test_unattributed_or_conflicting_events_cannot_finish_or_contaminate_turn(self):
        client, clock, methods, _ = self.client()
        client.events = queue.Queue()
        for identity in ({"threadId": "thread"}, {"turnId": "original"},
                         {"threadId": "foreign", "turnId": "original"},
                         {"threadId": "thread", "turnId": "old"}):
            client.events.put({"method": "item/agentMessage/delta", "params": {**identity, "delta": "WRONG"}})
            client.events.put({"method": "item/completed", "params": {**identity,
                "item": {"type": "agentMessage", "text": "WRONG"}}})
            client.events.put({"method": "turn/completed", "params": {**identity,
                "turn": {"status": "completed"}}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
            "turnId": "original", "turn": {"id": "old", "status": "completed"}}})
        client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "original",
            "item": {"type": "agentMessage", "text": "EXACT RESULT"}}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
            "turn": {"id": "original", "status": "completed"}}})
        self.assertEqual(client.ask("thread", "task", "model", "high"), "EXACT RESULT")
        self.assertEqual(methods, ["turn/start"])

    def test_status_history_deadline_is_not_a_task_failure(self):
        client, clock, methods, probes = self.client()
        original_read = client._read_turn
        first = True
        def read(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise RecoveryRequired("Codex 原回合历史查询超时")
            return original_read(*args, **kwargs)
        client._read_turn = read
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertEqual(len(probes), 1)

    def test_repeated_or_regressing_usage_does_not_claim_progress(self):
        client, clock, methods, _ = self.client(status="inProgress")
        def get(timeout):
            clock.value += 1
            return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread",
                "turnId": "original", "tokenUsage": {"total": {"totalTokens": 10 if clock.value % 2 else 5}}}}
        client.events.get = get
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(TimeoutError):
                client._ask_once("thread", "task", "model", "high", schema=None,
                    readonly=True, interactive=False, timeout=20, progress=None)
        self.assertLess(clock.value, 25)
        self.assertEqual(client.last_usage["total"]["totalTokens"], 10)
        self.assertEqual(methods, ["turn/start"])

    def test_thread_only_usage_cannot_change_this_turns_usage_or_progress(self):
        client, clock, methods, _ = self.client(status="inProgress")
        def get(timeout):
            clock.value += 1
            return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread",
                "tokenUsage": {"total": {"totalTokens": clock.value * 100}}}}
        client.events.get = get
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(TimeoutError):
                client._ask_once("thread", "task", "model", "high", schema=None,
                    readonly=True, interactive=False, timeout=20, progress=None)
        self.assertEqual(clock.value, 20)
        self.assertEqual(client.last_usage, {})
        self.assertEqual(methods, ["turn/start"])

    def test_network_interrupt_retries_only_confirmed_sandboxed_read_only_turn(self):
        for full_access in (False, True):
            with self.subTest(full_access=full_access):
                client, _, _, _ = self.client()
                client.full_access = full_access
                client.events = queue.Queue()
                client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
                    "turn": {"id": "original", "status": "interrupted",
                             "error": {"message": "stream disconnected"}}}})
                client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "retried",
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "RETRIED RESULT"}}})
                client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
                    "turn": {"id": "retried", "status": "completed"}}})
                methods = []
                def rpc(method, params=None, **kwargs):
                    methods.append(method)
                    if method == "turn/start":
                        return {"turn": {"id": "original" if methods.count(method)==1 else "retried"}}
                    if method == "thread/turns/list":
                        return {"data": [{"id": "original", "status": "interrupted"}]}
                    if method == "thread/resume":
                        return {}
                    raise AssertionError(method)
                client.rpc = rpc
                if full_access:
                    with self.assertRaises(RecoveryRequired):
                        client.ask("thread", "task", "model", "high", readonly=True)
                    self.assertEqual(methods.count("turn/start"), 1)
                else:
                    self.assertEqual(client.ask("thread", "task", "model", "high", readonly=True), "RETRIED RESULT")
                    self.assertEqual(methods.count("turn/start"), 2)
                    self.assertEqual(methods.count("thread/resume"), 1)
                self.assertNotIn("turn/interrupt", methods)

    def test_temporary_history_terminal_does_not_override_stream_completion(self):
        for status, empty in (("interrupted", False), ("completed", True), ("failed", False)):
            with self.subTest(status=status):
                client, clock, methods, _ = self.client(status=status, empty=empty)
                client.events = Events(clock, finish_at=31)
                original_get = client.events.get
                first = True
                def get(timeout):
                    nonlocal first
                    if first:
                        first = False
                        return {"method": "item/completed", "params": {"threadId": "thread", "turnId": "original",
                            "item": {"type": "agentMessage", "phase": "final_answer", "text": "EXACT RESULT"}}}
                    return original_get(timeout)
                client.events.get = get
                with patch("codex_bridge.time.monotonic", clock.monotonic):
                    self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
                self.assertLess(clock.value, 40)
                self.assertEqual(methods.count("turn/start"), 1)

    def test_whitespace_or_commentary_is_never_a_successful_final_answer(self):
        for item in ({"type": "agentMessage", "text": " \n "},
                     {"type": "agentMessage", "phase": "commentary", "text": "I will work on it"}):
            with self.subTest(item=item):
                client, _, _, _ = self.client()
                client.events = queue.Queue()
                client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "original", "item": item}})
                client.events.put({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "original", "status": "completed"}}})
                def rpc(method, params=None, **kwargs):
                    if method == "turn/start":
                        return {"turn": {"id": "original"}}
                    return {"data": [{"id": "original", "status": "completed", "items": [item]}]}
                client.rpc = rpc
                with self.assertRaises(RecoveryRequired):
                    client.ask("thread", "task", "model", "high")
                with self.assertRaises(RecoveryRequired):
                    client._recover_turn("thread", "original", None, 10, False)

    def test_final_phase_wins_without_duplicate_text(self):
        client, _, _, _ = self.client()
        client.events = queue.Queue()
        items = [{"type": "agentMessage", "phase": "commentary", "text": "Working"},
                 {"type": "agentMessage", "phase": "final_answer", "text": "FINAL"}]
        for item in items:
            client.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "original", "item": item}})
        client.events.put({"method": "turn/completed", "params": {"threadId": "thread",
            "turn": {"id": "original", "status": "completed", "items": items}}})
        self.assertEqual(client.ask("thread", "task", "model", "high"), "FINAL")

    def test_stop_during_status_rpc_is_not_overridden_by_completed_history(self):
        client, clock, methods, _ = self.client()
        original_rpc = client.rpc
        def rpc(method, *args, **kwargs):
            result = original_rpc(method, *args, **kwargs)
            if method == "thread/turns/list":
                client.stop_event.set()
            return result
        client.rpc = rpc
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            with self.assertRaises(Stopped):
                client.ask("thread", "task", "model", "high", timeout=90)
        self.assertEqual(methods.count("turn/start"), 1)

    def test_malformed_metadata_is_not_a_task_failure(self):
        client, clock, methods, _ = self.client()
        original_rpc = client.rpc
        first = True
        def rpc(method, *args, **kwargs):
            nonlocal first
            if method == "thread/turns/list" and first:
                first = False
                return {"data": None}
            return original_rpc(method, *args, **kwargs)
        client.rpc = rpc
        with patch("codex_bridge.time.monotonic", clock.monotonic):
            self.assertEqual(client.ask("thread", "task", "model", "high", timeout=90), "EXACT RESULT")
        self.assertEqual(methods.count("turn/start"), 1)


if __name__ == "__main__":
    unittest.main()
