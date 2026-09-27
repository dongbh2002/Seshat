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
│   └── sessions/
│       ├── README.md                 会话文件说明：数据流、示例与每个字段的含义
│       └── <tenant_id>/<user_id>/    会话文件 <session_id>.json
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
│   ├── fixtures/PE.docx              测试与评估共用的示例论文
│   ├── test_cli.py                   默认装配与 CLI 多轮命令
│   ├── test_llm.py                   真实模型连通性
│   ├── test_read_document.py         打印并校验 DOCX 读取结果
│   ├── test_write_document.py        DOCX 修改、修订与版本保护
│   └── test_runtime.py               默认装配下的真实多轮对话（不触发工具）
│
└── backend/
    ├── __init__.py                   包标记
    ├── cli.py                        终端入口：启动参数 --tenant/--user（都不传为游客）、终端交互（横幅、输入、/new /sessions /resume 等命令），启动时新开会话
    │
    ├── bootstrap/                    装配：解析启动身份，按配置创建并组装各组件，供 CLI 等入口使用
    │   ├── identity.py               Identity、resolve_identity：启动参数解析为租户/用户身份，都不传时生成带时间码的游客用户；校验名字并统一小写
    │   ├── paths.py                  PROJECT_ROOT；resolve_project_path：配置路径按项目根目录解析；workspace_directory、session_directory：数据根目录下各用户的工作区与会话目录
    │   ├── runtime_factory.py        create_default_runtime：按身份准备文档目录（不存在则创建）与模型客户端；create_runtime：用给定身份、目录与客户端创建共享组件、注册工具与 Hook（评估复用）；create_session_manager：按身份为 Runtime 装配会话管理器（评估不调用）
    │   └── __init__.py               导出装配函数与路径常量
    │
    ├── eval/                         离线评估，不参与运行时
    │   ├── __init__.py               包说明
    │   └── compress/                 上下文压缩评估：脚本化模型在真实 Runtime 上跑固定剧本，对比多种策略
    │       ├── eval_config.yaml      评估参数：文档、预算、行为、参评策略（kind + 配置覆盖，用于消融）、输出目录
    │       ├── run_eval.py           入口：预算 × 行为 × 策略逐一运行，测量每次请求，写出报告
    │       ├── scripted_agent.py     脚本化模型：10 轮剧本（通读→约束→问章→编辑→…→回顾），只按上下文可见内容决定是否重读；SDK 同款响应
    │       ├── metrics.py            指标：块可见性、过时/重复正文、前缀复用与估计未缓存 token、需求命中与汇总
    │       ├── baselines.py          对照组策略：不压缩、滑动窗口、清理旧工具结果
    │       ├── __init__.py           包说明
    │       └── results/              运行生成：report.md（表格与指标定义）、results.json（逐步明细）
    │
    ├── config/
    │   ├── application_local.yaml    唯一配置来源：游客身份、数据根目录、模型（含窗口大小）、token 预算与压缩比例、审阅状态上限、文档参数、日志
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
    │   ├── engine.py                 HookEngine：同步生命周期 Hook；HookEvent、HookContext、HookScope 等
    │   └── __init__.py               导出 Hook 相关类
    │
    ├── prompts/                      只放模板
    │   ├── system_prompt.j2          系统指令
    │   ├── profile_prompt.j2         研究与写作画像
    │   ├── memory_prompt.j2          长期记忆（当前为空）
    │   ├── history_summary_prompt.j2 压缩第二级：早期轮次摘要的排版
    │   ├── review_state_prompt.j2    审阅工作状态的排版（每步注入上下文末尾）
    │   ├── section_summary_prompt.j2 章节摘要子任务的系统提示词
    │   └── section_review_prompt.j2  章节审阅子任务的系统提示词
    │
    ├── templating/
    │   ├── renderer.py               PromptRenderer：渲染 prompts/ 下的模板，注册自定义过滤器
    │   ├── filters.py                模板过滤器：oneline（压成单行）、truncate_middle（保留首尾截断）
    │   └── __init__.py               导出 PromptRenderer、PROMPT_DIRECTORY 及过滤器
    │
    ├── utils/                        按格式划分的底层能力，不含业务流程
    │   ├── __init__.py               包标记
    │   ├── time_id.py                时间码 ID（YYYYMMDD-HHMMSS-6位十六进制）：会话 ID 与游客用户名共用
    │   └── docx/
    │       ├── parser.py             DocxParser：OOXML 解析为内容块（读、写、索引共用）；判断块 ID 是否按位置生成
    │       ├── models.py             DocumentBlock：内容块模型，Markdown 与索引转换
    │       ├── editor.py             DocxEditor：按块 ID 执行 XML 修改，保存前为缺少 paraId 的段落补齐；DocumentEditError
    │       ├── files.py              路径越界校验、revision 计算
    │       ├── index.py              DocumentIndex：大纲、章节、块范围与块正文，按 revision 缓存
    │       └── __init__.py           导出 DOCX 底层能力
    │
    ├── session/                      随会话存在的状态及会话持久化，/new 新开；跨会话数据（画像、记忆）不放这里
    │   ├── review_state.py           ReviewStateStore：已审阅块、问题（含原文片段 excerpt）、用户决定、文档版本与块修改记录；判断读取中的块是否过时；无损导出与恢复
    │   ├── review_state_hook.py      ReviewStateHook：读取后登记文档；原地修改后记录被改的块并取消其审阅标记
    │   ├── repository.py             SessionRecord、SessionRepository：一个会话一个 JSON 文件，按 ID 读写、原子替换、列出
    │   ├── manager.py                SessionManager：当前会话的新建、恢复、列出，每轮结束自动保存；SessionStateful：所依赖的运行时接口
    │   └── __init__.py               导出审阅状态与会话管理相关类
    │
    ├── llm_tasks/                    不属于 agent 循环的独立 LLM 调用
    │   ├── base.py                   LLMTask：渲染提示词 → 单次调用模型 → 解析 JSON
    │   ├── section_summarizer.py     单章摘要，按内容哈希缓存到磁盘
    │   ├── section_reviewer.py       单章审阅，返回问题列表
    │   └── __init__.py               导出子任务
    │
    ├── runtime/
    │   ├── runtime.py                Runtime：多轮对话入口，run 级 Hook；会话绑定与会话状态的清空、导出、恢复
    │   ├── agent_loop.py             AgentLoop：单次输入内的“模型 ↔ 工具”循环，每步把压缩后的历史与当前轮写回作为下一步输入，把压缩统计交给 Hook
    │   ├── tool_engine.py            ToolEngine：工具注册、超时执行、工具级 Hook
    │   ├── __init__.py               导出 Runtime、AgentLoop、ToolEngine、ContextEngine
    │   └── context/                  上下文引擎
    │       ├── engine.py             ContextEngine：每步组装发给模型的消息，持有历史归档，校验物理 token 上限，汇总压缩统计
    │       ├── review_state.py       ReviewStateContext：把审阅状态与大纲渲染为有上限的提示词
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
    │   ├── write_document/tool.py    工具 write_document：revision 校验与原子写入
    │   ├── update_review_state/tool.py 工具 update_review_state：记录已审阅范围、问题、状态、用户决定
    │   ├── list_findings/tool.py     工具 list_findings：查询完整问题列表
    │   ├── summarize_sections/tool.py 工具 summarize_sections：按章节摘要
    │   ├── review_sections/tool.py   工具 review_sections：按章节 map-reduce 审阅
    │   └── */__init__.py             导出对应工具类
    │
    └── skills/                       预留空目录，未被代码引用
```
