# 议庭与同类开源项目：可吸收的设计

调研日期：2026-10-30（Asia/Shanghai）。范围：官方 GitHub 仓库、官方文档、具体实现与测试；未安装外部框架、未触发模型调用、未更改运行中的议庭。

## 结论

有可吸收的经验，而且议庭已经实现了其中相当一部分。后续最值得加强的是：**独立验收器、可重复的任务效果评估、准确显示各阶段和连接状态、公开仓库的可复现安装与测试**。当前没有证据表明必须增加第三个模型或换掉现有运行时。

议庭可采用的项目定位是：**面向本机 Codex 与 DeepSeek Harness 的成果驱动协作桌面工具**。侧重一次输入、独立初答、交叉审核、文件顺序交接、实际核验及中断恢复。这是清楚的使用场景；不需要把“多 Agent”本身当成唯一特色。

下列“建议补强”是对照本地文档的改进方向，具体是否已有部分实现应以主审的源码核对为准。已读取本地 `MULTI_AGENT_RELIABILITY.md`、`RELEASE_NOTES.md`、`README.md`。

## 项目对照

| 项目 | 本次实际查看的内容 | 适合议庭吸收的部分 | 议庭已有覆盖 | 应用方式与优先级 |
|---|---|---|---|---|
| [LangGraph](https://github.com/langchain-ai/langgraph) | 检查点接口、SQLite 保存器、Pregel 恢复循环 | 任务身份、检查点与待完成写入分离；并行子任务各自保留完成记录 | 任务/阶段账本、文件哈希、原回合恢复、未知写入不重放 | 固化中断恢复契约和故障注入用例；优先保持现有实现，不整体迁移。高 |
| [AutoGen / Magentic-One](https://github.com/microsoft/autogen) | 实际编排器、保存/加载状态、AutoGenBench | 停滞计数、换计划；固定初始条件与重复任务评估 | 已有停滞重规划与裁决循环 | 主要吸收评估方法，避免重复加同一种讨论循环。高 |
| [CrewAI](https://github.com/crewAIInc/crewAI) | Task 实现、函数与 LLM guardrail 区分、SQLite 流程保存 | 机器验收与模型评价分开，验证失败给具体反馈 | 已有必做验收、问题跨轮保留和失败拦截 | 对代码/文档/计算任务逐类增加可执行验收器。高 |
| [OpenHands / Agent Canvas](https://github.com/OpenHands/OpenHands) | 当前 README、事件存储、历史分页接口与事件测试 | 持久事件与临时流式内容分开；事件去重、分页、会话切换原子复位 | 已有游标分页、切换任务隔离、原回合事件匹配 | 借鉴状态与事件契约、加恢复/切换专项测试；无需移植 React 界面。中高 |
| [AionUi](https://github.com/iOfficeAI/AionUi) | 中文 README、Team Mode 设计、teamSendRuntime 源码 | 一眼看清成员、任务、阻塞和准备状态；单成员故障定向处理 | 已有双模型阶段进度、模型强度、多个并行窗口 | 优先改善状态可见性和能力自检；可作为产品交互参考。中高 |

## 1. LangGraph：保存对话不等于保存执行事实

本次核对的提交：`6f2df053f4487ce6ea8decc0d7482c8e46e0f132`。

第一手依据：

- [BaseCheckpointSaver 与 CheckpointTuple](https://github.com/langchain-ai/langgraph/blob/6f2df053f4487ce6ea8decc0d7482c8e46e0f132/libs/checkpoint/langgraph/checkpoint/base/__init__.py)：接口包含 `thread_id`、检查点与 `pending_writes`，`put_writes` 另外记录子任务中间写入。
- [SQLite 保存器](https://github.com/langchain-ai/langgraph/blob/6f2df053f4487ce6ea8decc0d7482c8e46e0f132/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py)：检查点表和写入表分开；写入以 thread、namespace、checkpoint、task、idx 为键。重复记录采用 `INSERT OR IGNORE` 或特定场景下的 replace。
- [执行恢复循环](https://github.com/langchain-ai/langgraph/blob/6f2df053f4487ce6ea8decc0d7482c8e46e0f132/libs/langgraph/langgraph/pregel/_loop.py)：保留恢复加载的写入及重应用任务记录，区分已完成节点和本次新执行。
- [官方持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)：区分线程级 checkpointer 与跨线程 store。

对议庭最实用的吸收：把“模型已回答”“文件已写”“已验收”“最后呈现给用户”继续作为分别记录的事实；断流后先核对对应阶段，再继续缺失步骤。应重点覆盖“一方完成另一方断线”“文件已变但完成通知丢失”“恢复时目录或文件已被人修改”三类故障。

议庭已有文件哈希、阶段复用和未知写入不重放，因此这里的主要工作是把已有规则变成更清楚的契约及故障矩阵，而非新增一套框架。LangGraph 的数据库去重也不自动保证任意外部软件操作只执行一次；外部副作用仍需回读或幂等机制。

许可证：[MIT](https://github.com/langchain-ai/langgraph/blob/6f2df053f4487ce6ea8decc0d7482c8e46e0f132/LICENSE)。本次只借鉴设计；若以后实际复制实现，保留适用版权和许可声明。

## 2. AutoGen / Magentic-One：停滞后换办法，效果用固定任务比较

核对提交：`d07c667a3239ea4ab7af62a53b0f4ff205a1f23d`。

- [Magentic-One 编排器](https://github.com/microsoft/autogen/blob/d07c667a3239ea4ab7af62a53b0f4ff205a1f23d/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py)：要求进展账本回答是否满足任务、是否推进、是否陷入循环和下一步；达到停滞阈值后更新事实与计划并进入外层循环。`save_state`/`load_state` 保存任务、事实、计划和停滞计数。
- [AutoGenBench](https://github.com/microsoft/autogen/blob/d07c667a3239ea4ab7af62a53b0f4ff205a1f23d/python/packages/agbench/README.md)：固定初始环境、重复预定义任务，记录执行日志后计算指标。README 指出其兼容早期 AutoGen 0.1/0.2，因此适合学方法，不能把它当作与议庭即插即用的现成评估器。

本地已经引用并实现相近的停滞机制。真正值得补的是小型效果基准：同一份输入分别运行单模型、一次互审、现有成果循环；在每次重新复制的独立目录中开始，由固定脚本检查输出，记录成功率、错误修复、调用数、耗时和可取得的 token 用量。以相同模型配置和验收条件比较，避免把不同版本、不同负载下的两次运行直接当成协作收益。

官方现状应准确表述：[本次 README](https://github.com/microsoft/autogen/blob/d07c667a3239ea4ab7af62a53b0f4ff205a1f23d/README.md) 已写明项目归档且不再由 Microsoft 维护，并引导 [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)。同次 GitHub 元数据的 `archived` 仍为 false；这是元数据与 README 的状态差异，故报告依据 README 描述，不宣称 API 已把仓库设为只读。

许可证要分开核对：[代码 MIT（LICENSE-CODE）](https://github.com/microsoft/autogen/blob/d07c667a3239ea4ab7af62a53b0f4ff205a1f23d/LICENSE-CODE)，[仓库根 LICENSE 为 CC BY 4.0](https://github.com/microsoft/autogen/blob/d07c667a3239ea4ab7af62a53b0f4ff205a1f23d/LICENSE)。不要只凭 GitHub 自动显示的一个 license 字段判断所有文件。本次不复制代码。

## 3. CrewAI：用可执行条件验证可执行成果

核对提交：`97bbfb64811775a965304bda45c8ec0e8e4ef3a2`。

- [Task 实现](https://github.com/crewAIInc/crewAI/blob/97bbfb64811775a965304bda45c8ec0e8e4ef3a2/lib/crewai/src/crewai/task.py)：任务输出经过 guardrail 流程，并有验证重试次数配置。
- [官方任务说明](https://github.com/crewAIInc/crewAI/blob/97bbfb64811775a965304bda45c8ec0e8e4ef3a2/docs/edge/en/concepts/tasks.mdx)：明确区分函数型检查与 LLM 文字型检查，前者运行确定的校验逻辑，后者适合语义或主观评价。
- [SQLite 流程持久化](https://github.com/crewAIInc/crewAI/blob/97bbfb64811775a965304bda45c8ec0e8e4ef3a2/lib/crewai/src/crewai/flow/persistence/sqlite.py)：有流程状态保存和锁协调，表明持久化是专门能力，不依赖聊天消息偶然留下状态。

议庭已有验收条件与强制失败拦截，下一步可在此基础上做少量可执行验收器：Python 输出跑测试；计算结果与独立计算比对；Word 检查文件能打开、指定段落/表格存在并保存渲染；专业软件任务核对实际保存文件和工具回读。每个验收记录附对应成果哈希，防止改过文件以后沿用旧通过结果。

这能直接服务用户“讨论是否让任务做得更好”的目标。语义性评价仍可以由模型承担；不用为每一种主观写作质量编造一个伪客观分数。

许可证：[MIT](https://github.com/crewAIInc/crewAI/blob/97bbfb64811775a965304bda45c8ec0e8e4ef3a2/LICENSE)。适合自有轻量实现验收接口，无需把整个 CrewAI 加到现有运行时。

## 4. OpenHands / Agent Canvas：历史事件与临时显示分开

核对提交：`7219c1991e9b47b6af7977bd7e9e5de1aa4f404b`。

- [当前 README](https://github.com/OpenHands/OpenHands/blob/7219c1991e9b47b6af7977bd7e9e5de1aa4f404b/README.md) 把主仓库称作 Agent Canvas，定位为可自托管的多编码 Agent 控制中心，支持 Codex 等多种 Agent；说明每会话执行环境与工作目录隔离是不同问题，共享目录仍会共享文件。
- [事件存储](https://github.com/OpenHands/OpenHands/blob/7219c1991e9b47b6af7977bd7e9e5de1aa4f404b/src/stores/use-event-store.ts)：按 ID 去重，批量历史插入后排序；`loadedConversationId` 与清空事件绑定更新；临时流式 slot 放在 UI 层，不提前占用持久消息的去重 ID。
- [事件分页接口](https://github.com/OpenHands/OpenHands/blob/7219c1991e9b47b6af7977bd7e9e5de1aa4f404b/src/api/event-service/event-service.api.ts)：返回原始分页信息用于读取较早记录，区分已持久化历史和运行时接口。
- [事件测试](https://github.com/OpenHands/OpenHands/blob/7219c1991e9b47b6af7977bd7e9e5de1aa4f404b/__tests__/stores/use-event-store.test.ts)：覆盖批量添加的去重和时间顺序，以及流式 slot 不污染持久日志。

对应用户以前的“不刷新、卡顿、reconnecting、切换后显示错任务”，最适合迁移的是规则：以持久事件为权威；实时流作为加速呈现；连接恢复后按游标补齐；切换任务时对“当前任务身份＋显示快照”做一致更新。议庭已有多项覆盖，应由专项测试确认，不必为此重写成网页或上 Docker。

许可证：[MIT](https://github.com/OpenHands/OpenHands/blob/7219c1991e9b47b6af7977bd7e9e5de1aa4f404b/LICENSE)。当前前端代码为 TypeScript/React，与 Tkinter 不直接兼容；优先移植事件设计与测试场景。

## 5. AionUi：和议庭最接近的产品层参考

核对提交：`11cd2a9a20d1e033157636efc570b41384c66d85`。它是另一个产品参考。用户先前下载的“allin”已在本机确认为 All in Luna，见下节。

- [中文官方 README](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/docs/readme/readme_ch.md) 描述统一管理多种 CLI Agent、独立会话、文件预览、远程入口；Team Mode 由 Leader 分配给 Teammate，经异步邮箱和共享任务看板协作。
- [团队运行体验 PRD](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/docs/prds/teams/team-runtime-experience.md)：按成员实例 `slot_id` 维持身份，设计准备/就绪/失败状态和明确的错误展示。这是设计文档，不能单独当成全部功能已实现的证据。
- [对应技术设计](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/docs/prds/teams/team-runtime-experience.design.md)：进一步说明当前接口事实和实现选择，应作为可借鉴思路读取。
- [teamSendRuntime 已有源码](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/packages/desktop/src/renderer/pages/team/components/teamSendRuntime.ts)：明确存在运行门控、排队数、状态文本，以及成员 `runtime_failed` 时的定向启动重试回调。

给议庭的实际启发：把“连接正常、模型正在工作、任务等输入、工具不可用、一个成员失败、整体任务失败”分开显示；只重试失败的成员或阶段，保留已完成成果。对第三方新用户，首次启动还应清楚展示依赖是否发现、连接是否可用、模型目录是否能读取，并给具体修复入口。

不建议立即用 AionUi 替代议庭：它的多 Agent 入口、文件预览、远程使用有参考价值，但本次资料没有证明其 Team Mode 已具备议庭现有的证据依赖账本、跨轮验收问题保留与顺序文件交接规则。可以先吸收成熟交互，继续保留议庭的成果验收流程。

许可证：[Apache-2.0](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/LICENSE)。若以后实际复制代码，需按其适用要求保留许可、版权及 NOTICE（如有），标明修改；本次没有把其代码引入 App。

## 推荐落地顺序

1. **先建立公开演示和效果证据**：挑 6–10 个已去除个人信息的真实小任务，含代码、计算、文件、证据查错；每个有独立验收标准。先做少量比较，记录原始结果，再决定是否扩大评估。
2. **补强机器可验证的验收接口**：先覆盖当前最常用的代码和文档任务，把检测结果绑定到成果版本；沿用议庭现有问题账本。
3. **稳定状态与连接可见性**：采用“持久记录＋增量呈现＋恢复补齐”的事件契约，明确每个模型当前在做什么及任务是否受阻。已有实现用回归矩阵复核。
4. **提升新机器的可复现性**：首次依赖检查、干净环境启动、公开 README、离线 CI、最小示例和版本兼容记录。公开发布先做早期可用版本，不必等多平台、手机端或第三模型。
5. **按收益扩展能力**：用户实际任务显示现有两模型的能力瓶颈后，再评估 Claude 或新提供方；当前先不增加付费依赖。

## 来源可追踪性与检索记录

上述源码链接均固定到本次读取的提交，避免 `main` 更新后结论失去定位。原始响应、源码快照与抓取结果保存在本报告同目录的 `source-fetch-*.json`、`*_repo.json`、`*_tree.json`、`*.txt`。所有成功抓取的提交日期均未晚于用户当地调研日期 2026-10-30；未据此宣称任何框架或模型为“最新最强”。

检索中已适配三处路径变化：CrewAI 的旧 `src/crewai/task.py` 返回 404，已查树定位到 `lib/crewai/src/crewai/task.py`；AionUi 根 `README.md` 返回 404，改读 `docs/readme/readme_ch.md`；LangGraph 的 durable-execution 文档地址本次重定向至 persistence，因此耐久执行细节以固定源码为依据。


## 本轮主审发现对应的优先修补

主审反馈：当前进展审计的成果 digest 基于模型文字摘要，而真实文件哈希另有保存但未参与推进判定；证据记录身份还混入 claim，重述同一来源可能产生“新增证据”。这两个问题应优先于增加模型或增加讨论轮次处理。

建议把三类量分别记录：

- **记录身份**：保留每条 claim/关系/引用的审计记录，允许它们不同。
- **原始证据身份**：以规范化来源位置＋原文片段/工具观测内容确定同一份证据，claim 改写不代表新增来源；来源依赖 key 继续作为依赖分组，不能拿任意新 key 当新证据。
- **成果身份**：文件任务以实际文件清单和内容哈希为准；仅文字交付可以用实际交付正文哈希。模型声称“已优化”或换一种摘要不构成文件已变化。

同一原文产生新推理或反例仍可能有价值，因此“无新增原始证据”不等于“绝无推理进展”。应分别传给裁决，并要求它指明解决的具体问题或可核验行动，避免从一种形式计数换成另一种形式计数。

这是依据议庭自身数据契约作出的修补建议，并非声称上述外部框架已经自动完成语义新颖性判定。

### AionUi 状态区分的实际源码补证

- [teamTypes.ts](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/packages/desktop/src/common/types/team/teamTypes.ts#L94)：`TeamRunStatus` 与 `TeamSlotWorkState` 分开；成员工作有 idle/queued/starting/running/paused/blocked；阻塞原因还区分 runtime_starting、runtime_failed、removing、session_stopped。存在 turn ID、开始时间和 slow 标记。
- [useTeamRunView.ts](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/packages/desktop/src/renderer/pages/team/hooks/useTeamRunView.ts#L215)：成员工作状态可独立于 team run 更新；代码明确说明 Leader 自行唤醒处理邮箱时仍可能有工作，应单独合并成员状态，防止团队终态留下的 running 快照一直不清。此文件展示的是前端状态消费，未据此审计其后端邮箱的一次投递语义。
- [useBlockerTaskResolver.ts](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/packages/desktop/src/renderer/pages/team/activity/useBlockerTaskResolver.ts)：看板 `blocked_by` 引用按需批量补取，沿已有 feed 建索引，收到 taskChanged 更新，切团队重置索引，不为显示依赖把所有任务一次拉完。
- [对应阻塞显示测试](https://github.com/iOfficeAI/AionUi/blob/11cd2a9a20d1e033157636efc570b41384c66d85/tests/unit/renderer/team/TaskCardBlocked.dom.test.tsx)：覆盖依赖任务信息的界面显示。

与本轮目录锁冲突相结合：并行任务因共享/父子目录被拦截，应展示为“资源被占用／需独立目录”，与网络断线、模型失败、待用户输入分开。资源冲突本身说明隔离规则正在工作，改进重点是让用户看懂原因与下一步。


## 已下载的 All in Luna：直接可用的验收契约思路

本机仓库：`work/allinluna`，remote 为 [zenx0x/allinluna](https://github.com/zenx0x/allinluna)，本次读取本地 HEAD 为 `b471713089d8352c1c97e05fe3d25907a4164b1a`。这里报告本地源码观察，不声称它是远端最新版本；没有运行其测试或接入议庭。

- [verification.py](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/plugins/allinluna/runtime/allinluna_runtime/verification.py)：把语义目标 `done_when` 和可执行/可观察的 `VerificationSpec` 分开。验证类型有 command、artifact、workspace、human、pack；每个 spec 用 `satisfies` 连接对应目标；普通自然语言不会自动被提升成 shell 命令。独立记录来源、执行和信任元信息。
- [verification_planner.py](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/plugins/allinluna/runtime/allinluna_runtime/verification_planner.py)：查找现有仓库检查入口，未被已知 spec 覆盖的条件保留在 `unresolved_conditions`；不能编造一个永远返回成功的命令来表示通过。未覆盖条件也不会自动制造必须人工批准的门槛。
- [实际契约测试](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/tests/unit/test_phase3_verification_contract.py)：检查没有 verification spec 时保持未验证；显式 human 条件保持人工边界；拒绝把“run the tests”这样的字符串当成命令；已验证语义条件通过检查回执 receipt ID 关联证据。
- [规划器测试](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/tests/unit/test_verification_planner.py)：覆盖发现现有 pytest、没有入口时保留未覆盖条件及来源元信息。
- [eval 数据校验脚本](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/scripts/validate_evals.py)：检查 trigger/behavior 样例结构、必要协议和行为声明；这是评估数据的有效性检查，本身不是证明模型实际完成了这些任务。
- [product-eval-results.json](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/evals/product-eval-results.json)：将 pass/fail/unknown/blocked 分开声明。该文件是一份记录；本次未运行所列 runner，因此不把它的 pass 当成自己复验结果。

最值得用于议庭的改进是一个轻量的验收映射：**用户目标 → 具体检查 → 真实执行/观察回执 → 当前成果版本 → 是否通过**。这样“模型认为没问题”和“该条件有独立证据支持”能在代码和界面中明确区分。这与本轮修复进展误判相衔接：既关注是否真的修改，也关注修改后的成果是否真的满足目标。

任务拓扑和依赖调度可以作为以后复杂项目的扩展方向；当前两个独立任务已能并行，不必马上复制它的整套顶层任务调度。先把现有双模型协作的验收链和状态显示做好，投入更集中。

许可证：[Apache-2.0](https://github.com/zenx0x/allinluna/blob/b471713089d8352c1c97e05fe3d25907a4164b1a/LICENSE)。本次只读与借鉴设计；任何后续代码复制另行保留适用声明。
