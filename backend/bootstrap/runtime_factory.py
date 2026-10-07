"""Runtime 装配：按配置与启动身份创建共享组件、注册工具与 Hook，返回可运行的 Runtime；并为其装配会话管理器。

非游客身份额外装配“学习”组件：修改信号采集（版本观察与审阅反馈 Hook、版本确认工具、审阅状态中的待确认提示、
对话原文记录）与四级记忆（按得分注入上下文与审阅子任务、每轮结束后的记忆 agent、会话结束时的晋升与维护）。
另提供 CLI 使用的记忆管理、通用候选审核与全量维护组件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from backend.bootstrap.identity import Identity
from backend.bootstrap.paths import (
    global_directory,
    memory_root,
    resolve_project_path,
    session_directory,
    signal_directory,
    snapshot_directory,
    tenant_directory,
    tenants_root,
    transcript_directory,
    workspace_directory,
)
from backend.config import Settings
from backend.hooks import HookEngine, HookScope
from backend.llm_tasks import (
    MemoryConsolidator,
    MemoryExtractor,
    MemoryPromoter,
    SectionReviewer,
    SectionSummarizer,
)
from backend.logging import LoggingHook
from backend.memory import (
    MemoryAgentHook,
    MemoryCurator,
    MemoryMaintenance,
    MemoryPromotion,
    MemoryRecorder,
    MemoryRetriever,
    MemoryScorer,
    MemoryStore,
)
from backend.providers import create_model_client
from backend.runtime import AgentLoop, Runtime, ToolEngine
from backend.runtime.context import (
    ContextEngine,
    ReviewStateContext,
    TieredCompressionStrategy,
    TokenCalibrationHook,
    TokenEstimator,
)
from backend.session import (
    ReviewStateHook,
    ReviewStateStore,
    SessionManager,
    SessionRepository,
)
from backend.signals import (
    AuthorRoster,
    DocumentLineage,
    FeedbackCollector,
    ReviewFeedbackHook,
    SignalStore,
    TranscriptWriter,
    TurnCollector,
    VersionComparer,
    VersionObserverHook,
)
from backend.templating import PROMPT_DIRECTORY, PromptRenderer
from backend.tools import (
    ConfirmDocumentVersionTool,
    ListFindingsTool,
    ReadDocumentTool,
    ReviewSectionsTool,
    SummarizeSectionsTool,
    UpdateReviewStateTool,
    WriteDocumentTool,
)
from backend.utils.docx import DocumentIndex


@dataclass(frozen=True)
class _Learning:
    """非游客运行时的学习组件：修改信号采集与四级记忆。"""

    collector: FeedbackCollector  # 修改信号采集编排。
    retriever: MemoryRetriever  # 按会话选取记忆。
    agent_hook: MemoryAgentHook  # 每轮调用记忆 agent，会话结束时晋升。


def create_default_runtime(settings: Settings, identity: Identity) -> Runtime:
    """为启动身份创建注册好文档工具、章节子任务和 Hook 的 Runtime。

    身份对应的工作区目录不存在时直接创建；非游客身份启用信号采集与记忆。

    Args:
        settings: 入口加载一次并注入各组件的类型化项目配置。
        identity: 启动身份，决定工作区目录与运行范围。

    Returns:
        可持续调用 ``run`` 进行进程内多轮对话的 Runtime。

    Raises:
        OSError: 工作区目录创建失败。
        KeyError: 模型环境变量缺失。
    """
    document_root = workspace_directory(settings, identity)
    document_root.mkdir(parents=True, exist_ok=True)

    client, model = create_model_client(settings=settings)
    return create_runtime(
        settings,
        identity=identity,
        document_root=document_root,
        client=client,
        model=model,
        learning=not identity.is_guest,
    )


def create_runtime(
    settings: Settings,
    *,
    identity: Identity,
    document_root: Path,
    client: OpenAI,
    model: str,
    learning: bool,
) -> Runtime:
    """用给定的身份、文档目录和模型客户端装配 Runtime，供默认入口与评估脚本复用。

    Args:
        settings: 类型化项目配置。
        identity: 运行身份，写入 Hook 运行范围。
        document_root: 工具允许访问的文档目录。
        client: OpenAI 兼容模型客户端。
        model: 实际模型名称。
        learning: 是否启用信号采集与记忆；游客与评估为 False。

    Returns:
        注册好文档工具、章节子任务和 Hook 的 Runtime。
    """
    request_options = settings.active_model.parameters
    documents = settings.documents
    renderer = PromptRenderer(PROMPT_DIRECTORY)
    review_state = ReviewStateStore()
    document_index = DocumentIndex(document_root, documents.section_heading_level)
    learned = (
        _create_learning(
            settings,
            identity=identity,
            document_root=document_root,
            client=client,
            model=model,
            renderer=renderer,
            review_state=review_state,
            document_index=document_index,
        )
        if learning
        else None
    )

    budget = settings.context.budget
    estimator = TokenEstimator(budget)

    hook_engine = HookEngine()
    LoggingHook().register(hook_engine)
    ReviewStateHook(review_state).register(hook_engine)
    TokenCalibrationHook(estimator).register(hook_engine)
    context_engine = ContextEngine(
        renderer=renderer,
        compression=TieredCompressionStrategy(
            settings.context.compression,
            budget.working_tokens,
            estimator,
            review_state,
            renderer,
        ),
        review_state_context=ReviewStateContext(
            settings.context.review_state,
            review_state,
            document_index,
            renderer,
            learned.collector if learned else None,
        ),
        estimator=estimator,
        physical_limit_tokens=(
            settings.active_model.context_window_tokens - budget.output_reserve_tokens
        ),
        memory=learned.retriever if learned else None,
    )
    summarizer = SectionSummarizer(
        client,
        model,
        request_options,
        renderer,
        cache_path=resolve_project_path(documents.summary_cache_path),
        max_chars=documents.summary_max_chars,
    )
    reviewer = SectionReviewer(client, model, request_options, renderer)
    tool_engine = ToolEngine(hook_engine=hook_engine)
    tool_engine.register(
        ReadDocumentTool(
            document_root,
            default_max_chars=documents.read_default_max_chars,
            max_chars_limit=documents.read_max_chars,
        )
    )
    tool_engine.register(
        WriteDocumentTool(document_root, default_author=documents.revision_author)
    )
    tool_engine.register(UpdateReviewStateTool(review_state, document_index))
    tool_engine.register(ListFindingsTool(review_state))
    tool_engine.register(SummarizeSectionsTool(document_index, summarizer))
    tool_engine.register(
        ReviewSectionsTool(
            document_index,
            reviewer,
            review_state,
            learned.retriever if learned else None,
        )
    )
    if learned is not None:
        VersionObserverHook(learned.collector).register(hook_engine)
        ReviewFeedbackHook(
            review_state, document_index, learned.collector.store
        ).register(hook_engine)
        learned.agent_hook.register(hook_engine)
        tool_engine.register(ConfirmDocumentVersionTool(learned.collector))

    agent_loop = AgentLoop(
        client=client,
        model=model,
        tool_engine=tool_engine,
        context_engine=context_engine,
        hook_engine=hook_engine,
        settings=settings,
    )
    return Runtime(
        agent_loop=agent_loop,
        hook_engine=hook_engine,
        hook_scope=HookScope(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        ),
    )


def create_session_manager(
    settings: Settings,
    identity: Identity,
    runtime: Runtime,
) -> SessionManager:
    """为 Runtime 装配会话管理器：新开会话，并注册每轮结束后的自动保存；非游客身份另记录对话原文。

    评估等不需要落盘的入口只调用 create_runtime，不调用本函数。

    Args:
        settings: 类型化项目配置，提供数据根目录。
        identity: 启动身份，决定会话文件所在目录。
        runtime: 刚创建、尚无会话状态的 Runtime。

    Returns:
        已绑定当前会话的会话管理器。
    """
    repository = SessionRepository(session_directory(settings, identity))
    manager = SessionManager(runtime, repository)
    manager.register(runtime.hook_engine)
    if not identity.is_guest:
        TurnCollector(
            TranscriptWriter(transcript_directory(settings, identity))
        ).register(runtime.hook_engine)
    return manager


def create_memory_curator(
    settings: Settings,
    identity: Identity,
) -> MemoryCurator | None:
    """为 CLI 的记忆查看、修改与删除创建记忆管理；每次调用都重新读取版本链，看到最新文档。

    Args:
        settings: 类型化项目配置。
        identity: 启动身份。

    Returns:
        当前用户的记忆管理；游客不启用记忆时为 None。

    Raises:
        ValueError: 版本记录文件格式版本不符。
    """
    if identity.is_guest:
        return None
    return MemoryCurator(
        store=_create_memory_store(settings),
        lineage=_create_lineage(settings, identity),
        signals=SignalStore(signal_directory(settings, identity)),
        settings=settings.memory,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )


def create_memory_promotion(settings: Settings) -> MemoryPromotion:
    """为通用候选的生成与人工审核创建记忆晋升（CLI --review-memory）。

    Args:
        settings: 类型化项目配置。

    Returns:
        使用当前模型的记忆晋升。

    Raises:
        KeyError: 模型环境变量缺失。
    """
    client, model = create_model_client(settings=settings)
    return MemoryPromotion(
        store=_create_memory_store(settings),
        promoter=MemoryPromoter(
            client,
            model,
            settings.active_model.parameters,
            PromptRenderer(PROMPT_DIRECTORY),
        ),
        settings=settings.memory,
    )


def create_memory_maintenance(settings: Settings) -> MemoryMaintenance:
    """为全量维护创建记忆维护（CLI --maintain-memory）：全部归属的休眠标记、全量整理与归档。

    Args:
        settings: 类型化项目配置。

    Returns:
        使用当前模型的记忆维护。

    Raises:
        KeyError: 模型环境变量缺失。
    """
    client, model = create_model_client(settings=settings)
    return MemoryMaintenance(
        store=_create_memory_store(settings),
        scorer=MemoryScorer(settings.memory),
        consolidator=MemoryConsolidator(
            client,
            model,
            settings.active_model.parameters,
            PromptRenderer(PROMPT_DIRECTORY),
        ),
        settings=settings.memory,
    )


def _create_learning(
    settings: Settings,
    *,
    identity: Identity,
    document_root: Path,
    client: OpenAI,
    model: str,
    renderer: PromptRenderer,
    review_state: ReviewStateStore,
    document_index: DocumentIndex,
) -> _Learning:
    """创建信号采集与四级记忆组件，共用同一份版本链与记忆存储。

    Args:
        settings: 类型化项目配置。
        identity: 非游客的启动身份。
        document_root: 当前用户的工作区目录。
        client: OpenAI 兼容模型客户端，供记忆子任务使用。
        model: 实际模型名称。
        renderer: 提示词模板渲染器。
        review_state: 当前会话的审阅状态。
        document_index: 文档结构索引。

    Returns:
        学习组件。

    Raises:
        ValueError: 版本记录或成员表文件格式版本不符。
    """
    request_options = settings.active_model.parameters
    collector = _create_feedback_collector(settings, identity, document_root)
    store = _create_memory_store(settings)
    scorer = MemoryScorer(settings.memory)
    retriever = MemoryRetriever(
        store=store,
        scorer=scorer,
        settings=settings.memory,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        review_state=review_state,
        document_index=document_index,
        lineage=collector.lineage,
    )
    return _Learning(
        collector=collector,
        retriever=retriever,
        agent_hook=MemoryAgentHook(
            recorder=MemoryRecorder(
                store=store,
                scorer=scorer,
                signals=collector.store,
                lineage=collector.lineage,
                extractor=MemoryExtractor(client, model, request_options, renderer),
                settings=settings.memory,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
            ),
            promotion=MemoryPromotion(
                store=store,
                promoter=MemoryPromoter(client, model, request_options, renderer),
                settings=settings.memory,
            ),
            maintenance=MemoryMaintenance(
                store=store,
                scorer=scorer,
                consolidator=MemoryConsolidator(
                    client, model, request_options, renderer
                ),
                settings=settings.memory,
            ),
            retriever=retriever,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        ),
    )


def _create_feedback_collector(
    settings: Settings,
    identity: Identity,
    document_root: Path,
) -> FeedbackCollector:
    """按身份创建修改信号采集编排：用户级版本链与信号、课题组级成员表。

    Args:
        settings: 类型化项目配置。
        identity: 非游客的启动身份。
        document_root: 当前用户的工作区目录。

    Returns:
        信号采集编排。

    Raises:
        ValueError: 版本记录或成员表文件格式版本不符。
    """
    return FeedbackCollector(
        document_root=document_root,
        lineage=_create_lineage(settings, identity),
        roster=AuthorRoster(
            tenant_directory(settings, identity),
            lock_timeout_seconds=settings.data.lock_timeout_seconds,
            lock_stale_seconds=settings.data.lock_stale_seconds,
        ),
        store=SignalStore(signal_directory(settings, identity)),
        comparer=VersionComparer(
            assistant_author=settings.documents.revision_author,
            block_match_threshold=settings.signals.block_match_threshold,
        ),
        user_id=identity.user_id,
    )


def _create_lineage(settings: Settings, identity: Identity) -> DocumentLineage:
    """创建用户的版本链。

    Args:
        settings: 类型化项目配置。
        identity: 非游客的启动身份。

    Returns:
        加载了版本记录的版本链。

    Raises:
        ValueError: 版本记录文件格式版本不符。
    """
    return DocumentLineage(
        snapshot_directory(settings, identity),
        match_threshold=settings.signals.match_threshold,
        max_candidates=settings.signals.max_candidates,
    )


def _create_memory_store(settings: Settings) -> MemoryStore:
    """创建四级记忆存储。

    Args:
        settings: 类型化项目配置，提供数据根目录。

    Returns:
        记忆存储。
    """
    return MemoryStore(
        memory_root=memory_root(settings),
        tenants_root=tenants_root(settings),
        global_directory=global_directory(settings),
        lock_timeout_seconds=settings.data.lock_timeout_seconds,
        lock_stale_seconds=settings.data.lock_stale_seconds,
    )
