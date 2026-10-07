"""Seshat 终端多轮对话入口，只负责启动参数、终端交互与会话命令；组件装配见 backend/bootstrap/。"""

from __future__ import annotations

import argparse
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
    Identity,
    create_default_runtime,
    create_memory_curator,
    create_memory_maintenance,
    create_memory_promotion,
    create_session_manager,
    resolve_identity,
)
from backend.config import Settings, load_settings
from backend.logging import configure_logging, log_event
from backend.memory import MemoryCurator, VisibleMemory
from backend.session import SessionManager, SessionRecord
from backend.signals import Signal
from backend.templating import inline_diff, truncate_middle

_LOGGER = logging.getLogger(__name__)
_MEMORY_KINDS = {"rule": "规则", "fact": "事实"}  # 记忆类型的显示名。
_MEMORY_STATUSES = {  # 记忆状态的显示名。
    "active": "生效",
    "dormant": "休眠",
    "candidate": "候选",
    "promoted": "已晋升",
    "retired": "已下线",
    "rejected": "已拒绝",
}
_STATS_COLUMNS = (  # /memory stats 的状态列：列名与计入的状态。
    ("生效", ("active",)),
    ("休眠", ("dormant",)),
    ("候选", ("candidate",)),
    ("已拒绝", ("rejected",)),
    ("待归档", ("promoted", "retired")),
)
_ARCHIVE_LIST_LIMIT = 30  # /memory archived 显示的最近归档条数。
_SIGNAL_SOURCES = {  # 信号来源的显示名。
    "finding_outcome": "问题结局",
    "decision": "对话决定",
    "tracked_revision": "修订",
    "comment": "批注",
    "untracked_edit": "直接修改",
    "assistant_revision": "Seshat 修订结局",
}


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
        tenant_id: 当前租户标识。
        user_id: 当前用户标识。
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
    status.append("  workspace loaded", style="dim")
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
    print("  /memory              查看可见的长期记忆（文档、用户、课题组、通用）")
    print("  /memory show <ID>    查看一条记忆的依据与修改历史")
    print("  /memory edit <ID> <内容>  修改一条记忆（通用级除外）")
    print("  /memory stats        查看各级记忆的条目数、归档数与文件大小")
    print("  /memory archived     查看最近归档的记忆")
    print("  /memory restore <ID> 把归档的记忆恢复为生效")
    print("  /forget <记忆ID>     删除一条记忆（通用级除外）")
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


