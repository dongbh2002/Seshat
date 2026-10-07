"""定义在用户确认文档版本来源后采集修改信号的 Agent 工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from backend.signals import AUTHOR_ROLES, SIGNAL_ROLES, FeedbackCollector
from backend.tools.base import BaseTool, ToolImpact


class ConfirmDocumentVersionTool(BaseTool):
    """记录用户确认的文档版本来源与作者身份，比较版本并沉淀导师等人的修改。"""

    name: ClassVar[str] = "confirm_document_version"  # 模型调用版本确认时使用的名称。
    description: ClassVar[str] = (  # 提供给模型的版本确认说明。
        "审阅状态列出“待确认的文档版本”时，先向用户确认，再调用本工具："
        "是否为所列某个历史版本的新版（是则填 base_revision）、"
        "未开修订的直接修改由谁完成（untracked_role）、未登记作者的身份（author_roles）。"
        "系统随后比较版本，沉淀导师等人的修改。不得自行猜测用户未确认的内容。"
    )
    impact: ClassVar[ToolImpact] = ToolImpact.WRITE  # 写入成员表、版本链与信号。
    timeout_seconds: ClassVar[float] = 120.0  # 解析与比较两个版本的超时时间。
    parameters: ClassVar[dict[str, Any]] = {  # 版本确认工具的输入参数定义。
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "待确认文档在工作目录中的路径。",
            },
            "base_revision": {
                "type": "string",
                "description": (
                    "用户确认的历史版本号（审阅状态中列出的版本号）；"
                    "用户确认不是任何历史版本的新版时省略。"
                ),
            },
            "untracked_role": {
                "type": "string",
                "enum": list(SIGNAL_ROLES),
                "description": (
                    "与历史版本相比、未开修订的直接修改由谁完成：advisor 导师、"
                    "student 学生本人、other 其他人、unknown 用户也不清楚；"
                    "填写 base_revision 时必填。"
                ),
            },
            "author_roles": {
                "type": "array",
                "description": "用户确认的修订或批注作者身份；已登记的作者可省略。",
                "items": {
                    "type": "object",
                    "properties": {
                        "author": {
                            "type": "string",
                            "description": "审阅状态中列出的作者名，原样填写。",
                        },
                        "role": {
                            "type": "string",
                            "enum": list(AUTHOR_ROLES),
                            "description": "advisor 导师、student 学生、other 其他人。",
                        },
                    },
                    "required": ["author", "role"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, collector: FeedbackCollector) -> None:
        """初始化版本确认工具。

        Args:
            collector: 信号采集编排。

        Returns:
            None。
        """
        self.collector = collector  # 信号采集编排。

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """校验参数后交给采集编排确认版本并采集信号。

        Args:
            **arguments: path、可选的 base_revision、untracked_role 与 author_roles。

        Returns:
            ``FeedbackCollector.confirm`` 的结果：确认的版本、所属论文、基线与新写入的信号计数。

        Raises:
            ValueError: 参数无效，或确认信息不完整。
            FileNotFoundError: 文档不存在。
        """
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path 必须是非空字符串")
        base_revision = arguments.get("base_revision")
        if base_revision is not None and not isinstance(base_revision, str):
            raise ValueError("base_revision 必须是字符串")
        untracked_role = arguments.get("untracked_role")
        if untracked_role is not None and not isinstance(untracked_role, str):
            raise ValueError("untracked_role 必须是字符串")
        return self.collector.confirm(
            path,
            base_revision=base_revision.strip() if base_revision else None,
            untracked_role=untracked_role,
            author_roles=self._parse_author_roles(arguments.get("author_roles", [])),
        )

    @staticmethod
    def _parse_author_roles(value: Any) -> dict[str, str]:
        """把作者身份数组转为作者名到角色的映射。

        Args:
            value: author_roles 参数。

        Returns:
            作者名到角色的映射。

        Raises:
            ValueError: 结构无效或作者名为空。
        """
        if not isinstance(value, list):
            raise ValueError("author_roles 必须是数组")
        roles: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("author_roles 的每一项必须是对象")
            author = item.get("author")
            role = item.get("role")
            if not isinstance(author, str) or not author.strip():
                raise ValueError("author 必须是非空字符串")
            if not isinstance(role, str):
                raise ValueError("role 必须是字符串")
            roles[author] = role
        return roles
