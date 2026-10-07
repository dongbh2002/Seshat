# Seshat 文件说明

每个文件做什么。新增、删除、移动文件时同步更新本文。

```text
Seshat/
├── AGENTS.md                         开发约束
├── ARCHITECTURE.md                   本文：各文件的作用
├── README.md                         项目说明
├── pyproject.toml                    项目信息、依赖、pytest 配置
├── uv.lock                           uv 依赖锁定
├── .gitignore / .gitattributes       Git 忽略规则、属性
├── env/
│   └── .env.example                  模型环境变量示例（本地 .env 不提交）
├── logs/
│   └── example.log                   日志格式示例（运行日志不提交）
├── cache/
│   └── section_summaries.json        运行时生成的章节摘要缓存（不提交）
├── data/                             运行时数据根目录（yaml data.root），除两份 README 外不提交
│   ├── workspaces/
│   │   ├── README.md                 工作区说明
│   │   └── <tenant_id>/<user_id>/    用户工作区：待处理文档与修改产出，工具只能访问这里；启动时不存在则创建
│   ├── sessions/
│   │   ├── README.md                 会话文件说明：数据流、示例与每个字段的含义
│   │   └── <tenant_id>/<user_id>/    会话文件 <session_id>.json
│   ├── signals/<tenant_id>/<user_id>/      修改信号 signals.jsonl（只追加）与 ID 索引 signals.ids（去重）；游客不采集
│   ├── transcripts/<tenant_id>/<user_id>/  每个会话只追加的对话原文 <session_id>.jsonl；游客不记录
│   ├── snapshots/<tenant_id>/<user_id>/    见过的文档版本快照 <revision>.docx 与版本链 lineage.json
│   ├── memory/<tenant_id>/<user_id>/       用户级记忆 user.json、文档级记忆 documents/<paper_id>.json、各自的归档 *.archive.jsonl、信号处理进度 state.json
│   ├── tenants/<tenant_id>/                课题组共享数据：成员表 roster.json（作者名 → 导师/学生/其他）、课题组级记忆 memory.json 与归档 memory.archive.jsonl
│   └── global/                             通用级记忆 memory.json（含待审核与被拒绝的候选）与归档 memory.archive.jsonl
│
├── docs/
│   ├── 01-工具体系总览.md            工具基类、执行流程、工具清单
│   ├── 02-read_document文档读取.md   read_document 机制与返回字段
│   ├── 03-write_document文档修改.md  write_document 操作与返回字段
│   ├── 04-审阅状态工具.md            ReviewState、update_review_state、list_findings
│   ├── 05-章节工具.md                summarize_sections、review_sections
│   └── 06-上下文压缩机制.md          上下文压缩
│
├── tests/
│   ├── samples/PE.docx               测试用示例论文（IEEE 模板原始版本，测试中只修改临时副本）
│   ├── test_cli.py                   默认装配与 CLI 多轮命令
│   ├── test_llm.py                   真实模型连通性
│   ├── test_read_document.py         打印并校验 DOCX 读取结果
│   ├── test_write_document.py        DOCX 修改、修订与版本保护
│   └── test_runtime.py               默认装配下的真实多轮对话（不触发工具）
│
└── backend/
    ├── __init__.py                   包标记
    ├── cli.py                        终端入口：启动参数 --tenant/--user（都不传为游客）；管理员模式 --review-memory（逐条审核通用候选）、--maintain-memory（全量维护）；终端交互（横幅、输入、/new /sessions /resume /memory [show|edit|stats|archived|restore] /forget 等命令），启动时新开会话，退出时结束会话以整理记忆
    │
    ├── bootstrap/                    装配：解析启动身份，按配置创建并组装各组件，供 CLI 等入口使用
    │   ├── identity.py               Identity、resolve_identity：启动参数解析为租户（课题组）/用户（学生）身份，都不传时生成带时间码的游客（is_guest）；校验名字并统一小写
    │   ├── paths.py                  PROJECT_ROOT；resolve_project_path：配置路径按项目根目录解析；workspace/session/signal/transcript/snapshot_directory：各用户的数据目录；tenant_directory：课题组共享目录；memory_root、tenants_root、global_directory：记忆存储的根目录
    │   ├── runtime_factory.py        create_default_runtime：按身份准备文档目录与模型客户端，非游客启用学习组件；create_runtime：创建共享组件、注册工具与 Hook，learning 控制是否装配信号采集与四级记忆（评估复用，不启用）；create_session_manager：装配会话管理器，非游客另记录对话原文；create_memory_curator、create_memory_promotion、create_memory_maintenance：CLI 的记忆管理、通用候选审核与全量维护
    │   └── __init__.py               导出装配函数与路径常量
    │
    ├── eval/                         离线评估，不参与运行时
    │   ├── __init__.py               包说明
    │   └── compress/                 上下文压缩评估：脚本化模型在真实 Runtime 上跑固定剧本，对比多种策略
    │       ├── eval_config.yaml      评估参数：文档、预算、行为、参评策略（kind + 配置覆盖，用于消融）、输出目录
    │       ├── pe.docx               评估文档，与 results/ 中已有报告对应，保持不变以便复现
    │       ├── run_eval.py           入口：预算 × 行为 × 策略逐一运行，测量每次请求，写出报告
    │       ├── scripted_agent.py     脚本化模型：10 轮剧本（通读→约束→问章→编辑→…→回顾），只按上下文可见内容决定是否重读；SDK 同款响应
    │       ├── metrics.py            指标：块可见性、过时/重复正文、前缀复用与估计未缓存 token、需求命中与汇总
    │       ├── baselines.py          对照组策略：不压缩、滑动窗口、清理旧工具结果
    │       ├── __init__.py           包说明
    │       └── results/              运行生成：report.md（表格与指标定义）、results.json（逐步明细）
    │
    ├── config/
    │   ├── application_local.yaml    唯一配置来源：游客身份、数据根目录与文件锁、模型（含窗口大小）、token 预算与压缩比例、审阅状态上限、文档参数（含默认修订作者）、信号采集阈值、记忆（记忆 agent 输入上限、原文泄露检查、依据权重与每轮支持分上限、晋升门槛、衰减与休眠、归档阈值与容量、整理门槛与规模、各级注入上限与新条目名额）、日志
    │   ├── settings.py               类型化 Settings dataclass 与字段校验
    │   ├── config.py                 读取 YAML 并构造 Settings
    │   └── __init__.py               导出配置类和加载函数
    │
    ├── providers/
    │   ├── client.py                 按配置读取环境变量，创建 OpenAI 兼容模型客户端
    │   └── __init__.py               导出 create_model_client
    │
    ├── logging/
    │   ├── config.py                 日志级别、路径、轮转配置
    │   ├── formatter.py              单行 JSON 日志格式化与 log_event
    │   ├── hook.py                   LoggingHook：记录全部生命周期事件；模型请求前附带本步压缩统计（context）
    │   └── __init__.py               导出日志入口
    │
    ├── hooks/                        基础层：runtime、logging、session 共用
    │   ├── engine.py                 HookEngine：同步生命周期 Hook；HookEvent（含会话结束 SESSION_END）、HookContext、HookScope 等
    │   └── __init__.py               导出 Hook 相关类
    │
    ├── prompts/                      只放模板
    │   ├── system_prompt.j2          系统指令
    │   ├── profile_prompt.j2         研究与写作画像（用户级记忆中的事实）
    │   ├── memory_prompt.j2          长期记忆：文档（只适用于对应文档）、导师要求、课题组共性、用户、通用五部分生效条目及冲突优先级，没有记忆时为空
    │   ├── history_summary_prompt.j2 压缩第二级：早期轮次摘要的排版
    │   ├── review_state_prompt.j2    审阅工作状态的排版（每步注入上下文末尾），含待用户确认来源的文档版本
    │   ├── section_summary_prompt.j2 章节摘要子任务的系统提示词
    │   ├── section_review_prompt.j2  章节审阅子任务的系统提示词，含长期记忆中的要求
    │   ├── memory_extract_prompt.j2  记忆 agent 的系统提示词：各级定义、何时值得记、现有记忆与输出格式
    │   ├── memory_extract_input.j2   记忆 agent 的用户消息：本轮对话与工具调用、按来源排版的本轮新信号
    │   ├── memory_promote_prompt.j2  记忆归纳子任务的系统提示词：把多名学生或多个课题组中相同的规则归纳为上一级
    │   ├── memory_promote_input.j2   记忆归纳子任务的用户消息：待归纳的下级规则与上级已有记忆
    │   ├── memory_consolidate_prompt.j2 记忆整理子任务的系统提示词：合并、下线、改写的条件与输出格式
    │   └── memory_consolidate_input.j2  记忆整理子任务的用户消息：待整理条目及其支持数、最近支持时间、是否休眠
    │
    ├── templating/
    │   ├── renderer.py               PromptRenderer：渲染 prompts/ 下的模板，注册自定义过滤器
    │   ├── filters.py                模板过滤器：oneline（压成单行）、truncate_middle（保留首尾截断）、inline_diff（两段文本的增删差异）
    │   └── __init__.py               导出 PromptRenderer、PROMPT_DIRECTORY 及过滤器
    │
    ├── utils/                        按格式划分的底层能力，不含业务流程
    │   ├── __init__.py               包标记
    │   ├── time_id.py                时间码 ID（YYYYMMDD-HHMMSS-6位十六进制）：会话 ID 与游客用户名共用
    │   ├── file_lock.py              FileLock：独占创建锁文件的跨进程锁，带超时与残留锁清除；保护课题组、通用数据的读改写
    │   ├── text.py                   文本比较：字符片段覆盖度（记忆相关度）、Jaccard 相似度（整理时找相似条目）、最长连续相同片段（原文泄露检查）
    │   └── docx/
    │       ├── parser.py             DocxParser：OOXML 解析为内容块（读、写、索引共用），修订视图可接受全部、拒绝全部或只接受部分作者；记录块内修订作者与批注；判断块 ID 是否按位置生成
    │       ├── models.py             DocumentBlock：内容块模型（含修订作者、批注），Markdown 与索引转换
    │       ├── editor.py             DocxEditor：按块 ID 执行 XML 修改，保存前为缺少 paraId 的段落补齐；DocumentEditError
    │       ├── files.py              路径越界校验、revision 计算
    │       ├── index.py              DocumentIndex：大纲、章节、块范围、块正文与标题路径，按 revision 缓存；build_heading_paths
    │       ├── diff.py               align_blocks：两个版本的内容块对齐（稳定 ID → 正文相同 → 相似度），供版本比较
    │       └── __init__.py           导出 DOCX 底层能力
    │
    ├── session/                      随会话存在的状态及会话持久化，/new 新开；跨会话数据（画像、记忆）不放这里
    │   ├── review_state.py           ReviewStateStore：已审阅块、问题（含原文片段 excerpt，可按编号查询）、用户决定、文档版本与块修改记录；判断读取中的块是否过时；无损导出与恢复
    │   ├── review_state_hook.py      ReviewStateHook：读取后登记文档；原地修改后记录被改的块并取消其审阅标记
    │   ├── repository.py             SessionRecord、SessionRepository：一个会话一个 JSON 文件，按 ID 读写、原子替换、列出
    │   ├── manager.py                SessionManager：当前会话的新建、恢复、列出、关闭，每轮结束自动保存，离开会话前触发 SESSION_END；SessionStateful：所依赖的运行时接口
    │   └── __init__.py               导出审阅状态与会话管理相关类
    │
    ├── signals/                      修改信号采集（跨会话，游客不采集）：修订、批注、版本差异、建议结局与用户决定统一为 Signal，供记忆沉淀
    │   ├── models.py                 Signal：来源、修改者与角色、位置、修改前后原文、批注/建议、结局；按内容生成去重 ID
    │   ├── store.py                  SignalStore：按用户追加写 signals.jsonl，按 ID 索引文件去重；按字节位置读取新增信号、按 ID 查找
    │   ├── roster.py                 AuthorRoster：课题组成员表，Word 作者名 → advisor/student/other，组内共享，写入加文件锁
    │   ├── document.py               DocxVersion：一个版本的各修订视图、标题、内容指纹与内容键；fingerprint_similarity
    │   ├── lineage.py                DocumentLineage、VersionRecord：版本快照与版本链（所属论文、父版本、待确认状态），按内容匹配疑似历史版本
    │   ├── comparison.py             VersionComparer：三层比较——他人的修订与批注、Seshat 修订的去向、未开修订的直接修改
    │   ├── collector.py              FeedbackCollector：登记见到的版本（自动归入 / 新论文 / 待确认），确认后比较并写入信号，生成待确认提示
    │   ├── hooks.py                  VersionObserverHook：工具结果含 path 与 revision 时登记版本；ReviewFeedbackHook：问题结局与用户决定转为信号
    │   ├── transcript.py             TurnRecord、TurnCollector：收集一轮的用户输入、回复与工具调用，轮次结束交给处理函数；TranscriptWriter：按会话追加对话原文
    │   └── __init__.py               导出信号采集组件
    │
    ├── memory/                       四级记忆（文档、用户、课题组、通用，游客不启用）：每轮由记忆 agent 决定写入，会话结束时晋升，按会话注入上下文
    │   ├── models.py                 MemoryItem、MemoryScope：条目与归属；状态含生效、休眠、候选、已晋升、下线、拒绝；修改历史与替代条目；支持依据与反例依据各记录计入的分（同一依据只计一次），支持分、反例分由此求和；find_live：查找同内容条目；MemorySource：读取记忆的接口
    │   ├── store.py                  MemoryStore、ArchivedMemory：每个归属一个主文件（按修改时间缓存）与只追加的归档文件；归档、读取归档、恢复；写入加进程内锁与跨进程文件锁
    │   ├── scoring.py                MemoryScorer：得分 = 净支持分（支持减反例）× 衰减（用户、课题组级按半衰期）；休眠判定；注入选取（得分前列 + 新条目名额）；记忆 agent 输入选取（相关度 + 得分）
    │   ├── recorder.py               MemoryRecorder：把一轮对话、本轮新信号与相关的现有记忆交给记忆 agent，校验后写入文档/用户/课题组级（课题组级仅限导师信号支持；用户、课题组级拒绝照抄原文），依据按来源加权、每轮支持分封顶，信号按字节位置消费
    │   ├── promotion.py              MemoryPromotion：会话结束时用户→课题组共性的晋升与通用候选生成（成员计入净支持分；已有上级条目可由单个新成员强化）；通用候选的批准与拒绝。文档级不参与晋升
    │   ├── maintenance.py            MemoryMaintenance：休眠标记；整理本会话变动的条目及其相似条目（分块、有上限）；把已下线、已晋升、长期休眠与超出容量的条目移入归档；run_full 全量维护
    │   ├── retriever.py              MemoryRetriever：按会话涉及的文档选取各级生效记忆与画像（按得分并为新条目留名额）；审阅子任务只取被审文档的文档级记忆
    │   ├── curator.py                MemoryCurator、MemoryStats：用户查看（含本人依据与修改历史）、修改、删除、从归档恢复记忆，统计各级条目数、归档数与文件大小；通用级只读
    │   ├── hooks.py                  MemoryAgentHook：每轮结束后在后台串行调用记忆 agent；会话结束时等待完成、处理遗留信号、晋升课题组共性、标记休眠与整理、生成通用候选、归档治理，每步失败只记日志
    │   └── __init__.py               导出记忆组件
    │
    ├── llm_tasks/                    不属于 agent 循环的独立 LLM 调用
    │   ├── base.py                   LLMTask：渲染提示词 → 单次调用模型 → 解析 JSON
    │   ├── section_summarizer.py     单章摘要，按内容哈希缓存到磁盘
    │   ├── section_reviewer.py       单章审阅，返回问题列表；按优先级传入长期记忆中的要求
    │   ├── memory_extractor.py       记忆 agent 的模型调用：本轮对话、本轮新信号与现有记忆 → 新增 / 强化 / 修订 / 推翻操作
    │   ├── memory_promoter.py        记忆归纳：多个下级范围中相同的规则 → 上一级规则分组
    │   ├── memory_consolidator.py    记忆整理：一个归属下的全部条目 → 合并 / 下线 / 改写操作
    │   └── __init__.py               导出子任务
    │
    ├── runtime/
    │   ├── runtime.py                Runtime：多轮对话入口，run 级 Hook；会话绑定与会话状态的清空、导出、恢复
    │   ├── agent_loop.py             AgentLoop：单次输入内的“模型 ↔ 工具”循环，每步把压缩后的历史与当前轮写回作为下一步输入，把压缩统计交给 Hook
    │   ├── tool_engine.py            ToolEngine：工具注册、超时执行、工具级 Hook
    │   ├── __init__.py               导出 Runtime、AgentLoop、ToolEngine、ContextEngine
    │   └── context/                  上下文引擎
    │       ├── engine.py             ContextEngine：每步组装发给模型的消息（系统指令、画像与长期记忆、历史、审阅状态），持有历史归档，校验物理 token 上限，汇总压缩统计
    │       ├── review_state.py       ReviewStateContext：把审阅状态、大纲与待确认的文档版本渲染为有上限的提示词；VersionNoticeSource：待确认提示来源接口
    │       ├── token_estimator.py    TokenEstimator：字符 ÷ 比例估算 token；TokenCalibrationHook：按 usage 校准比例
    │       ├── __init__.py           导出上下文引擎组件
    │       └── compression/
    │           ├── base.py           CompressionStrategy 基类（结果写回、在自身输出上继续压缩）、CompressionResult、CompressionStats（本步增量统计）、HistoryArchive（已归档轮次）；按轮分组、工具调用名映射
    │           ├── read_results.py   read_document 结果的解析与按块清理（占位行、整条引用），纯消息变换，各策略复用
    │           ├── summary.py        HistorySummarizer：按规则提取轮次条目，在上限内渲染历史摘要
    │           ├── tiered.py         TieredCompressionStrategy：只负责分级调度——无损清理、第一级清正文、第二级归档、第三级按价值舍弃、兜底
    │           └── __init__.py       导出压缩策略
    │
    ├── tools/                        一个工具一个文件夹
    │   ├── base.py                   BaseTool、ToolImpact
    │   ├── section_tool.py           SectionTool：章节类工具基类（加载文档、选择章节）
    │   ├── __init__.py               导出全部工具
    │   ├── read_document/tool.py     工具 read_document：DOCX → 带块 ID 的 Markdown，索引记录每块位置，只返回相关的脚注与页眉页脚
    │   ├── write_document/tool.py    工具 write_document：revision 校验与原子写入，默认修订作者取自配置
    │   ├── update_review_state/tool.py 工具 update_review_state：记录已审阅范围、问题、状态、用户决定
    │   ├── list_findings/tool.py     工具 list_findings：查询完整问题列表
    │   ├── summarize_sections/tool.py 工具 summarize_sections：按章节摘要
    │   ├── review_sections/tool.py   工具 review_sections：按章节 map-reduce 审阅，子任务遵循长期记忆中的要求
    │   ├── confirm_document_version/tool.py 工具 confirm_document_version：用户确认文档版本来源、直接修改者与作者身份后采集修改信号（非游客注册）
    │   └── */__init__.py             导出对应工具类
    │
    └── skills/                       预留空目录，未被代码引用
```
