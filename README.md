# Agora · 议庭

**输入一次任务，让 Codex 与 DeepSeek 围绕同一份成果分工、交叉审核、修改和核验。**

议庭是一个 Windows 桌面协作工具，面向论文准备、课程项目、代码和文档等本地任务。它把模型路由、相关对话续接、独立初答、交叉审核、文件交接和成果验收连接起来，让用户能追踪每一步实际做了什么。

**我们的核心目标：让多模型协作带来可检查、可使用的更好成果。** 讨论围绕具体问题和当前文件推进，每轮把有价值的意见转化为行动，再通过验收决定交付或继续完善。

[English](README.en.md) · [协作设计](docs/ARCHITECTURE.md) · [效果评估计划](docs/EVALUATION.md) · [同类项目研究](docs/RELATED_PROJECTS.md)

## 怎样推进任务

```text
用户目标与文件
      ↓
选择模型、相关对话和工作目录
      ↓
直接执行，或双方独立分析 → 交叉审核
      ↓
制作候选成果 → 检查 → 裁决具体行动
      ↑                     ↓
      └── 修改 / 补证 / 核验 ──┘
                            ↓
                       验收与交付
```

Codex 主导实施和整合，DeepSeek 提供独立分析、复核和适合分工的辅助工作。裁决结合当前成果、未解决问题和证据决定下一步。重要问题继续处理；剩余改进收益很小时可以交付，并保留具体记录。

## 功能

- **一次输入统一协作**：自动选择工作流，可手动指定模型、推理强度和相关对话。
- **围绕成果迭代**：先形成候选成果，再审核、修改、补证和验收。
- **文件拖入与顺序共同修改**：明确交接后接手编辑，保留备份及文件哈希。
- **独立项目并行**：使用多个窗口、互不重叠的工作目录和不同对话运行任务。
- **可追踪的过程**：查看阶段、模型、耗时、验收条件、成果版本和来源记录。
- **中断续接**：复用已完成阶段，核对原回合与文件后继续剩余工作。
- **快速、均衡与深度模式**：根据任务需要安排投入；正常推进的必要回合以完成任务为目标。

## 环境

- Windows 10/11，Python 3.12。
- 已安装并登录的 Codex CLI 或 Codex 桌面应用。
- 使用双模型时，需要已配置的 DeepSeek Harness 桌面应用并保持运行。

模型使用现有客户端账户与连接，费用沿用各客户端的计费方式。当前 Harness 适配器依据 0.2.1-rc.2 的本机协议实现；首次使用可在设置中检查连接与模型目录。

## 从源码启动

在解压或克隆后的目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe codex_conductor.py
```

也可以运行 `setup.cmd` 安装依赖，然后双击 `launch_codex_conductor.cmd`。

第一次使用时，在“设置”选择一个新建的项目工作目录，确认 Codex 和 Harness 的连接。先运行下面的小任务熟悉阶段与成果面板：

```text
请双方互审：编写一个 mean(values) 函数，非空数值序列返回算术平均值，
空序列抛出 ValueError。保存代码并运行 [1, 2]、[-1, 0]、[] 三个用例。
在最终答复给出代码位置和实际测试结果。
```

并行项目请选择互不重叠的目录，例如 `projects/paper` 和 `projects/course`，让各项目拥有独立的文件与对话。需要编辑文件时按任务开启写入权限；需要无人值守时可在设置中选择完全访问。

## Windows 程序

当前发布 **0.9.4 源码测试版**，可按上面的步骤直接启动。Windows 目录版可使用下方构建命令生成；运行时将 `CodexConductor.exe` 与 `_internal` 保存在一起。

源码版数据默认保存在当前程序目录；Windows 目录版通常使用 `%LOCALAPPDATA%/CodexConductor`，兼容已有的同目录数据库。可通过 `CONDUCTOR_DATA_DIR` 指定独立数据目录。任务数据库、设置和成果保存在本机，发布目录使用单独整理的源码。

## 测试与构建

离线测试不提交模型任务：

```powershell
$env:CONDUCTOR_DATA_DIR = Join-Path $env:TEMP 'agora-offline-tests'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -p 'test*.py'
```

本候选源码在 Windows 上通过 275 项离线回归，覆盖路由、互审顺序、验收、恢复、文件交接、并行隔离和桌面显示。模型效果用独立任务比较，见 [效果评估计划](docs/EVALUATION.md)。

构建程序：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_release.py --freeze-only --work-dir build
```

## 版本与参与

当前为 **0.9.4 源码测试版**，增加真实文件进展与原始证据增量校验。发布记录见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

欢迎提交可复现问题和改进建议，见 [CONTRIBUTING.md](CONTRIBUTING.md)。项目开发使用 AI 辅助，重视真实场景、明确的验收条件和可复现测试。

项目许可证待确定；第三方组件许可见 `third_party_licenses`。后续发布计划见 [PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md)。
