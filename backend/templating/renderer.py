"""提示词模板渲染器，统一加载并渲染 backend/prompts 目录下的 Jinja 模板。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from backend.templating.filters import oneline, truncate_middle

PROMPT_DIRECTORY = Path(__file__).resolve().parents[1] / "prompts"  # 提示词模板目录。


class PromptRenderer:
    """按模板文件名渲染提示词，模板缺失变量时直接报错。"""

    def __init__(self, directory: Path) -> None:
        """初始化模板渲染器。

        Args:
            directory: 模板所在目录。

        Returns:
            None。
        """
        self.directory = directory.resolve()  # 模板所在目录。
        self._environment = Environment(  # 所有模板共用的 Jinja 环境。
            undefined=StrictUndefined
        )
        self._environment.filters.update(
            {"oneline": oneline, "truncate_middle": truncate_middle}
        )

    def render(self, name: str, **variables: Any) -> str:
        """读取并渲染指定模板。

        Args:
            name: 模板文件名，相对于模板目录。
            **variables: 传入模板的运行时变量。

        Returns:
            去除首尾空白后的渲染结果。

        Raises:
            FileNotFoundError: 模板文件不存在。
            jinja2.UndefinedError: 模板使用了未提供的变量。
        """
        source = (self.directory / name).read_text(encoding="utf-8")
        return self._environment.from_string(source).render(**variables).strip()
