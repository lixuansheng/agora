"""Native Tk regression coverage for cross-process viewing and long transcripts."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from conductor_core import DEFAULTS, Store
from codex_conductor import ConductorApp, TkinterDnD


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "view.db")
        self.addCleanup(self.store.conn.close)
        self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.root.withdraw()
        with patch("codex_conductor.Store", return_value=self.store), \
                patch.object(Store, "recover_interrupted", return_value=[]), \
                patch("codex_conductor.load_settings", return_value={**DEFAULTS, "workspace": str(self.folder)}), \
                patch.object(ConductorApp, "refresh", lambda app: None):
            self.app = ConductorApp(self.root)
        self.app.engine.refresh = lambda: ""

    def tearDown(self):
        self.app.closing = True
        deadline = time.monotonic() + 3
        while self.app.snapshot_busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
            if not self.app.events.empty():
                while not self.app.events.empty():
                    kind, _ = self.app.events.get_nowait()
                    if kind in ("snapshot", "snapshot_error"):
                        self.app.snapshot_busy = False
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.assertEqual(self.callback_errors, [], "Unexpected Tk callback error")

    def wait_for(self, condition, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.005)
        self.fail("Native view did not reach expected state")

    def test_new_parallel_window_starts_a_separate_app_process(self):
        with patch("codex_conductor.subprocess.Popen") as spawn:
            self.app.new_parallel_window()
        command = spawn.call_args.args[0]
        self.assertEqual(Path(command[1]).name, "codex_conductor.py")
        self.assertTrue(spawn.call_args.kwargs["close_fds"])

    def select(self, jid):
        self.app.show_history()
        self.app.trees["history"].selection_set(str(jid))
        self.app.show_job()
        self.wait_for(lambda: self.app.view_job is not None and self.app.view_job["id"] == jid)

    def test_external_writer_updates_idle_view_and_completion(self):
        jid = self.store.create("external task")
        self.select(jid)
        with closing(sqlite3.connect(self.folder / "view.db")) as writer:
            with writer:
                writer.execute("INSERT INTO job_messages(job_id,created,stage,speaker,text,usage) VALUES(?,?,?,?,?,?)",
                               (jid, time.time(), "Codex 汇总执行", "model / max", '{"answer":"EXTERNAL FINAL"}', '{}'))
                writer.execute("UPDATE jobs SET status='completed' WHERE id=?", (jid,))
        self.wait_for(lambda: "EXTERNAL FINAL" in self.app.log.get("1.0", "end"))
        self.assertEqual(self.app.status.cget("text"), "任务完成")
        self.assertEqual(self.app.trees["history"].selection(), (str(jid),))
        self.app.refresh()
        self.wait_for(lambda: not self.app.snapshot_busy)
        self.assertEqual(self.app.log.get("1.0", "end").count("EXTERNAL FINAL"), 1)

    def test_busy_refresh_reads_records_without_network_or_engine_mutation(self):
        jid = self.store.create("busy task")
        self.select(jid)
        self.app.busy_ui(True)
        self.assertNotEqual(self.app.refresh_btn.cget("state"), "disabled")
        self.store.message(jid, "stage", "model / max", "WHILE BUSY")
        with patch.object(self.app.engine, "refresh", side_effect=AssertionError("active engine mutated")):
            self.app.refresh()
            self.wait_for(lambda: "WHILE BUSY" in self.app.log.get("1.0", "end"))
        other = self.store.create("other task")
        self.select(other)
        self.assertEqual(self.app.current_job, other)

    def test_old_snapshot_and_queued_text_cannot_bleed_into_other_job(self):
        first, second = self.store.create("first"), self.store.create("second")
        self.store.message(first, "stage", "m", "FIRST ONLY " * 20000)
        self.store.message(second, "stage", "m", "SECOND ONLY")
        self.select(first)
        self.app.select_job(second)
        self.wait_for(lambda: "SECOND ONLY" in self.app.log.get("1.0", "end"))
        self.assertNotIn("FIRST ONLY", self.app.log.get("1.0", "end"))

    def test_other_jobs_notifications_status_and_route_do_not_bleed(self):
        first, second = self.store.create("first"), self.store.create("second")
        self.app.engine.job_id = first
        self.app.busy_ui(True)
        self.select(second)
        for kind, value in (("notice", "OTHER NOTICE"), ("sessions", {"codex_id": "OTHER SESSION"}),
                            ("done", {"calls": 999}), ("failed", {"status": "failed", "error": "OTHER ERROR"})):
            self.app.engine.emit(kind, value)
        self.root.update()
        self.app.drain()
        self.assertNotIn("OTHER", self.app.log.get("1.0", "end"))
        self.assertNotIn("999", self.app.metrics.cget("text"))
        self.assertNotEqual(self.app.status.cget("text"), "任务失败")
        self.assertIn("尚未记录", self.app.route_label.cget("text"))

    def test_long_history_is_incremental_and_reselect_keeps_scroll(self):
        jid = self.store.create("long task")
        for i in range(19):
            self.store.message(jid, "stage", "m", f"MESSAGE-{i:02d}\n" + "readable evidence\n" * 1800)
        self.select(jid)
        heartbeat = []
        def tick():
            heartbeat.append(time.monotonic())
            if not self.app.closing:
                self.root.after(20, tick)
        tick()
        self.wait_for(lambda: "MESSAGE-18" in self.app.log.get("1.0", "end") and not self.app.render_queue, 12)
        self.assertGreater(len(heartbeat), 10)
        self.assertLess(max(b - a for a, b in zip(heartbeat, heartbeat[1:])), .6)
        self.assertEqual(self.app.log.get("1.0", "end").count("MESSAGE-00"), 1)
        self.app.log.yview_moveto(.2)
        before = self.app.log.yview()[0]
        generation = self.app.view_generation
        self.app.show_job()
        self.wait_for(lambda: not self.app.snapshot_busy)
        self.assertEqual(self.app.view_generation, generation)
        self.assertAlmostEqual(self.app.log.yview()[0], before, places=2)

    def test_result_shows_final_files_and_pending_without_changing_history(self):
        jid = self.store.create("planning task")
        artifacts = self.folder / "artifacts"
        artifacts.mkdir()
        (artifacts / "final.md").write_text("ACTUAL CONDITIONAL RESULT", encoding="utf-8")
        (artifacts / "actions.json").write_text(json.dumps([{"action": "improve", "files": [str(self.folder / "plan.md")]}]), encoding="utf-8")
        (artifacts / "final_acceptance.json").write_text(json.dumps({"accepted": False, "missing": ["real pending check"]}), encoding="utf-8")
        self.store.update(jid, status="needs_attention", route=json.dumps({"artifacts": str(artifacts)}))
        self.select(jid)
        text = self.app.summary.get("1.0", "end")
        self.assertIn("ACTUAL CONDITIONAL RESULT", text)
        self.assertIn("real pending check", text)
        self.assertIn("plan.md", text)
        self.assertEqual(self.store.job(jid)["status"], "needs_attention")
        self.assertEqual(self.app.result_folder, str(self.folder))
        self.root.deiconify()
        self.wait_for(lambda: self.app.summary.winfo_height() > 1)
        self.assertEqual(self.app.summary.yview()[0], 0)

    def test_invalid_result_records_do_not_stop_refresh_and_can_recover(self):
        jid = self.store.create("damaged result")
        artifacts = self.folder / "damaged"
        artifacts.mkdir()
        self.store.update(jid, route=json.dumps({"artifacts": str(artifacts)}))
        job = self.store.job(jid)
        cases = (("final_acceptance.json", None), ("final_acceptance.json", {"missing": None}),
                 ("actions.json", [None]), ("actions.json", [{"files": [None]}]),
                 ("actions.json", [{"deliverable": {}}]))
        for filename, data in cases:
            with self.subTest(data=data):
                path = artifacts / filename
                path.write_text(json.dumps(data), encoding="utf-8")
                result = self.app.read_result_artifacts(job)
                self.app.show_result(job, [], result)
                self.assertIn("暂不可读", self.app.summary.get("1.0", "end"))
                path.unlink()
        (artifacts / "final.md").write_text("RECOVERED RESULT", encoding="utf-8")
        self.select(jid)
        self.wait_for(lambda: "RECOVERED RESULT" in self.app.summary.get("1.0", "end"))
        self.store.message(jid, "stage", "model", "STILL REFRESHING")
        self.wait_for(lambda: "STILL REFRESHING" in self.app.log.get("1.0", "end"))

    def test_transient_artifact_read_error_is_retried_without_a_file_change(self):
        jid = self.store.create("temporarily locked result")
        artifacts = self.folder / "locked"
        artifacts.mkdir()
        final = artifacts / "final.md"
        final.write_text("UNCHANGED RESULT", encoding="utf-8")
        job = {**self.store.job(jid), "route": json.dumps({"artifacts": str(artifacts)})}
        with patch.object(Path, "read_text", side_effect=PermissionError("temporary lock")):
            self.assertTrue(self.app.read_result_artifacts(job).get("read_errors"))
        self.assertEqual(self.app.read_result_artifacts(job).get("final.md"), "UNCHANGED RESULT")

    def test_artifact_metadata_error_does_not_hide_history(self):
        jid = self.store.create("temporarily locked metadata")
        job = {**self.store.job(jid), "route": json.dumps({"artifacts": str(self.folder / "locked")})}
        with patch.object(Path, "stat", side_effect=PermissionError("temporary lock")):
            self.assertTrue(self.app.read_result_artifacts(job).get("read_errors"))

    def test_runtime_metrics_explain_soft_time_target(self):
        jid = self.store.create("soft time limit")
        self.app.current_job = self.app.engine.job_id = jid
        self.app.busy = True
        self.app.engine.started_at = time.monotonic() - 360
        self.app.engine.runtime["minutes"] = 5
        self.app.last_progress_tick = 0
        self.app.drain()
        self.assertIn("已到目标，完成任务优先", self.app.metrics.cget("text"))

    def test_settings_save_error_keeps_dialog_and_active_engine(self):
        self.app.settings_dialog()
        dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
        form = dialog.winfo_children()[0]
        save = next(widget for widget in form.winfo_children() if widget.cget("text") == "保存")
        engine = self.app.engine
        with patch("codex_conductor.save_settings", side_effect=PermissionError("locked")), \
                patch("codex_conductor.messagebox.showerror") as error:
            save.invoke()
        error.assert_called_once()
        self.assertIs(self.app.engine, engine)
        self.assertTrue(dialog.winfo_exists())

    def test_model_display_has_catalog_name_and_maps_back_to_exact_id(self):
        self.app.engine.harness_models = {("deepseek-official", "deepseek-flash"): {
            "name": "DeepSeek-V4.1-Flash", "reasoning": {"efforts": [{"id": "max"}]}}}
        self.app.engine.codex_models = {"lead": {}}
        self.app.engine.default_model, self.app.engine.default_effort = "lead", "high"
        self.app.catalog("")
        label = self.app.pickers["harness_model"].cget("values")[1]
        self.app.pickers["harness_model"].set(label)
        self.app.update_efforts()
        self.assertIn("DeepSeek-V4.1-Flash", label)
        self.assertEqual(self.app.model_value(label), "deepseek-official/deepseek-flash")
        self.assertIn("max", self.app.pickers["harness_effort"].cget("values"))
        self.assertIn("DeepSeek-V4.1-Flash", self.app.route_text({"mode": "dual", "harness_model": "deepseek-flash"}))


if __name__ == "__main__":
    unittest.main()
