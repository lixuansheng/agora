# Agora · 议庭｜Windows 使用

一次输入，组织 Codex 与 DeepSeek 独立分析、交叉审核，并把修改意见落实到成果上。

版本：0.9.4 Beta。适用于 Windows 10/11 64 位，已包含 Python 运行环境。

1. 将压缩包完整解压到固定目录，保留 `CodexConductor.exe` 与 `_internal` 在一起。
2. 安装并登录 Codex；需要双方协作时启动已配置的 DeepSeek Harness。
3. 双击 `CodexConductor.exe`。在设置中选择专用项目目录并检查连接。
4. 输入任务，选择自动或双方互审；可拖入参考文件，再查看阶段进度及成果记录。
5. 多任务使用不同窗口、互不重叠的工作目录与独立对话。需要修改文件或无人值守时，按任务选择写入与完全访问设置。

初次测试可要求双方编写并测试一个均值函数，检查 `[1, 2]`、`[-1, 0]` 和空序列。完成后查看实际文件和验收结果。

模型沿用客户端账户与计费。个人设置、会话和任务成果保存在本机，程序目录版通常使用 `%LOCALAPPDATA%/CodexConductor`。

可双击 `Create Desktop Shortcut.cmd` 创建桌面快捷方式，之后保留解压目录即可。更新版本时解压到新目录，再运行新版本。

项目与反馈：https://github.com/lixuansheng/agora

下载更新：https://github.com/lixuansheng/agora/releases

压缩包旁的 `.sha256` 文件用于核对下载完整性，包内 `SHA256SUMS.txt` 列出各文件的 SHA-256。更新说明见 `RELEASE_NOTES.md`。
