"""Seshat 终端多轮对话入口，只负责终端交互与会话命令；组件装配见 backend/bootstrap/。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from rich import box
from rich.console import Console
from rich.control import Control
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backend.bootstrap import (
    PROJECT_ROOT,
    create_default_runtime,
    create_session_manager,
)
from backend.config import load_settings
from backend.logging import configure_logging, log_event
from backend.session import SessionManager, SessionRecord

_LOGGER = logging.getLogger(__name__)


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
    model_name: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> None:
    """打印带 Seshat 像素标志和当前运行范围的启动横幅。

    Args:
        console: 用于渲染终端样式的 Rich Console。
        model_name: 当前启用的模型配置名称。
        tenant_id: 当前默认租户标识。
        user_id: 当前默认用户标识。
        session_id: 启动时新开的会话 ID。

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
    details.add_row("MODEL", model_name)
    details.add_row("WORKSPACE", f"{tenant_id} / {user_id}")
    details.add_row("SESSION", session_id)

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
    print("  /help                查看命令")
    print("  /new                 新开会话（当前会话已自动保存）")
    print("  /sessions            列出已保存的会话")
    print("  /resume <序号或ID>   恢复会话，序号见 /sessions")
    print("  /exit                退出程序")


def _print_sessions(
    console: Console,
    records: list[SessionRecord],
    current_id: str,
) -> None:
    """以表格列出已保存的会话，当前会话在序号后标记 *。

    Args:
        console: 用于渲染表格的 Rich Console。
        records: 按最后保存时间从新到旧排序的会话记录。
        current_id: 当前会话 ID。

    Returns:
        None。
    """
    if not records:
        print("暂无已保存的会话。")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("ID", style="#79c0ff", no_wrap=True)
    table.add_column("更新时间", no_wrap=True)
    table.add_column("轮数", justify="right", no_wrap=True)
    table.add_column("标题", no_wrap=True, overflow="ellipsis")
    for index, record in enumerate(records, start=1):
        table.add_row(
            f"{index}{'*' if record.id == current_id else ''}",
            record.id,
            datetime.fromisoformat(record.updated_at).strftime("%m-%d %H:%M"),
            str(record.turn_count),
            record.title,
        )
    console.print(table)


def _resume_session(sessions: SessionManager, argument: str) -> SessionRecord:
    """按 /sessions 中的序号或会话 ID 恢复会话。

    Args:
        sessions: 会话管理器。
        argument: 用户输入的序号或会话 ID。

    Returns:
        恢复后的当前会话记录。

    Raises:
        ValueError: 未提供参数、序号越界、ID 无效或状态无法恢复。
        FileNotFoundError: 会话不存在。
    """
    if not argument:
        raise ValueError("请指定序号或会话 ID，例如 /resume 1")
    session_id = argument
    if argument.isdigit():
        records = sessions.list_sessions()
        index = int(argument)
        if not 1 <= index <= len(records):
            raise ValueError(f"序号超出范围: {argument}（共 {len(records)} 个会话）")
        session_id = records[index - 1].id
    return sessions.resume(session_id)


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
        settings = load_settings()
        log_path = configure_logging(
            settings.logging,
            base_directory=PROJECT_ROOT,
        )
    except Exception as error:  # noqa: BLE001 - 配置或日志失败时 CLI 无法可靠启动。
        print(f"Seshat 配置或日志初始化失败：{error}", file=sys.stderr)
        return 1

    try:
        identity = settings.default_identity
        runtime = create_default_runtime(settings)
        sessions = create_session_manager(settings, runtime)
    except Exception as error:  # noqa: BLE001 - CLI 边界需要展示所有启动错误。
        log_event(
            _LOGGER,
            "application_start_error",
            {
                "error_type": type(error).__name__,
                "error": str(error),
            },
            level=logging.ERROR,
            error=error,
        )
        print(f"Seshat 启动失败：{error}", file=sys.stderr)
        return 1

    definitions = runtime.agent_loop.tool_engine.get_definitions()
    log_event(
        _LOGGER,
        "application_started",
        {
            "model": runtime.agent_loop.model,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "session_id": sessions.current.id,
            "tools": [
                definition.get("function", {}).get("name") for definition in definitions
            ],
            "log_path": log_path,
        },
    )

    _print_banner(
        console,
        settings.current_model,
        identity.tenant_id,
        identity.user_id,
        sessions.current.id,
    )

    while True:
        try:
            user_input = _read_user_input(console)
        except (EOFError, KeyboardInterrupt):
            log_event(
                _LOGGER,
                "application_stopped",
                {"reason": "input_closed"},
            )
            print("\n会话已结束。")
            return 0

        if not user_input:
            continue
        command, _, argument = user_input.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command in {"/exit", "/quit"}:
            log_event(
                _LOGGER,
                "application_stopped",
                {"reason": "user_exit"},
            )
            print("会话已结束。")
            return 0
        if command == "/new":
            record = sessions.start_new()
            log_event(_LOGGER, "session_started", {"session_id": record.id})
            print(f"已新开会话 {record.id}。")
            continue
        if command == "/sessions":
            _print_sessions(console, sessions.list_sessions(), sessions.current.id)
            continue
        if command == "/resume":
            try:
                record = _resume_session(sessions, argument)
            except (OSError, ValueError) as error:
                print(f"恢复失败：{error}", file=sys.stderr)
                continue
            log_event(_LOGGER, "session_resumed", {"session_id": record.id})
            print(f"已恢复会话 {record.id}（{record.turn_count} 轮）：{record.title}")
            continue
        if command == "/help":
            _print_help()
            continue
        if command.startswith("/"):
            print(f"未知命令：{command}，输入 /help 查看命令。")
            continue

        try:
            reply = runtime.run(user_input)
        except KeyboardInterrupt:
            # TODO: 中断时仍在线程池中执行的工具不会被终止，可能在下一轮开始后才写入结果。
            print(
                "\n已中断本轮：本轮对话不保留，已产生的审阅记录与文档修改会保留。",
                file=sys.stderr,
            )
            continue
        except Exception as error:  # noqa: BLE001 - 单轮失败不应结束 CLI 会话。
            print(f"运行失败：{error}", file=sys.stderr)
            continue
        _print_assistant_message(console, reply)


if __name__ == "__main__":
    raise SystemExit(main())
