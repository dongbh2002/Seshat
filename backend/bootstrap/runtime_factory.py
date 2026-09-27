"""Runtime 装配：按配置与启动身份创建共享组件、注册工具与 Hook，返回可运行的 Runtime；并为其装配会话管理器。"""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from backend.bootstrap.identity import Identity
from backend.bootstrap.paths import TENANT_PACKS_ROOT, resolve_project_path
from backend.config import Settings
from backend.hooks import HookEngine, HookScope
from backend.llm_tasks import SectionReviewer, SectionSummarizer
from backend.logging import LoggingHook
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
from backend.templating import PROMPT_DIRECTORY, PromptRenderer
from backend.tools import (
    ListFindingsTool,
    ReadDocumentTool,
    ReviewSectionsTool,
    SummarizeSectionsTool,
    UpdateReviewStateTool,
    WriteDocumentTool,
)
from backend.utils.docx import DocumentIndex


def create_default_runtime(settings: Settings, identity: Identity) -> Runtime:
    """为启动身份创建注册好文档工具、章节子任务和 Hook 的 Runtime。

    身份对应的文档工作目录不存在时直接创建。

    Args:
        settings: 入口加载一次并注入各组件的类型化项目配置。
        identity: 启动身份，决定文档工作目录与运行范围。

    Returns:
        可持续调用 ``run`` 进行进程内多轮对话的 Runtime。

    Raises:
        OSError: 文档工作目录创建失败。
        KeyError: 模型环境变量缺失。
    """
    document_root = TENANT_PACKS_ROOT / identity.tenant_id / identity.user_id
    document_root.mkdir(parents=True, exist_ok=True)

    client, model = create_model_client(settings=settings)
    return create_runtime(
        settings,
        identity=identity,
        document_root=document_root,
        client=client,
        model=model,
    )


def create_runtime(
    settings: Settings,
    *,
    identity: Identity,
    document_root: Path,
    client: OpenAI,
    model: str,
) -> Runtime:
    """用给定的身份、文档目录和模型客户端装配 Runtime，供默认入口与评估脚本复用。

    Args:
        settings: 类型化项目配置。
        identity: 运行身份，写入 Hook 运行范围。
        document_root: 工具允许访问的文档目录。
        client: OpenAI 兼容模型客户端。
        model: 实际模型名称。

    Returns:
        注册好文档工具、章节子任务和 Hook 的 Runtime。
    """
    request_options = settings.active_model.parameters
    documents = settings.documents
    renderer = PromptRenderer(PROMPT_DIRECTORY)
    review_state = ReviewStateStore()
    document_index = DocumentIndex(document_root, documents.section_heading_level)

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
        ),
        estimator=estimator,
        physical_limit_tokens=(
            settings.active_model.context_window_tokens - budget.output_reserve_tokens
        ),
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
    tool_engine.register(WriteDocumentTool(document_root))
    tool_engine.register(UpdateReviewStateTool(review_state, document_index))
    tool_engine.register(ListFindingsTool(review_state))
    tool_engine.register(SummarizeSectionsTool(document_index, summarizer))
    tool_engine.register(ReviewSectionsTool(document_index, reviewer, review_state))

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
    """为 Runtime 装配会话管理器：新开会话，并注册每轮结束后的自动保存。

    评估等不需要落盘的入口只调用 create_runtime，不调用本函数。

    Args:
        settings: 类型化项目配置，提供会话根目录。
        identity: 启动身份，决定会话文件所在目录。
        runtime: 刚创建、尚无会话状态的 Runtime。

    Returns:
        已绑定当前会话的会话管理器。
    """
    directory = (
        resolve_project_path(settings.sessions.root)
        / identity.tenant_id
        / identity.user_id
    )
    manager = SessionManager(runtime, SessionRepository(directory))
    manager.register(runtime.hook_engine)
    return manager
