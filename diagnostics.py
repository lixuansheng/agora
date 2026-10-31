"""Read-only installation checks; no prompts or provider credentials are exported."""
import json
from pathlib import Path
import platform

from app_paths import DATA_DIR, FROZEN, VERSION
from conductor_core import Conductor, Store, load_settings


def write_diagnostics(path):
    report = {"version": VERSION, "standalone": FROZEN, "platform": platform.platform(), "data_dir": str(DATA_DIR)}
    root = None
    engine = None
    store = None
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        root.withdraw()
        report["tk_version"] = str(root.tk.call("info", "patchlevel"))
        report["drag_drop_ready"] = bool(root.tk.call("package", "require", "tkdnd"))
        settings = load_settings()
        report["workspace_exists"] = Path(settings["workspace"]).is_dir()
        store = Store()
        report["database_ready"] = True
        engine = Conductor(settings, store)
        harness_error = engine.refresh()
        report.update(codex_connected=True, codex_models=len(engine.codex_models),
                      codex_conversations=len(engine.codex_rows), harness_connected=not bool(harness_error),
                      harness_models=len(engine.harness_models), harness_conversations=len(engine.harness_rows),
                      selected_default_model=engine.default_model)
        if harness_error:
            report["harness_error"] = harness_error
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if engine:
            engine.codex.close()
        if store:
            store.conn.close()
        if root:
            root.destroy()
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
