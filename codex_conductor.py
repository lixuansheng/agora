"""Native desktop console for Codex and DeepSeek Harness."""
from __future__ import annotations

import json
import os
import ctypes
from collections import deque
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from conductor_core import Conductor, Store, RUNTIME_MODES, runtime_policy, attachment_paths, load_settings, save_settings, presentation_text
from app_paths import VERSION, DISPLAY_NAME, APP_DIR, resource_directory

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Source installs can still run without optional drag-and-drop support.
    DND_FILES, TkinterDnD = None, None


def parse_result_artifact(name, text):
    """Validate only the fields used by the result view; preserve full records on disk."""
    if name.endswith(".md"):
        return text
    data = json.loads(text)
    if name == "final_acceptance.json":
        if not isinstance(data, dict) or not isinstance(data.get("missing", []), list):
            raise ValueError("验收记录应为对象，missing 应为列表")
        if any(not isinstance(row, str) and not (isinstance(row, dict) and
                isinstance(row.get("criterion"), str)) for row in data.get("missing", [])):
            raise ValueError("待处理事项格式无效")
    elif name == "actions.json":
        if not isinstance(data, list):
            raise ValueError("行动记录应为列表")
        for row in data:
            if not isinstance(row, dict):
                raise ValueError("行动条目应为对象")
            candidate = row["action"] if isinstance(row.get("action"), dict) else row
            if (not isinstance(candidate.get("files", []), list) or
                    any(not isinstance(path, str) or not path for path in candidate.get("files", [])) or
                    not isinstance(candidate.get("deliverable", ""), str)):
                raise ValueError("成果内容或文件列表格式无效")
    return data


