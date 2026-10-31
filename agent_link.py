"""Task-scoped CLI for either desktop agent; no provider keys are accepted."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

from codex_bridge import CodexClient
from conductor_core import Conductor, Store, WorkflowBusy, load_settings, workflow_guard
from harness_bridge import HarnessClient


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=["codex", "harness", "workflow"])
    parser.add_argument("action", choices=["sessions", "models", "ask", "resume"])
    parser.add_argument("--job", type=int)
    parser.add_argument("--prompt-file")
    parser.add_argument("--session")
    parser.add_argument("--cwd")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--mode", choices=["auto", "codex", "dual", "astra"], default="auto")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.action in ("ask", "resume") and os.environ.get("CONDUCTOR_MANAGED_PHASE") == "1":
        parser.error("Already inside a managed Conductor phase. Return the current result; the outer coordinator schedules peer reviews. Do not start nested model calls.")
    settings = load_settings()
    cwd = str(Path(args.cwd or settings["workspace"]).resolve())
    if not Path(cwd).is_dir():
        parser.error("Working directory does not exist")
    prompt = ""
    if args.action == "ask":
        if not args.prompt_file:
            parser.error("ask requires --prompt-file")
        prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig")
        if not prompt.strip():
            parser.error("Prompt is empty")
    if args.provider == "workflow":
        if args.action not in ("ask", "resume"):
            parser.error("workflow supports ask or resume")
        if args.action == "resume" and not args.job:
            parser.error("resume requires --job")
        engine = Conductor({**settings, "workspace": cwd}, Store())
        try:
            result = engine.resume_run(args.job) if args.action == "resume" else engine.run(prompt, {"mode": args.mode})
            print(json.dumps({"job_id": engine.job_id, "answer": result}, ensure_ascii=False))
        except KeyboardInterrupt:
            engine.stop()
            raise
        finally:
            engine.codex.close()
        return
    if args.action == "resume":
        parser.error("resume is supported by workflow only")
    client = CodexClient(settings["codex_exe"]) if args.provider == "codex" else HarnessClient(settings["harness_home"], settings["harness_url"])
    client.full_access = bool(settings.get("full_access"))
    store = Store() if args.action == "ask" else None
    try:
        with ExitStack() as guards:
            if args.action == "ask":
                guards.enter_context(workflow_guard(shared_legacy=True, wait_seconds=5))
                guards.enter_context(workflow_guard(registration=True, wait_seconds=5))
            if store:
                store._mark_interrupted()
            if store and store.has_running_jobs():
                raise WorkflowBusy("议庭有受控任务正在运行；为避免模型会话或文件操作互相干扰，请等待任务完成")
            if args.provider == "codex":
                client.start()
            if args.action == "sessions":
                result = client.list_threads() if args.provider == "codex" else client.list_sessions()
            elif args.action == "models":
                result = client.models()
            elif args.provider == "codex":
                model = args.model or settings["default_model"]
                if model == "auto":
                    models = client.models()
                    model = next((row["model"] for row in models if row.get("isDefault")), models[0]["model"])
                if args.session:
                    client.resume(args.session)
                    sid = args.session
                else:
                    sid = client.create(model, cwd, "Agent link / " + prompt[:60])
                answer = client.ask(sid, prompt, model, args.effort or settings["default_effort"], readonly=True, interactive=False, timeout=args.timeout)
                result = {"session_id": sid, "answer": answer, "usage": client.last_usage}
            else:
                sid = args.session or client.create(cwd, "Agent link / " + prompt[:60])
                provider, model = (args.model or settings["harness_model"]).split("/", 1)
                selection = {"provider": provider, "model": model, "reasoningEffort": args.effort or settings["harness_effort"]}
                answer = client.ask(sid, prompt, selection, timeout=args.timeout)
                result = {"session_id": sid, "answer": answer, "usage": client.last_usage}
        print(json.dumps(result, ensure_ascii=False))
    except WorkflowBusy:
        parser.error("Conductor is already coordinating a task. Its outer coordinator owns peer scheduling; wait for it to finish before a standalone model call.")
    except KeyboardInterrupt:
        client.cancel()
        raise
    finally:
        if args.provider == "codex":
            client.close()
        if store:
            store.conn.close()


if __name__ == "__main__":
    main()