def _print_memory(console: Console, entries: list[VisibleMemory]) -> None:
    """以表格列出用户可见的长期记忆。

    Args:
        console: 用于渲染表格的 Rich Console。
        entries: 可见记忆。

    Returns:
        None。
    """
    if not entries:
        print("暂无记忆。")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("ID", style="#79c0ff", no_wrap=True)
    table.add_column("范围", no_wrap=True, overflow="ellipsis", max_width=24)
    table.add_column("类型", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("支持", justify="right", no_wrap=True)
    table.add_column("内容")
    for entry in entries:
        table.add_row(
            entry.item.id,
            entry.label,
            _MEMORY_KINDS[entry.item.kind],
            _MEMORY_STATUSES[entry.item.status],
            str(entry.item.support),
            entry.item.content,
        )
    console.print(table)


def _print_memory_details(
    entry: VisibleMemory,
    evidence: list[Signal],
    counter_evidence: list[Signal],
) -> None:
    """打印一条记忆的详情：基本信息、本人的支持与反例依据、修改历史。

    Args:
        entry: 可见记忆。
        evidence: 支持依据中属于本人的信号。
        counter_evidence: 反例依据中属于本人的信号。

    Returns:
        None。
    """
    item = entry.item
    print(
        f"{item.id}｜{entry.label}｜{_MEMORY_KINDS[item.kind]}｜{_MEMORY_STATUSES[item.status]}"
    )
    print(f"  内容：{item.content}")
    print(
        f"  支持 {item.support}、反例 {item.against}；"
        f"最近支持 {item.last_supported_at[:10]}；创建 {item.created_at[:10]}"
    )
    for title, total, signals in (
        ("支持依据", len(item.evidence), evidence),
        ("反例依据", len(item.counter_evidence), counter_evidence),
    ):
        print(f"  {title} {total} 条，其中本人的 {len(signals)} 条：")
        for signal in signals:
            change = (
                inline_diff(signal.before, signal.after, 20)
                if signal.after or not signal.comment
                else signal.comment
            )
            print(
                f"    - [{_SIGNAL_SOURCES[signal.source]}] "
                f"{truncate_middle(change, 120)}"
            )
    for record in item.history:
        print(
            f"  历史 {record['changed_at'][:16]}：{record['reason']}"
            f"（原内容：{record['content']}）"
        )


def _handle_memory_command(
    console: Console,
    settings: Settings,
    identity: Identity,
    command: str,
    argument: str,
) -> None:
    """处理 /memory（列出、show、edit）与 /forget；每次重新读取记忆与版本链，看到最新状态。

    Args:
        console: 用于渲染表格的 Rich Console。
        settings: 类型化项目配置。
        identity: 当前身份。
        command: ``/memory`` 或 ``/forget``。
        argument: 命令参数：``show <ID>``、``edit <ID> <内容>`` 或 /forget 的 ID。

    Returns:
        None。
    """
    curator = create_memory_curator(settings, identity)
    if curator is None:
        print("游客身份不记录长期记忆，请以 --tenant/--user 启动。")
        return
    action, _, rest = argument.partition(" ")
    if command == "/memory" and not argument:
        _print_memory(console, curator.visible())
        return
    try:
        if command == "/forget":
            if not argument:
                print("请指定记忆 ID，ID 见 /memory。")
                return
            item = curator.forget(argument)
            log_event(_LOGGER, "memory_forgotten", {"memory_id": item.id})
            print(f"已删除记忆 {item.id}：{item.content}")
        elif action == "show" and rest.strip():
            _print_memory_details(*curator.details(rest.strip()))
        elif action == "edit" and len(rest.split(maxsplit=1)) == 2:
            item_id, content = rest.split(maxsplit=1)
            item = curator.edit(item_id, content)
            log_event(_LOGGER, "memory_edited", {"memory_id": item.id})
            print(f"已修改记忆 {item.id}：{item.content}")
        elif action == "stats" and not rest.strip():
            _print_memory_stats(console, curator)
        elif action == "archived" and not rest.strip():
            _print_archived(console, curator)
        elif action == "restore" and rest.strip():
            item = curator.restore(rest.strip())
            log_event(_LOGGER, "memory_restored", {"memory_id": item.id})
            print(f"已恢复记忆 {item.id}：{item.content}")
        else:
            print(
                "用法：/memory、/memory show <ID>、/memory edit <ID> <新内容>、"
                "/memory stats、/memory archived、/memory restore <ID>"
            )
    except ValueError as error:
        print(f"操作失败：{error}", file=sys.stderr)


def _print_memory_stats(console: Console, curator: MemoryCurator) -> None:
    """以表格打印各级记忆的条目数、归档数与文件大小。

    Args:
        console: 用于渲染表格的 Rich Console。
        curator: 当前用户的记忆管理。

    Returns:
        None。
    """
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("级别", no_wrap=True)
    for title, _ in _STATS_COLUMNS:
        table.add_column(title, justify="right", no_wrap=True)
    table.add_column("已归档", justify="right", no_wrap=True)
    table.add_column("大小", justify="right", no_wrap=True)
    for row in curator.stats():
        table.add_row(
            row.label,
            *(
                str(sum(row.counts.get(status, 0) for status in statuses))
                for _, statuses in _STATS_COLUMNS
            ),
            str(row.archived),
            f"{row.size_bytes / 1024:.1f}K",
        )
    console.print(table)
    print(f"修改信号：{curator.signal_size_bytes() / 1024:.1f} KB")


def _print_archived(console: Console, curator: MemoryCurator) -> None:
    """以表格打印最近归档的记忆。

    Args:
        console: 用于渲染表格的 Rich Console。
        curator: 当前用户的记忆管理。

    Returns:
        None。
    """
    records = curator.archived()
    if not records:
        print("暂无归档的记忆。")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("ID", style="#79c0ff", no_wrap=True)
    table.add_column("范围", no_wrap=True, overflow="ellipsis", max_width=24)
    table.add_column("归档时间", no_wrap=True)
    table.add_column("原因", no_wrap=True)
    table.add_column("内容")
    for label, record in records[:_ARCHIVE_LIST_LIMIT]:
        table.add_row(
            record.item.id,
            label,
            record.archived_at[:16].replace("T", " "),
            record.reason,
            record.item.content,
        )
    console.print(table)
    if len(records) > _ARCHIVE_LIST_LIMIT:
        print(f"另有 {len(records) - _ARCHIVE_LIST_LIMIT} 条较早的归档未显示。")


def _maintain_all_memory(settings: Settings) -> int:
    """全量维护全部记忆：休眠标记、全量整理与归档（管理员手动运行）。

    Args:
        settings: 类型化项目配置。

    Returns:
        正常结束返回 0，组件初始化失败返回 1。
    """
    try:
        maintenance = create_memory_maintenance(settings)
    except Exception as error:  # noqa: BLE001 - CLI 边界需要展示所有启动错误。
        print(f"维护初始化失败：{error}", file=sys.stderr)
        return 1
    print("正在全量维护记忆（休眠标记、整理、归档）……")
    counts = maintenance.run_full()
    log_event(_LOGGER, "memory_maintained", counts)
    print(
        f"完成：转为休眠 {counts['dormant']} 条，整理操作 {counts['consolidated']} 次，"
        f"移入归档 {counts['archived']} 条。"
    )
    return 0


def _review_global_memory(settings: Settings) -> int:
    """归纳通用候选并逐条人工审核：批准后对所有用户生效，拒绝后不再提出。

    Args:
        settings: 类型化项目配置。

    Returns:
        正常结束返回 0，组件初始化失败返回 1。
    """
    try:
        promotion = create_memory_promotion(settings)
    except Exception as error:  # noqa: BLE001 - CLI 边界需要展示所有启动错误。
        print(f"审核初始化失败：{error}", file=sys.stderr)
        return 1
    print("正在从各课题组的记忆中归纳通用候选……")
    try:
        promotion.refresh_global_candidates()
    except Exception as error:  # noqa: BLE001 - 归纳失败时仍可审核已有候选。
        print(f"归纳失败，仅审核已有候选：{error}", file=sys.stderr)
    candidates = promotion.global_candidates()
    if not candidates:
        print("暂无待审核的通用候选。")
        return 0
    for index, item in enumerate(candidates, start=1):
        print(f"\n[{index}/{len(candidates)}] {item.id}（支持 {item.support}）")
        print(f"  候选：{item.content}")
        for tenant_id, member in promotion.candidate_evidence(item):
            print(f"  依据（课题组 {tenant_id}）：{member.content}")
        answer = ""
        while answer not in {"y", "n", "s", "q"}:
            try:
                answer = input("批准 y / 拒绝 n / 跳过 s / 退出 q：").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "q"
        if answer == "q":
            break
        if answer == "y":
            promotion.approve(item.id)
            log_event(_LOGGER, "global_memory_approved", {"memory_id": item.id})
            print("已批准，对所有用户生效。")
        elif answer == "n":
            promotion.reject(item.id)
            log_event(_LOGGER, "global_memory_rejected", {"memory_id": item.id})
            print("已拒绝。")
    return 0


def _end_sessions(sessions: SessionManager, identity: Identity) -> None:
    """退出前结束当前会话，触发记忆提炼等收尾处理。

    Args:
        sessions: 会话管理器。
        identity: 当前身份；游客没有记忆，不提示等待。

    Returns:
        None。
    """
    if not identity.is_guest:
        print("正在整理本次会话的记忆……")
    try:
        sessions.close()
    except KeyboardInterrupt:
        print("已跳过记忆整理，未处理的修改会在下次会话结束时整理。", file=sys.stderr)


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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析启动参数。

    Args:
        argv: 不含程序名的参数列表；None 时读取 sys.argv。

    Returns:
        含 tenant、user（未提供为 None）、review_memory 与 maintain_memory 的参数对象。
    """
    # TODO: 接入注册登录后，身份改由登录结果提供。
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Seshat 终端对话。租户与用户须同时提供；都不提供时以游客身份启动。",
    )
    parser.add_argument("--tenant", help="租户名，通常对应课题组")
    parser.add_argument("--user", help="用户名")
    parser.add_argument(
        "--review-memory",
        action="store_true",
        help="管理员模式：归纳并逐条审核通用级记忆候选，不进入对话",
    )
    parser.add_argument(
        "--maintain-memory",
        action="store_true",
        help="管理员模式：全量维护全部记忆（休眠标记、整理、归档），不进入对话",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """按启动参数解析身份并启动 Runtime，持续处理终端中的多轮用户输入。

    Args:
        argv: 不含程序名的启动参数；None 时读取 sys.argv。

    Returns:
        正常退出时返回 0，身份或 Runtime 初始化失败时返回 1。
    """
    args = _parse_args(argv)
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
    if args.review_memory:
        return _review_global_memory(settings)
    if args.maintain_memory:
        return _maintain_all_memory(settings)

    try:
        identity = resolve_identity(settings, args.tenant, args.user)
        runtime = create_default_runtime(settings, identity)
        sessions = create_session_manager(settings, identity, runtime)
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
            print()
            _end_sessions(sessions, identity)
            log_event(
                _LOGGER,
                "application_stopped",
                {"reason": "input_closed"},
            )
            print("会话已结束。")
            return 0

        if not user_input:
            continue
        command, _, argument = user_input.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command in {"/exit", "/quit"}:
            _end_sessions(sessions, identity)
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
        if command in {"/memory", "/forget"}:
            _handle_memory_command(console, settings, identity, command, argument)
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
