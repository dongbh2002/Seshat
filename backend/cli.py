"""Seshat 终端多轮对话入口，使用默认配置装配 Runtime 和文档工具。"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.control import Control
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.config import config
from backend.providers import create_model_client
from backend.runtime import (
    AgentLoop,
    ContextEngine,
    HookEngine,
    Runtime,
    ToolEngine,
)
from backend.tools import ReadDocumentTool, WriteDocumentTool

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TENANT_PACKS_ROOT = _PROJECT_ROOT / "backend" / "data_agent" / "tenant_packs"


def _get_default_identity() -> tuple[str, str, str]:
    """从项目配置读取默认租户、用户和会话名称。

    Returns:
        ``(tenant_id, user_id, session_name)`` 三元组。

    Raises:
        ValueError: 默认租户、用户或会话配置缺失或类型无效。
        TypeError: 租户配置不是对象。
    """
    tenant_value = config.get("default_tenant")
    if tenant_value is None:
        raise ValueError("配置缺少 default_tenant")
    tenant_id = str(tenant_value)

    tenants = config.get("tenants")
    if not isinstance(tenants, Mapping):
        raise TypeError("配置中的 tenants 必须是对象")
    tenant_config = tenants.get(tenant_id)
    if not isinstance(tenant_config, Mapping):
        raise TypeError(f"默认租户配置必须是对象: {tenant_id}")

    user_value = tenant_config.get("default_user")
    session_value = tenant_config.get("session_name")
    if user_value is None:
        raise ValueError(f"租户 {tenant_id} 缺少 default_user")
    if session_value is None:
        raise ValueError(f"租户 {tenant_id} 缺少 session_name")
    return tenant_id, str(user_value), str(session_value)


def create_default_runtime() -> Runtime:
    """按默认配置创建注册好 DOCX 读写工具和 Hook 的 Runtime。

    Returns:
        可持续调用 ``run`` 进行进程内多轮对话的 Runtime。

    Raises:
        NotADirectoryError: 默认租户用户目录不存在。
        ValueError: 默认租户、用户或会话配置无效。
        KeyError: 模型环境变量缺失。
    """
    tenant_id, user_id, _ = _get_default_identity()
    document_root = _TENANT_PACKS_ROOT / tenant_id / user_id
    if not document_root.is_dir():
        raise NotADirectoryError(f"默认文档目录不存在: {document_root}")

    hook_engine = HookEngine()
    tool_engine = ToolEngine(hook_engine=hook_engine)
    tool_engine.register(ReadDocumentTool(document_root))
    tool_engine.register(WriteDocumentTool(document_root))

    client, model = create_model_client()
    agent_loop = AgentLoop(
        client=client,
        model=model,
        tool_engine=tool_engine,
        context_engine=ContextEngine(),
        hook_engine=hook_engine,
    )
    return Runtime(
        agent_loop=agent_loop,
        hook_engine=hook_engine,
    )


def _build_pixel_art(
    pixel_rows: tuple[str, ...],
    color: str,
    *,
    cell_width: int = 2,
    depth_color: str | None = None,
) -> Text:
    """把字符像素矩阵渲染为带可选纵深色的实心方块图案。

    Args:
        pixel_rows: 使用 ``#`` 表示主像素、``+`` 表示纵深像素的文本行。
        color: Rich 支持的主像素背景色。
        cell_width: 每个像素占用的终端字符宽度。
        depth_color: 可选的纵深像素背景色，省略时沿用主色。

    Returns:
        可直接交给 Rich 渲染的像素图文本。
    """
    pixel_mark = Text()
    for row_index, row in enumerate(pixel_rows):
        for cell in row:
            if cell == "#":
                style = f"on {color}"
            elif cell == "+":
                style = f"on {depth_color or color}"
            else:
                style = None
            pixel_mark.append(" " * cell_width, style=style)
        if row_index < len(pixel_rows) - 1:
            pixel_mark.append("\n")
    return pixel_mark


def _build_pixel_mark() -> Text:
    """构建带透视中轴与扫描线的 Y2K 蓝色金字塔标志。

    Returns:
        可直接交给 Rich 渲染的金字塔像素图。
    """
    return _build_pixel_art(
        (
            "       #       ",
            "      #+#      ",
            "     # + #     ",
            "    #  +  #    ",
            "   #   +   #   ",
            "  #    +    #  ",
            " #     +     # ",
            "###############",
            " +     +     + ",
            "  +    +    +  ",
            "   +   +   +   ",
            "    +  +  +    ",
            "     + + +     ",
            "      +++      ",
            "       #       ",
        ),
        "#1688f8",
        depth_color="#075ea8",
    )


def _build_pixel_wordmark() -> Text:
    """构建带深蓝右下投影的方正 SESHAT 像素艺术字。

    Returns:
        可直接交给 Rich 渲染的项目名称像素字。
    """
    letters = (
        (" ####", "##   ", "##   ", " ### ", "   ##", "   ##", "#### "),
        ("#####", "##   ", "##   ", "#### ", "##   ", "##   ", "#####"),
        (" ####", "##   ", "##   ", " ### ", "   ##", "   ##", "#### "),
        ("## ##", "## ##", "## ##", "#####", "## ##", "## ##", "## ##"),
        (" ### ", "## ##", "## ##", "#####", "## ##", "## ##", "## ##"),
        ("#####", "  ## ", "  ## ", "  ## ", "  ## ", "  ## ", "  ## "),
    )
    face_rows = tuple(
        " ".join(letter[row_index] for letter in letters) for row_index in range(7)
    )
    width = len(face_rows[0])
    rows: list[str] = []
    for row_index in range(len(face_rows) + 1):
        face_row = face_rows[row_index] if row_index < len(face_rows) else " " * width
        shadow_row = face_rows[row_index - 1] if row_index > 0 else " " * width
        rows.append(
            "".join(
                "#"
                if column_index < width and face_row[column_index] == "#"
                else "+"
                if column_index > 0 and shadow_row[column_index - 1] == "#"
                else " "
                for column_index in range(width + 1)
            )
        )
    return _build_pixel_art(
        tuple(rows),
        "#58a6ff",
        cell_width=1,
        depth_color="#164a73",
    )


def _print_banner(
    console: Console,
    tenant_id: str,
    user_id: str,
    session_name: str,
) -> None:
    """打印带 Seshat 像素标志和当前运行范围的启动横幅。

    Args:
        console: 用于渲染终端样式的 Rich Console。
        tenant_id: 当前默认租户标识。
        user_id: 当前默认用户标识。
        session_name: 当前默认会话名称。

    Returns:
        None。
    """
    heading = _build_pixel_wordmark()
    tagline = Text(
        "Distilling wisdom from every paper",
        style="italic dim",
        justify="center",
    )

    details = Table.grid(padding=(0, 2))
    details.add_column(width=9, style="dim")
    details.add_column(style="#79c0ff")
    details.add_row("MODEL", str(config["current_model"]))
    details.add_row("WORKSPACE", f"{tenant_id} / {user_id}")
    details.add_row("SESSION", session_name)

    status = Text()
    status.append("  ", style="on #1688f8")
    status.append("  READY", style="bold #58a6ff")
    status.append("  default workspace loaded", style="dim")
    status.justify = "center"

    identity = Table.grid(padding=(0, 0))
    identity.add_row(heading)
    identity.add_row(tagline)
    identity.add_row(Text(""))
    identity.add_row(details)
    identity.add_row(status)

    layout = Table.grid(padding=(0, 3))
    layout.add_column(vertical="middle")
    layout.add_column(vertical="middle")
    layout.add_row(_build_pixel_mark(), identity)
    console.print()
    console.print(
        Panel(
            layout,
            box=box.ROUNDED,
            border_style="#1688f8",
            padding=(1, 2),
            expand=False,
            subtitle="[dim]输入 /help 查看命令[/dim]",
            subtitle_align="right",
        )
    )


def _print_help() -> None:
    """打印终端会话支持的本地命令。

    Returns:
        None。
    """
    print("可用命令：")
    print("  /help   查看命令")
    print("  /reset  清空当前进程内的对话历史")
    print("  /exit   退出程序")


def _read_user_input(console: Console) -> str:
    """在固定于输出底部的全宽白线输入器中读取一条消息。

    Args:
        console: 用于按当前终端宽度渲染横线的 Rich Console。

    Returns:
        去除首尾空白后的用户输入。

    Raises:
        EOFError: 标准输入已经关闭。
        KeyboardInterrupt: 用户主动中断输入。
    """
    interactive = console.is_terminal and sys.stdin.isatty()
    if interactive:
        console.print()
        console.rule(style="white", characters="─")
        console.print()
        console.rule(style="white", characters="─")
        console.control(Control.move(y=-2), Control.move_to_column(0))

    try:
        user_input = input("> " if interactive else "").strip()
    except (EOFError, KeyboardInterrupt):
        if interactive:
            _clear_input_composer(console)
        raise

    if interactive:
        _clear_input_composer(console)
    _print_submitted_user_input(console, user_input)
    return user_input


def _clear_input_composer(console: Console) -> None:
    """清除当前三行输入器，并把光标移回其顶部位置。

    Args:
        console: 输入器所在的交互式 Rich Console。

    Returns:
        None。
    """
    console.file.write("\r\x1b[2K")
    for _ in range(2):
        console.file.write("\x1b[1A\r\x1b[2K")
    console.file.flush()


def _print_submitted_user_input(console: Console, user_input: str) -> None:
    """把刚提交的输入行重绘为延伸到终端右边界的浅灰底色。

    Args:
        console: 用于移动光标和自适应终端宽度的 Rich Console。
        user_input: 刚刚通过输入区提交的内容。

    Returns:
        None。
    """
    submitted_line = Text(
        f"> {user_input}",
        style="white on grey23",
        no_wrap=True,
    )
    submitted_line.truncate(console.size.width, overflow="ellipsis", pad=True)
    console.print(submitted_line, soft_wrap=True)


def _print_assistant_message(console: Console, reply: str) -> None:
    """用白色实心点展示 AI 回复，并在末尾打印完成时间。

    Args:
        console: 用于渲染回复样式的 Rich Console。
        reply: Runtime 返回的最终回复文本。

    Returns:
        None。
    """
    assistant_message = Text()
    assistant_message.append("●", style="bold white")
    assistant_message.append("  ")
    assistant_message.append(reply, style="white")
    console.print(assistant_message)

    completed_at = datetime.now().astimezone().strftime("%H:%M:%S")
    console.print(Text(f"* done {completed_at}", style="dim"))


def main() -> int:
    """启动默认 Runtime，并持续处理终端中的多轮用户输入。

    Returns:
        正常退出时返回 0，Runtime 初始化失败时返回 1。
    """
    console = Console(highlight=False)
    try:
        tenant_id, user_id, session_name = _get_default_identity()
        runtime = create_default_runtime()
    except Exception as error:  # noqa: BLE001 - CLI 边界需要展示所有启动错误。
        print(f"Seshat 启动失败：{error}", file=sys.stderr)
        return 1

    _print_banner(console, tenant_id, user_id, session_name)

    while True:
        try:
            user_input = _read_user_input(console)
        except (EOFError, KeyboardInterrupt):
            print("\n会话已结束。")
            return 0

        if not user_input:
            continue
        command = user_input.lower()
        if command in {"/exit", "/quit"}:
            print("会话已结束。")
            return 0
        if command == "/reset":
            runtime.agent_loop.reset()
            print("对话历史已清空。")
            continue
        if command == "/help":
            _print_help()
            continue

        try:
            reply = runtime.run(user_input)
        except Exception as error:  # noqa: BLE001 - 单轮失败不应结束 CLI 会话。
            print(f"运行失败：{error}", file=sys.stderr)
            continue
        _print_assistant_message(console, reply)


if __name__ == "__main__":
    raise SystemExit(main())
