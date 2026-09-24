"""CLI 独立测试，验证默认 Runtime 装配和终端多轮命令处理。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from backend import cli


def test_create_default_runtime_keeps_multi_turn_messages_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 CLI 创建的 Runtime 注册文档工具并保留两轮消息。

    Args:
        monkeypatch: pytest 提供的运行时替换工具。

    Returns:
        None；Runtime 装配或多轮消息不符合预期时由断言报告失败。
    """
    assistant_message = MagicMock()
    assistant_message.tool_calls = None
    assistant_message.content = "模拟回复"
    assistant_message.model_dump.return_value = {"content": "模拟回复"}

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=assistant_message)]
    )
    monkeypatch.setattr(
        cli,
        "create_model_client",
        MagicMock(return_value=(fake_client, "fake-model")),
    )

    runtime = cli.create_default_runtime()
    first_reply = runtime.run("第一轮问题")
    second_reply = runtime.run("第二轮问题")

    assert first_reply == "模拟回复"
    assert second_reply == "模拟回复"
    assert fake_client.chat.completions.create.call_count == 2
    second_messages = fake_client.chat.completions.create.call_args_list[1].kwargs[
        "messages"
    ]
    assert [message["role"] for message in second_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "Seshat" in second_messages[0]["content"]
    assert second_messages[1]["content"] == "第一轮问题"
    assert second_messages[3]["content"] == "第二轮问题"

    assert runtime.agent_loop.tool_engine is not None
    definitions = runtime.agent_loop.tool_engine.get_definitions()
    assert [definition["function"]["name"] for definition in definitions] == [
        "read_document",
        "write_document",
    ]
    assert runtime.hook_scope.tenant_id == "233"
    assert runtime.hook_scope.user_id == "dbh"
    assert runtime.hook_scope.session_name == "pe"


def test_cli_main_handles_multi_turn_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证终端循环处理多轮输入、重置、帮助和退出命令。

    Args:
        monkeypatch: pytest 提供的运行时替换工具。
        capsys: pytest 提供的标准输出和错误输出捕获器。

    Returns:
        None；命令调用或输出不符合预期时由断言报告失败。
    """
    fake_runtime = MagicMock()
    fake_runtime.run.side_effect = ["第一轮回复", "第二轮回复"]
    monkeypatch.setattr(
        cli,
        "create_default_runtime",
        MagicMock(return_value=fake_runtime),
    )

    with patch(
        "builtins.input",
        side_effect=["第一轮问题", "/reset", "第二轮问题", "/help", "/exit"],
    ):
        exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert fake_runtime.run.call_args_list == [call("第一轮问题"), call("第二轮问题")]
    fake_runtime.agent_loop.reset.assert_called_once_with()
    assert "第一轮回复" in captured.out
    assert "第二轮回复" in captured.out
    assert "对话历史已清空" in captured.out
    assert "可用命令" in captured.out
    assert "会话已结束" in captured.out


def test_cli_main_reports_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """验证 Runtime 初始化失败时 CLI 返回非零状态并输出错误。

    Args:
        monkeypatch: pytest 提供的运行时替换工具。
        capsys: pytest 提供的标准输出和错误输出捕获器。

    Returns:
        None；退出状态或错误输出不符合预期时由断言报告失败。
    """
    monkeypatch.setattr(
        cli,
        "create_default_runtime",
        MagicMock(side_effect=RuntimeError("模拟启动失败")),
    )

    exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Seshat 启动失败" in captured.err
    assert "模拟启动失败" in captured.err