class ConductorApp:
    def __init__(self, root):
        self.root = root
        root.title(DISPLAY_NAME + " " + VERSION)
        icon = resource_directory() / "conductor.ico"
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        width, height = min(1180, root.winfo_screenwidth() - 40), min(800, root.winfo_screenheight() - 95)
        root.geometry(f"{width}x{height}+20+20")
        root.minsize(min(900, width), min(650, height))
        self.settings, self.store = load_settings(), Store()
        recovered = self.store.recover_interrupted()
        self.current_job = None
        self.events = queue.Queue()
        self.busy = self.refreshing = self.closing = False
        self.view_generation = self.message_cursor = 0
        self.snapshot_busy = False
        self.more_messages = False
        self.last_snapshot_tick = 0
        self.render_queue = deque()
        self.view_job = None
        self.view_final_answer = ""
        self.summary_signature = None
        self.pending_stage = None
        self.artifact_cache = {}
        self.model_labels = {}
        self.next_sessions = {"codex_thread": "auto", "harness_session": "auto"}
        self.engine = self.make_engine()
        self.build()
        if recovered:
            self.append(f"发现 {len(recovered)} 项中断记录。已保留完成的答复；请在“记录”查看。远端状态尚未确认，不会自动重发。", "notice")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(60, self.drain)
        self.refresh()

    def make_engine(self):
        engine = Conductor(self.settings, self.store, approval=self.approval)
        engine.emit = lambda k, v: self.events.put(("engine", (engine.job_id, k, v)))
        return engine

    def build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#f4f5f5", foreground="#202627")
        style.configure("TButton", padding=(9, 6))
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=28)
        style.map("Treeview", background=[("selected", "#d7eae4")], foreground=[("selected", "#183d31")])
        style.configure("Muted.TLabel", foreground="#5c6865")
        self.root.configure(bg="#f4f5f5")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        head = ttk.Frame(self.root, padding=(18, 14, 18, 10))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text=DISPLAY_NAME, font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.status = ttk.Label(head, text="正在连接", style="Muted.TLabel")
        self.status.pack(side="left", padx=18)
        self.settings_btn = ttk.Button(head, text="设置", command=self.settings_dialog)
        self.settings_btn.pack(side="right")
        self.parallel_btn = ttk.Button(head, text="新并行窗口", command=self.new_parallel_window)
        self.parallel_btn.pack(side="right", padx=8)
        self.refresh_btn = ttk.Button(head, text="刷新", command=self.refresh)
        self.refresh_btn.pack(side="right", padx=8)
        modes = ttk.Frame(self.root, padding=(18, 0, 18, 8))
        modes.grid(row=1, column=0, sticky="ew")
        self.mode = tk.StringVar(value="auto")
        for label, value in (("自动", "auto"), ("Codex", "codex"), ("双方互审", "dual"), ("最高能力指挥", "astra")):
            ttk.Radiobutton(modes, text=label, variable=self.mode, value=value).pack(side="left", padx=(0, 14))
        models = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        models.grid(row=2, column=0, sticky="ew")
        self.pickers = {}
        models.columnconfigure(1, weight=1)
        for row, (label, key, width) in enumerate((("Codex", "codex_model", 25), ("DeepSeek", "harness_model", 32))):
            ttk.Label(models, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            picker = ttk.Combobox(models, values=["auto"], state="readonly", width=width)
            picker.set("auto")
            picker.grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=2)
            self.pickers[key] = picker
            effort_key = "effort" if key == "codex_model" else "harness_effort"
            ttk.Label(models, text="强度").grid(row=row, column=2, padx=(0, 8))
            effort_picker = ttk.Combobox(models, values=["auto"], state="readonly", width=9)
            effort_picker.set("auto")
            effort_picker.grid(row=row, column=3, pady=2)
            self.pickers[effort_key] = effort_picker
        self.pickers["codex_model"].bind("<<ComboboxSelected>>", lambda e: self.update_efforts())
        self.pickers["harness_model"].bind("<<ComboboxSelected>>", lambda e: self.update_efforts())
        runtime = ttk.Frame(models)
        runtime.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(runtime, text="节奏").pack(side="left", padx=(0, 8))
        self.runtime_mode = ttk.Combobox(runtime, values=[p["label"] for p in RUNTIME_MODES.values()], state="readonly", width=6)
        self.runtime_mode.pack(side="left", padx=(0, 16))
        ttk.Label(runtime, text="目标时间（分钟，0 不限）").pack(side="left")
        self.time_minutes = ttk.Spinbox(runtime, from_=0, to=10080, width=6)
        self.time_minutes.pack(side="left", padx=(8, 16))
        ttk.Label(runtime, text="目标轮数").pack(side="left")
        self.round_limit = ttk.Spinbox(runtime, from_=1, to=50, width=5)
        self.round_limit.pack(side="left", padx=8)
        selected = self.settings.get("runtime_mode", "deep")
        self.runtime_mode.set(RUNTIME_MODES.get(selected, RUNTIME_MODES["deep"])["label"])
        self.runtime_mode.bind("<<ComboboxSelected>>", self.runtime_changed)
        self.runtime_changed()
        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.grid(row=3, column=0, sticky="nsew", padx=18)
        side = ttk.Frame(body, width=295)
        body.add(side, weight=0)
        self.search = tk.StringVar()
        ttk.Entry(side, textvariable=self.search).pack(fill="x", pady=(0, 8))
        self.search.trace_add("write", lambda *a: self.show_sessions())
        self.tabs = ttk.Notebook(side)
        self.tabs.pack(fill="both", expand=True)
        self.trees = {}
        for provider, label in (("codex", "Codex"), ("harness", "Harness"), ("history", "记录")):
            page = ttk.Frame(self.tabs)
            self.tabs.add(page, text=label)
            tree = ttk.Treeview(page, columns=("title",), show="headings", selectmode="browse")
            tree.heading("title", text="任务记录" if provider == "history" else "对话")
            tree.column("title", width=265, minwidth=140)
            scroll = ttk.Scrollbar(page, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            self.trees[provider] = tree
            if provider == "history":
                tree.bind("<<TreeviewSelect>>", self.show_job)
            else:
                tree.bind("<Double-1>", lambda e, p=provider: self.select_session(p))
                tree.bind("<Return>", lambda e, p=provider: self.select_session(p))
        choices = ttk.Frame(side, padding=(0, 8, 0, 0))
        choices.pack(fill="x")
        ttk.Button(choices, text="本次用所选", width=9, command=self.select_current).pack(side="left")
        ttk.Button(choices, text="新建", width=5, command=lambda: self.set_sessions("new")).pack(side="left", padx=5)
        ttk.Button(choices, text="自动", width=5, command=lambda: self.set_sessions("auto")).pack(side="left")
        self.selection = ttk.Label(side, text="对话：自动选择", wraplength=280, style="Muted.TLabel")
        self.selection.pack(fill="x", pady=8)
        self.resume_btn = ttk.Button(side, text="继续所选中断任务", command=self.resume_job)
        self.resume_btn.pack(fill="x", pady=(0, 8))
        main = ttk.Frame(body, padding=(14, 0, 0, 0))
        body.add(main, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        self.route_label = ttk.Label(main, text="", wraplength=720, style="Muted.TLabel")
        self.route_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        main.bind("<Configure>", lambda e: self.route_label.configure(wraplength=max(180, e.width - 28)))
        self.result_tabs = ttk.Notebook(main, width=1, height=1)
        self.result_tabs.grid(row=1, column=0, sticky="nsew")
        result_page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(result_page, text="本轮结果")
        self.open_result_btn = ttk.Button(result_page, text="打开成果目录", command=self.open_results, state="disabled")
        self.open_result_btn.pack(anchor="e", padx=8, pady=6)
        self.summary = tk.Text(result_page, width=1, state="disabled", wrap="word", bg="white", fg="#202627",
                               relief="flat", padx=14, pady=12, font=("Microsoft YaHei UI", 10))
        result_scroll = ttk.Scrollbar(result_page, command=self.summary.yview)
        self.summary.configure(yscrollcommand=result_scroll.set)
        result_scroll.pack(side="right", fill="y")
        self.summary.pack(fill="both", expand=True)
        frame = ttk.Frame(self.result_tabs)
        self.result_tabs.add(frame, text="对话")
        self.log = tk.Text(frame, width=1, height=10, state="disabled", bg="white", fg="#252d2b", relief="flat", wrap="word", padx=14, pady=12,
                           font=("Microsoft YaHei UI", 10), highlightthickness=1, highlightbackground="#d8dfdc")
        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("user", foreground="#08795f", font=("Microsoft YaHei UI", 11, "bold"))
        self.log.tag_configure("notice", foreground="#9a5a16")
        self.log.tag_configure("heading", foreground="#326558", font=("Microsoft YaHei UI", 10, "bold"))
        self.log.tag_configure("muted", foreground="#687470")
        progress_page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(progress_page, text="阶段进度")
        self.progress = ttk.Treeview(progress_page, columns=("state", "stage", "speaker", "elapsed"), show="headings", selectmode="browse")
        for key, label, width in (("state", "状态", 65), ("stage", "阶段", 155), ("speaker", "模型 / 强度", 260), ("elapsed", "耗时", 65)):
            self.progress.heading(key, text=label)
            self.progress.column(key, width=width, minwidth=55, stretch=key not in ("state", "elapsed"))
        progress_scroll = ttk.Scrollbar(progress_page, command=self.progress.yview)
        self.progress.configure(yscrollcommand=progress_scroll.set)
        progress_scroll.pack(side="right", fill="y")
        self.progress.pack(fill="both", expand=True)
        self.progress.bind("<Double-1>", self.show_stage)
        quality_page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(quality_page, text="成果进展")
        self.quality = tk.Text(quality_page, width=1, state="disabled", wrap="word", bg="white", fg="#202627", relief="flat", padx=14, pady=12,
                               font=("Microsoft YaHei UI", 10))
        quality_scroll = ttk.Scrollbar(quality_page, command=self.quality.yview)
        self.quality.configure(yscrollcommand=quality_scroll.set)
        quality_scroll.pack(side="right", fill="y")
        self.quality.pack(fill="both", expand=True)
        self.input = tk.Text(main, width=1, height=4, wrap="word", bg="white", fg="#202627", relief="flat", padx=12, pady=10,
                             font=("Microsoft YaHei UI", 11), highlightthickness=1, highlightbackground="#c6d3cd")
        self.input.grid(row=2, column=0, sticky="ew", pady=(10, 8))
        self.input.bind("<Control-Return>", self.submit)
        if DND_FILES:
            self.input.drop_target_register(DND_FILES)
            self.input.dnd_bind("<<Drop>>", self.drop_files)
        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, sticky="ew")
        self.send_btn = ttk.Button(actions, text="发送", command=self.submit)
        self.send_btn.pack(side="right")
        self.stop_btn = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=8)
        self.export_btn = ttk.Button(actions, text="导出记录", command=self.export_job, state="disabled")
        self.export_btn.pack(side="right")
        self.attach_btn = ttk.Button(actions, text="添加附件", command=self.choose_files)
        self.attach_btn.pack(side="left", padx=(0, 8))
        self.metrics = ttk.Label(actions, text="", style="Muted.TLabel")
        self.metrics.pack(side="left")
        foot = ttk.Frame(self.root, padding=(18, 10, 18, 12))
        foot.grid(row=4, column=0, sticky="ew")
        self.connection = ttk.Label(foot, text="", style="Muted.TLabel")
        self.connection.pack(side="right")
        self.path_label = ttk.Label(foot, text=self.settings["workspace"], style="Muted.TLabel")
        self.path_label.pack(side="left")
        foot.bind("<Configure>", lambda e: self.path_label.configure(wraplength=max(250, e.width - 300)))
        self.show_history()
        if self.jobs:
            latest = next(iter(self.jobs))
            self.trees["history"].selection_set(latest)
            self.tabs.select(2)
            self.show_job()
        self.last_progress_tick = 0

    def model_value(self, value):
        return self.model_labels.get(value, value)

    def harness_name(self, model, provider="deepseek-official"):
        row = self.engine.harness_models.get((provider, model), {})
        return row.get("name", model)

    def speaker_text(self, text):
        for (provider, model), row in self.engine.harness_models.items():
            text = text.replace(provider + "/" + model + " /", row.get("name", model) + " /", 1)
        return text

    def update_efforts(self):
        for model_key, effort_key in (("codex_model", "effort"), ("harness_model", "harness_effort")):
            chosen = self.model_value(self.pickers[model_key].get())
            if model_key == "codex_model":
                row = self.engine.codex_models.get(chosen, {})
                efforts = [item["reasoningEffort"] for item in row.get("supportedReasoningEfforts", [])]
            else:
                row = self.engine.harness_models.get(tuple(chosen.split("/", 1)), {})
                efforts = [item["id"] for item in row.get("reasoning", {}).get("efforts", [])]
            self.pickers[effort_key].configure(values=["auto"] + efforts)
            if self.pickers[effort_key].get() not in ["auto"] + efforts:
                self.pickers[effort_key].set("auto")

    def runtime_changed(self, event=None):
        mode = next(key for key, row in RUNTIME_MODES.items() if row["label"] == self.runtime_mode.get())
        preset = RUNTIME_MODES[mode]
        self.time_minutes.set(preset["minutes"])
        self.round_limit.set(self.settings["max_discussion_rounds"] if mode == "deep" else preset["rounds"])

    def route_text(self, route):
        text = "Codex " + route.get("codex_model", "-") + " / " + route.get("codex_effort", "未记录")
        if route.get("mode") == "dual":
            model = route.get("harness_model", "-")
            name = route.get("harness_model_name") or self.harness_name(model, route.get("harness_provider", "deepseek-official"))
            text += "\nDeepSeek " + name + " / " + route.get("harness_effort", "未记录")
            text += " · " + {"manual": "本次手动指定", "settings": "默认配置"}.get(route.get("harness_selection_source"), "历史已记录模型")
            if route.get("harness_helper_task"):
                text += " · 并行核证"
        if route.get("arbiter_model"):
            text += "\n裁决 " + route["arbiter_model"] + " / " + route.get("arbiter_effort", "未记录")
        if route.get("runtime"):
            profile = route["runtime"]
            target = "目标 " + str(profile["minutes"]) + " 分钟" if profile["minutes"] else "不限总时间"
            text += f"\n{profile['label']} · {target} · 目标 {profile['rounds']} 轮"
        if "full_access" in route:
            text += " · " + ("完全访问" if route["full_access"] else "按操作确认")
        return text

    def append(self, text, tag=""):
        follow = self.log.yview()[1] >= .99
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n\n", tag)
        if follow:
            self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.quality.configure(state="normal")
        self.quality.delete("1.0", "end")
        self.quality.configure(state="disabled")

    def show_quality(self, payload):
        judgment = payload.get("judgment")
        if judgment:
            text = payload["stage"] + "\n" + judgment.get("response", "")
            text += "\n下一步：" + judgment.get("next_step", "")
            for issue in judgment.get("open_issues", []):
                text += "\n" + issue["id"] + " / " + issue["status"] + "：" + issue["summary"]
        else:
            text = payload["stage"] + "\n实质变化：" + payload.get("changes", "")
            text += "\n验证证据：" + payload.get("evidence", "")
            for check in payload.get("checks", []):
                text += "\n" + check["status"] + "：" + check["criterion"] + "；" + check["evidence"]
            if payload.get("blocked"):
                text += "\n阻塞：" + payload["blocked"]
        self.quality.configure(state="normal")
        self.quality.insert("end", text + "\n\n")
        self.quality.see("end")
        self.quality.configure(state="disabled")

    def refresh(self):
        self.request_snapshot()
        if self.busy or self.refreshing:
            return
        self.refreshing = True
        self.connection.configure(text="正在刷新连接和模型")
        def load():
            try:
                self.events.put(("catalog", self.engine.refresh()))
            except Exception as exc:
                self.events.put(("refresh_error", str(exc)))
        threading.Thread(target=load, daemon=True).start()

    def new_parallel_window(self):
        if getattr(sys, "frozen", False):
            command = [sys.executable]
            working_directory = str(Path(sys.executable).resolve().parent)
        else:
            script = Path(__file__).resolve()
            command = [sys.executable, str(script)]
            working_directory = str(script.parent)
        try:
            subprocess.Popen(command, cwd=working_directory, close_fds=True)
        except OSError as exc:
            messagebox.showerror("新并行窗口", f"无法打开第二个窗口：{exc}", parent=self.root)

    def catalog(self, error=None):
        if error is None:
            error = self.engine.harness_error
        self.refreshing = False
        self.show_sessions()
        self.pickers["codex_model"].configure(values=["auto"] + list(self.engine.codex_models))
        chosen = self.model_value(self.pickers["harness_model"].get())
        self.model_labels = {self.harness_name(m, p) + " [" + p + "/" + m + "]": p + "/" + m
                             for p, m in self.engine.harness_models}
        self.pickers["harness_model"].configure(values=["auto"] + list(self.model_labels))
        self.pickers["harness_model"].set(next((label for label, value in self.model_labels.items() if value == chosen), chosen))
        self.update_efforts()
        if not self.current_job:
            provider, model = self.settings["harness_model"].split("/", 1)
            self.route_label.configure(text=f"Codex {self.engine.default_model} / {self.engine.default_effort}\nDeepSeek {self.harness_name(model, provider)} / {self.settings['harness_effort']}\n权限：" + ("双方完全访问" if self.settings.get("full_access") else "按操作确认"))
        elif self.view_job and self.view_job.get("route"):
            self.route_label.configure(text=self.route_text(json.loads(self.view_job["route"])))
        self.connection.configure(text=f"Codex {len(self.engine.codex_rows)} / Harness {len(self.engine.harness_rows)}" + (" · Harness 离线" if error else " · 已连接"))
        if error:
            self.append(error, "notice")

    def show_sessions(self):
        search = self.search.get().strip().lower()
        self.labels = {"codex": {}, "harness": {}}
        for provider, rows in (("codex", self.engine.codex_rows), ("harness", self.engine.harness_rows)):
            tree = self.trees[provider]
            tree.delete(*tree.get_children())
            for row in rows:
                sid = row["id"] if provider == "codex" else row["sessionId"]
                title = row.get("name") or row.get("preview") or row.get("projections", {}).get("values", {}).get("title") or sid
                self.labels[provider][sid] = str(title).replace("\n", " ")
                if not search or search in str(title).lower() or search in str(row.get("cwd", "")).lower():
                    tree.insert("", "end", iid=sid, values=(self.labels[provider][sid][:100],))

    def select_current(self):
        index = self.tabs.index(self.tabs.select())
        if index < 2:
            self.select_session("codex" if index == 0 else "harness")

    def select_session(self, provider):
        selected = self.trees[provider].selection()
        if selected and not self.busy:
            self.next_sessions["codex_thread" if provider == "codex" else "harness_session"] = selected[0]
            self.selection_label()

    def set_sessions(self, value):
        if not self.busy:
            self.next_sessions = {key: value for key in self.next_sessions}
            self.selection_label()

    def selection_label(self):
        parts = []
        for field, sid in self.next_sessions.items():
            provider = "codex" if field == "codex_thread" else "harness"
            label = {"auto": "自动", "new": "新建"}.get(sid) or self.labels[provider].get(sid, sid)[:25]
            parts.append(provider + "：" + label)
        self.selection.configure(text="本次 " + "\n".join(parts))

    def busy_ui(self, busy):
        self.busy = busy
        for widget in (self.send_btn, self.settings_btn, self.resume_btn):
            widget.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def submit(self, event=None):
        task = self.input.get("1.0", "end").strip()
        if not task or self.busy:
            return "break"
        if self.refreshing or not self.engine.codex_models:
            self.append("连接尚未就绪，请稍后重试或刷新。", "notice")
            return "break"
        controls = {**self.next_sessions, "mode": self.mode.get(), **{k: self.model_value(w.get()) for k, w in self.pickers.items()}}
        try:
            controls.update(runtime_mode=next(key for key, row in RUNTIME_MODES.items() if row["label"] == self.runtime_mode.get()),
                            time_minutes=self.time_minutes.get(), round_limit=self.round_limit.get())
            runtime_policy(controls, self.settings)
            self.settings["runtime_mode"] = controls["runtime_mode"]
            save_settings(self.settings)
            self.engine.prepare_run()
        except Exception as exc:
            self.append(str(exc), "notice")
            return "break"
        self.set_sessions("auto")
        self.input.delete("1.0", "end")
        self.select_job(None)
        self.clear_log()
        self.append("你\n" + task, "user")
        self.busy_ui(True)
        self.metrics.configure(text="")
        def run():
            try:
                self.engine.run(task, controls)
            except Exception:
                pass
            finally:
                self.events.put(("idle", None))
        threading.Thread(target=run, daemon=True).start()
        return "break"

    def drop_files(self, event):
        """Append dropped paths as read-only task attachments."""
        try:
            dropped = self.root.tk.splitlist(event.data)
        except tk.TclError:
            dropped = (event.data,)
        self.add_attachments(dropped)
        return "break"

    def choose_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="添加任务附件")
        if paths:
            self.add_attachments(paths)

    def add_attachments(self, dropped):
        paths = []
        for raw in dropped:
            try:
                path = Path(raw).expanduser()
                resolved = str(path.resolve())
                if path.is_file() and resolved not in paths:
                    paths.append(resolved)
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
        if not paths:
            self.append("没有识别到可读取的文件附件。", "notice")
            return
        existing = self.input.get("1.0", "end").strip()
        already = set(attachment_paths(existing)) if existing else set()
        new_paths = [path for path in paths if path not in already]
        if new_paths:
            suffix = ("\n\n" if existing else "") + "[附件]\n" + "\n".join("- " + path for path in new_paths)
            self.input.insert("end", suffix)

    def resume_job(self):
        if self.busy or self.refreshing:
            return
        selected = self.trees["history"].selection()
        if not selected:
            self.tabs.select(2)
            self.append("请在“记录”选中需要继续的中断任务。", "notice")
            return
        job_id = self.jobs[selected[0]]["id"]
        try:
            self.engine.prepare_run()
        except Exception as exc:
            self.append(str(exc), "notice")
            return
        self.append(f"继续任务 #{job_id}，保留已完成的阶段。", "notice")
        self.busy_ui(True)
        def run():
            try:
                self.engine.resume_run(job_id)
            except Exception as exc:
                self.events.put(("notice", str(exc)))
            finally:
                self.events.put(("idle", None))
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        if self.busy:
            self.stop_btn.configure(state="disabled")
            self.status.configure(text="正在停止")
            targets = self.engine.request_stop()
            threading.Thread(target=self.engine.finish_stop, args=(targets,), daemon=True).start()

    def show_history(self, rows=None):
        tree = self.trees["history"]
        self.jobs = {str(row["id"]): row for row in (self.store.recent() if rows is None else rows)}
        labels = {"completed": "完成", "needs_attention": "待处理", "running": "运行中", "failed": "失败", "stopped": "已停止", "interrupted": "已中断"}
        for obsolete in set(tree.get_children()) - self.jobs.keys():
            tree.delete(obsolete)
        for index, (jid, row) in enumerate(self.jobs.items()):
            values = ("#" + jid + " · " + labels.get(row["status"], row["status"]) + " / " + row["prompt"][:70].replace("\n", " "),)
            if tree.exists(jid):
                if tree.item(jid, "values") != values:
                    tree.item(jid, values=values)
                if tree.index(jid) != index:
                    tree.move(jid, "", index)
            else:
                tree.insert("", index, iid=jid, values=values)

    def show_job(self, event=None):
        selected = self.trees["history"].selection()
        if not selected:
            return
        self.select_job(int(selected[0]))

    def select_job(self, job_id):
        if job_id == self.current_job:
            self.request_snapshot()
            return
        self.current_job = job_id
        self.view_generation += 1
        self.message_cursor = 0
        self.more_messages = False
        self.render_queue.clear()
        self.summary_signature = None
        self.pending_stage = None
        self.view_job = None
        self.view_final_answer = ""
        self.route_label.configure(text="正在读取本次执行配置" if job_id else "")
        self.metrics.configure(text="")
        if job_id and self.trees["history"].exists(str(job_id)):
            self.trees["history"].selection_set(str(job_id))
        self.clear_log()
        self.progress.delete(*self.progress.get_children())
        self.export_btn.configure(state="normal" if job_id else "disabled")
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "正在读取本轮结果" if job_id else "")
        self.summary.configure(state="disabled")
        self.open_result_btn.configure(state="disabled")
        if job_id:
            self.request_snapshot()

    def request_snapshot(self):
        if self.closing or self.snapshot_busy:
            return
        self.snapshot_busy = True
        self.last_snapshot_tick = time.monotonic()
        generation, job_id, cursor = self.view_generation, self.current_job, self.message_cursor
        def load():
            try:
                payload = {"generation": generation, "job_id": job_id, "jobs": self.store.recent()}
                if job_id:
                    job = self.store.job(job_id)
                    payload.update(job=job, steps=self.store.steps(job_id),
                                   messages=self.store.messages(job_id, after_id=cursor, limit=8))
                    payload["artifacts"] = self.read_result_artifacts(job)
                self.events.put(("snapshot", payload))
            except Exception as exc:
                self.events.put(("snapshot_error", str(exc)))
        threading.Thread(target=load, daemon=True, name="agora-view").start()

    def read_result_artifacts(self, job):
        route = json.loads(job["route"]) if job.get("route") else {}
        folder = route.get("artifacts")
        if not folder:
            return {}
        paths = [Path(folder) / name for name in ("final.md", "final_acceptance.json", "actions.json", "deliverable.md")]
        result, signature = {}, []
        for path in paths:
            try:
                info = path.stat()
                signature.append((info.st_mtime_ns, info.st_size))
            except FileNotFoundError:
                signature.append(None)
            except OSError as exc:
                signature.append(None)
                result.setdefault("read_errors", []).append(path.name + "：" + str(exc))
        signature = tuple(signature)
        cached = self.artifact_cache.get(job["id"])
        if not result and cached and cached[0] == signature:
            return cached[1]
        for path, metadata in zip(paths, signature):
            if metadata is not None:
                try:
                    text = path.read_text(encoding="utf-8")
                    result[path.name] = parse_result_artifact(path.name, text)
                except (OSError, ValueError) as exc:
                    result.setdefault("read_errors", []).append(path.name + "：" + str(exc))
        if "read_errors" not in result:
            self.artifact_cache[job["id"]] = (signature, result)
        else:
            self.artifact_cache.pop(job["id"], None)
        return result

    def apply_snapshot(self, payload):
        self.snapshot_busy = False
        self.show_history(payload["jobs"])
        if payload["generation"] != self.view_generation or payload["job_id"] != self.current_job:
            self.request_snapshot()
            return
        job = payload.get("job")
        if not job:
            return
        if self.view_job is None:
            self.queue_text("你\n" + job["prompt"], "user")
        self.view_job = job
        self.more_messages = len(payload["messages"]) == 8
        for msg in payload["messages"]:
            self.queue_text(msg["stage"] + " / " + self.speaker_text(msg["speaker"]), "heading")
            self.queue_text(presentation_text(msg["stage"], msg["text"]))
            self.render_queue.append(("quality", msg, ""))
            self.message_cursor = msg["id"]
            if msg["stage"] in ("Codex 汇总执行", "Codex 执行"):
                self.view_final_answer = presentation_text(msg["stage"], msg["text"])
        self.show_progress(job["id"], payload["steps"])
        if job.get("route"):
            route = json.loads(job["route"])
            self.route_label.configure(text=self.route_text(route))
            self.path_label.configure(text=route.get("workspace", self.settings["workspace"]))
        else:
            self.route_label.configure(text="本任务尚未记录执行配置")
            self.path_label.configure(text=self.settings["workspace"])
        running = [step["stage"] for step in payload["steps"] if step["status"] == "running"]
        status = {"completed": "任务完成", "needs_attention": "已有记录 / 待处理", "failed": "任务失败", "stopped": "任务已停止", "interrupted": "任务已中断"}
        self.status.configure(text=("进行中：" + " / ".join(running) if running else status.get(job["status"], job["status"])))
        self.show_result(job, payload["steps"], payload["artifacts"])

    def queue_text(self, text, tag=""):
        text += "\n\n"
        for offset in range(0, len(text), 6000):
            self.render_queue.append(("text", text[offset:offset + 6000], tag))

    def render_pending(self):
        deadline = time.monotonic() + .012
        follow = self.log.yview()[1] >= .99
        self.log.configure(state="normal")
        count = 0
        while self.render_queue and count < 3 and time.monotonic() < deadline:
            kind, value, tag = self.render_queue.popleft()
            if kind == "text":
                self.log.insert("end", value, tag)
            else:
                try:
                    data = json.loads(value["text"])
                    if isinstance(data, dict) and "deliverable" in data:
                        self.show_quality({"stage": value["stage"], **data})
                    elif isinstance(data, dict) and "next_action" in data:
                        self.show_quality({"stage": value["stage"], "judgment": data})
                except (ValueError, KeyError, TypeError):
                    pass
            count += 1
        if follow:
            self.log.see("end")
        self.log.configure(state="disabled")
        self.scroll_to_stage()

    def show_result(self, job, steps, artifacts):
        labels = {"completed": "已完成", "running": "进行中", "needs_attention": "已交付阶段成果，仍有待处理事项", "failed": "失败", "stopped": "已停止", "interrupted": "已中断"}
        title = f"任务 #{job['id']} · {labels.get(job['status'], job['status'])}"
        final = artifacts.get("final.md", "") or self.view_final_answer
        acceptance = artifacts.get("final_acceptance.json", {})
        actions = artifacts.get("actions.json", [])
        latest = actions[-1] if isinstance(actions, list) and actions else {}
        candidate = latest.get("action") if isinstance(latest.get("action"), dict) else latest
        missing = list(filter(None, dict.fromkeys(row.get("criterion", "") if isinstance(row, dict) else str(row)
                                                 for row in acceptance.get("missing", []))))
        files = candidate.get("files", [])
        if job["status"] == "needs_attention" and not final and not files:
            title = f"任务 #{job['id']} · 待处理，尚无最终成果"
        text = title + "\n\n"
        if final:
            text += final
        else:
            running = [s["stage"] for s in steps if s["status"] == "running"]
            text += ("当前阶段：" + " / ".join(running) + "\n\n" if running else "")
            text += artifacts.get("deliverable.md") or candidate.get("deliverable", "尚未生成最终答复。")
        if files:
            text += "\n\n成果文件：\n" + "\n".join(files)
        if missing:
            text += "\n\n完整待处理记录：\n" + "\n".join("- " + item for item in missing)
        if job.get("error"):
            text += "\n\n运行说明：" + job["error"]
        if artifacts.get("read_errors"):
            text += "\n\n部分成果记录暂不可读：\n" + "\n".join(artifacts["read_errors"])
        if text != self.summary_signature:
            position = self.summary.yview()[0] if self.summary_signature is not None else 0
            self.summary.configure(state="normal")
            self.summary.delete("1.0", "end")
            self.summary.insert("1.0", text)
            self.summary.yview_moveto(position)
            self.summary.configure(state="disabled")
            self.summary_signature = text
        route = json.loads(job["route"]) if job.get("route") else {}
        self.result_folder = str(Path(files[0]).parent) if files else route.get("artifacts") or route.get("workspace")
        self.open_result_btn.configure(state="normal" if self.result_folder else "disabled")

    def open_results(self):
        if getattr(self, "result_folder", None):
            try:
                os.startfile(self.result_folder)
            except OSError as exc:
                messagebox.showerror("打开成果目录", str(exc), parent=self.root)

    def show_stage(self, event=None):
        selected = self.progress.selection()
        if not selected or not self.current_job:
            return
        stage = self.progress.item(selected[0], "values")[1]
        self.result_tabs.select(self.log.master)
        self.pending_stage = stage
        self.scroll_to_stage()

    def scroll_to_stage(self):
        if not self.pending_stage:
            return
        index = self.log.search(self.pending_stage + " / ", "1.0", stopindex="end")
        if index:
            self.log.see(index)
            self.pending_stage = None

    def show_progress(self, job_id, steps=None):
        labels = {"completed": "完成", "running": "运行中", "failed": "失败", "stopped": "已停止", "interrupted": "已中断"}
        steps = self.store.steps(job_id) if steps is None else steps
        current_ids = {str(step["id"]) for step in steps}
        for obsolete in set(self.progress.get_children()) - current_ids:
            self.progress.delete(obsolete)
        for step in steps:
            elapsed = max(0, (step["finished"] or time.time()) - step["started"])
            duration = f"{int(elapsed // 60)}:{int(elapsed % 60):02d}"
            values = (labels.get(step["status"], step["status"]), step["stage"], self.speaker_text(step["speaker"]), duration)
            if self.progress.exists(str(step["id"])):
                self.progress.item(str(step["id"]), values=values)
            else:
                self.progress.insert("", "end", iid=str(step["id"]), values=values)
        self.metrics.configure(text=f"{sum(s['status'] == 'completed' for s in steps)} 阶段")

    def export_job(self):
        if self.current_job is None:
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出任务记录", defaultextension=".md",
                                          initialfile=f"conductor-task-{self.current_job}.md", filetypes=[("Markdown", "*.md")])
        if path:
            try:
                Path(path).write_text(self.store.export_markdown(self.current_job), encoding="utf-8")
            except (OSError, ValueError) as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)

    def approval(self, method, params, cancel_event):
        reply = queue.Queue()
        self.events.put(("approval", (method, params, reply)))
        while not cancel_event.is_set() and not self.closing:
            try:
                return reply.get(timeout=.2)
            except queue.Empty:
                pass
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return {"decision": "cancel"}

    def ask_approval(self, payload):
        method, params, reply = payload
        if self.engine.cancelled.is_set():
            reply.put(None)
        elif method == "item/tool/requestUserInput":
            answers = {}
            for question in params.get("questions", []):
                options = "\n".join(o.get("label", "") + ": " + o.get("description", "") for o in question.get("options", []))
                answer = simpledialog.askstring("Codex 提问", question.get("question", "") + "\n" + options, parent=self.root)
                if answer is not None:
                    answers[question["id"]] = {"answers": [answer]}
            reply.put({"answers": answers})
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            answer = messagebox.askyesnocancel("Codex 操作确认", json.dumps(params, ensure_ascii=False, indent=2)[:7000], parent=self.root)
            reply.put({"decision": "accept" if answer else "cancel" if answer is None else "decline"})
        elif method == "item/permissions/requestApproval":
            answer = messagebox.askyesno("Codex 本回合权限", json.dumps(params.get("permissions", {}), ensure_ascii=False, indent=2)[:7000], parent=self.root)
            reply.put({"permissions": params.get("permissions", {}) if answer else {}, "scope": "turn"})
        else:
            self.append("未支持的交互请求：" + method, "notice")
            reply.put(None)

    def drain(self):
        if self.closing:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "engine":
                    job_id, kind, value = value
                    if kind not in ("job", "progress", "idle", "approval") and job_id != self.current_job:
                        continue
                if kind == "status":
                    if self.current_job == self.engine.job_id:
                        self.status.configure(text=value)
                elif kind == "job":
                    self.select_job(value)
                elif kind == "progress":
                    self.request_snapshot()
                elif kind == "message":
                    self.request_snapshot()
                elif kind == "quality":
                    self.request_snapshot()
                elif kind == "notice":
                    self.append(value, "notice")
                elif kind == "catalog":
                    self.catalog(value)
                    if not self.current_job:
                        self.status.configure(text="就绪" if not value else "Codex 就绪 / Harness 离线")
                elif kind == "refresh_error":
                    self.refreshing = False
                    self.connection.configure(text="模型目录连接失败，可重试；本地记录仍可查看")
                    self.append(value, "notice")
                elif kind == "snapshot":
                    self.apply_snapshot(value)
                elif kind == "snapshot_error":
                    self.snapshot_busy = False
                    self.status.configure(text="读取记录失败，正在重试：" + value[:120])
                elif kind == "route":
                    if self.current_job == self.engine.job_id:
                        self.route_label.configure(text=self.route_text(value))
                elif kind == "sessions":
                    self.append("Codex " + value["codex_id"] + ("\nHarness " + value["harness_id"] if value.get("harness_id") else ""), "muted")
                elif kind == "done":
                    self.status.configure(text="待处理：仍有阻塞或未解决问题" if value.get("needs_attention") else "任务完成")
                    self.metrics.configure(text=f"本次 {value['calls']} 个模型回合")
                elif kind == "failed":
                    self.status.configure(text="任务已停止" if value["status"] == "stopped" else "待处理：需核对原回合" if value["status"] == "needs_attention" else "任务失败")
                    self.append(value["error"], "notice")
                elif kind == "idle":
                    self.busy_ui(False)
                    self.catalog()
                    self.request_snapshot()
                elif kind == "approval":
                    self.ask_approval(value)
        except queue.Empty:
            pass
        if self.render_queue:
            self.render_pending()
        if not self.render_queue and self.more_messages and not self.snapshot_busy:
            self.more_messages = False
            self.request_snapshot()
        if time.monotonic() - self.last_snapshot_tick >= 1:
            self.request_snapshot()
        if self.busy and self.current_job == self.engine.job_id and time.monotonic() - self.last_progress_tick >= 1:
            if self.engine.started_at is not None:
                elapsed = int((time.monotonic() - self.engine.started_at) / 60)
                target = self.engine.runtime["minutes"]
                state = " · 已到目标，完成任务优先" if self.engine.time_due() else f" / 目标 {target} 分钟" if target else " · 不限时"
                self.metrics.configure(text=f"已用 {elapsed} 分钟{state}")
            self.last_progress_tick = time.monotonic()
        self.root.after(20 if self.render_queue else 60, self.drain)

    def settings_dialog(self):
        if self.busy or self.refreshing:
            return
        window = tk.Toplevel(self.root)
        window.title("连接与工作目录")
        window.transient(self.root)
        window.grab_set()
        form = ttk.Frame(window, padding=20)
        form.pack(fill="both", expand=True)
        fields = (("Codex 程序", "codex_exe"), ("工作目录", "workspace"), ("默认 Codex 模型", "default_model"),
                  ("默认推理强度", "default_effort"), ("Astra 模型", "astra_model"), ("Astra 推理强度", "astra_effort"),
                  ("Harness 主目录", "harness_home"), ("Harness 本机地址（留空自动发现）", "harness_url"),
                  ("默认 DeepSeek 模型", "harness_model"), ("默认 DeepSeek 强度", "harness_effort"),
                  ("讨论安全上限（轮）", "max_discussion_rounds"))
        values = {}
        for row, (label, key) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
            var = tk.StringVar(value=str(self.settings[key]))
            widget = ttk.Spinbox(form, textvariable=var, from_=1, to=50, width=8) if key == "max_discussion_rounds" else ttk.Entry(form, textvariable=var, width=50)
            widget.grid(row=row, column=1, sticky="w" if key == "max_discussion_rounds" else "ew", pady=6)
            values[key] = var
        def browse():
            path = filedialog.askdirectory(parent=window, initialdir=values["workspace"].get())
            if path:
                values["workspace"].set(path)
        ttk.Button(form, text="...", command=browse).grid(row=1, column=2, padx=5)
        allow = tk.BooleanVar(value=self.settings["allow_write"])
        ttk.Checkbutton(form, text="允许 Codex 按任务要求制作和改进成果", variable=allow).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=8)
        peer_edit = tk.BooleanVar(value=self.settings.get("peer_edit", True))
        ttk.Checkbutton(form, text="允许 DeepSeek 顺序接手修改成果（Codex 主导）", variable=peer_edit).grid(row=len(fields)+1, column=0, columnspan=2, sticky="w", pady=8)
        full = tk.BooleanVar(value=self.settings.get("full_access", False))
        ttk.Checkbutton(form, text="双方完全访问 / 不弹操作授权确认", variable=full).grid(row=len(fields)+2, column=0, columnspan=2, sticky="w", pady=8)
        def save():
            settings = {**self.settings, **{k: v.get().strip() for k, v in values.items()}, "allow_write": allow.get(), "peer_edit": peer_edit.get(), "full_access": full.get()}
            try:
                settings["max_discussion_rounds"] = int(settings["max_discussion_rounds"])
                if not 1 <= settings["max_discussion_rounds"] <= 50:
                    raise ValueError()
            except ValueError:
                messagebox.showerror("设置", "讨论安全上限须为 1 到 50 轮的整数", parent=window)
                return
            if not Path(settings["workspace"]).is_dir():
                messagebox.showerror("设置", "工作目录不存在", parent=window)
                return
            try:
                save_settings(settings)
            except OSError as exc:
                messagebox.showerror("设置", "保存失败，原设置仍保留，请重试：" + str(exc), parent=window)
                return
            self.engine.codex.close()
            self.settings = settings
            self.engine = self.make_engine()
            self.path_label.configure(text=settings["workspace"])
            window.destroy()
            self.refresh()
        ttk.Button(form, text="保存", command=save).grid(row=len(fields)+3, column=1, sticky="e")

    def close(self):
        self.closing = True
        self.engine.cancelled.set()
        self.engine.codex.stop_event.set()
        self.engine.harness.cancel_event.set()
        self.root.withdraw()
        finished = threading.Event()
        def finish():
            try:
                self.engine.stop()
                self.engine.codex.close()
            finally:
                finished.set()
        threading.Thread(target=finish, daemon=True).start()
        def check():
            if finished.is_set():
                self.root.destroy()
            else:
                self.root.after(100, check)
        check()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=DISPLAY_NAME + " desktop")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--diagnose", metavar="REPORT_JSON")
    args = parser.parse_args()
    if args.diagnose:
        from diagnostics import write_diagnostics
        write_diagnostics(args.diagnose)
        return
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodexConductor.Desktop")
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    ConductorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
