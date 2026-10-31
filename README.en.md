# Agora

A Windows desktop coordinator for Codex and DeepSeek Harness. Enter a task once, let agents analyze independently and review each other, then turn their findings into saved deliverables and acceptance checks.

Codex leads execution and integration. DeepSeek provides an independent perspective and suitable supporting work. The workflow records stages, model choices, evidence, file versions, handoffs, and unresolved requirements.

**The goal is better, usable outcomes through collaboration.** Each review turns useful findings into concrete actions on the current deliverable, followed by acceptance checks that guide delivery or further improvement.

**[Download the Windows x64 portable beta](https://github.com/lixuansheng/agora/releases/tag/v0.9.4-beta.1).** Extract the complete ZIP and open `CodexConductor.exe`. The portable package includes its Python runtime; connect your own Codex and DeepSeek Harness clients using the included guide.

## What you can do

- Enter a task once and coordinate model selection, conversations, and review.
- Develop a candidate deliverable, review it, apply changes, and verify the result.
- Drag in files and hand editing work between agents in sequence.
- Run separate projects in parallel with independent workspaces and conversations.
- Follow progress, model settings, evidence, and artifact versions in the desktop interface.

## Run

Requires Windows 10/11 x64 and an installed and authenticated Codex client. Dual-agent workflows additionally require a configured, running DeepSeek Harness desktop client. The current Harness adapter targets its 0.2.1-rc.2 local protocol. Running from source additionally requires Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe codex_conductor.py
```

Choose a dedicated project directory in Settings. Separate parallel projects require non-overlapping workspaces and separate conversations.

## Test

```powershell
$env:CONDUCTOR_DATA_DIR = Join-Path $env:TEMP 'agora-offline-tests'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -p 'test*.py'
```

This source passed 275 offline regression tests locally and in GitHub's Windows environment. See the [evaluation plan](docs/EVALUATION.md) for assessing collaboration outcomes separately from software regression tests.

This is the **0.9.4 beta**, available as source and a Windows portable package. It adds checks for actual file progress and new source evidence. A project license is pending selection; third-party notices are included in `third_party_licenses`.

[中文说明](README.md) · [Architecture](docs/ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md)
