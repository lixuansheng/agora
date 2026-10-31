"""Build the desktop binary and optional allowlisted sharing ZIPs."""
import argparse
import hashlib
import importlib.metadata
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from app_paths import APP_NAME, VERSION


ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    "app_paths.py", "codex_bridge.py", "harness_bridge.py", "conductor_core.py",
    "codex_conductor.py", "agent_link.py", "diagnostics.py", "test_conductor.py",
    "test_desktop.py", "test_codex_liveness.py", "test_progress_regressions.py",
    "requirements.txt", "requirements-build.txt", "setup.cmd", "launch_codex_conductor.cmd",
    "build_release.py", "install_shortcut.ps1", "Create Desktop Shortcut.cmd", "RELEASE_NOTES.md",
    "MULTI_AGENT_RELIABILITY.md",
    "README.en.md", "CONTRIBUTING.md", "SECURITY.md", ".gitignore",
    "docs/ARCHITECTURE.md", "docs/EVALUATION.md", "docs/RELATED_PROJECTS.md",
    "docs/PUBLISH_CHECKLIST.md",
    "conductor.ico",
)
PORTABLE_FILES = ("install_shortcut.ps1", "Create Desktop Shortcut.cmd", "RELEASE_NOTES.md")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_licenses(destination, extras):
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", destination / "Python-LICENSE.txt")
    tk_license = Path(sys.base_prefix) / "tcl" / "tk8.6" / "license.terms"
    shutil.copy2(tk_license, destination / "Tk-LICENSE.txt")
    for name in ("PyYAML", "websocket-client", "tkinterdnd2", "pyinstaller"):
        dist = importlib.metadata.distribution(name)
        files = [p for p in dist.files or () if p.name.lower() in ("license", "license.txt", "copying.txt")]
        if not files:
            raise RuntimeError("No distribution license found for " + name)
        for index, relative in enumerate(files):
            shutil.copy2(dist.locate_file(relative), destination / f"{name}-{index}-LICENSE.txt")
    bundled_extras = [ROOT / "third_party_licenses" / name for name in ("OpenSSL-LICENSE.txt", "Tcl-LICENSE.txt")]
    for extra in [*(p for p in bundled_extras if p.is_file()), *extras]:
        shutil.copy2(extra, destination / extra.name)


def zip_directory(folder, destination):
    manifest = folder / "SHA256SUMS.txt"
    paths = sorted(p for p in folder.rglob("*") if p.is_file() and p != manifest)
    manifest.write_text("".join(f"{sha256(p)}  {p.relative_to(folder).as_posix()}\n" for p in paths), encoding="utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            archive.write(path, str(Path(folder.name) / path.relative_to(folder)))
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        sha256(destination) + "  " + destination.name + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--extra-license", type=Path, action="append", default=[])
    parser.add_argument("--skip-freeze", action="store_true")
    parser.add_argument("--freeze-only", action="store_true", help="Build the local EXE without creating sharing ZIPs")
    args = parser.parse_args()
    output, work = args.output_dir.resolve(), args.work_dir.resolve()
    if not args.freeze_only:
        output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    binary = work / "binary" / "CodexConductor" / "CodexConductor.exe"
    if not args.skip_freeze:
        version = tuple(int(part) for part in VERSION.split(".")) + (0,)
        version_info = work / "windows-version.txt"
        version_info.write_text(
            f"VSVersionInfo(ffi=FixedFileInfo(filevers={version!r}, prodvers={version!r}, "
            "mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), "
            "kids=[StringFileInfo([StringTable('040904B0', ["
            f"StringStruct('FileDescription', {APP_NAME!r}), "
            f"StringStruct('ProductName', {APP_NAME!r}), "
            f"StringStruct('FileVersion', {VERSION!r}), "
            f"StringStruct('ProductVersion', {VERSION!r}), "
            "StringStruct('InternalName', 'CodexConductor'), "
            "StringStruct('OriginalFilename', 'CodexConductor.exe')])]), "
            "VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n", encoding="utf-8")
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", "CodexConductor", "--distpath", str(binary.parent.parent),
            "--workpath", str(work / "pyinstaller"), "--specpath", str(work),
            "--collect-all", "tkinterdnd2",
            "--icon", str(ROOT / "conductor.ico"),
            "--version-file", str(version_info),
            "--add-data", str(ROOT / "conductor.ico") + ";.",
            str(ROOT / "codex_conductor.py"),
        ], cwd=ROOT, check=True)
    if not binary.is_file():
        raise FileNotFoundError(binary)
    runtime = binary.parent / "_internal"
    if not runtime.is_dir():
        raise FileNotFoundError("The folder build requires its _internal runtime: " + str(runtime))
    # Build workspaces can label new files low; the desktop launcher needs normal user integrity.
    subprocess.run(["icacls", str(binary), "/setintegritylevel", "M"], check=True, capture_output=True)
    if args.freeze_only:
        print(str(binary), flush=True)
        return
    # Fresh staging directories plus explicit lists keep histories, credentials and venvs out.
    for kind, files in (("windows", PORTABLE_FILES), ("source", SOURCE_FILES)):
        folder = work / f"codex-conductor-{kind}-v{VERSION}"
        if folder.exists():
            raise FileExistsError("Use a fresh work directory or remove only the old release staging folder: " + str(folder))
        folder.mkdir()
        for name in files:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, folder / name)
        readme = "SHARE_README.md" if kind == "windows" else "README.md"
        shutil.copy2(ROOT / readme, folder / "README.md")
        if kind == "windows":
            shutil.copy2(binary, folder / binary.name)
            shutil.copytree(runtime, folder / "_internal")
        else:
            shutil.copy2(ROOT / "SHARE_README.md", folder / "SHARE_README.md")
        copy_licenses(folder / "third_party_licenses", args.extra_license)
        archive = output / f"codex-conductor-{kind}-v{VERSION}.zip"
        zip_directory(folder, archive)
        print(f"{archive.name}: {archive.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    main()
