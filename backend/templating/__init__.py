"""模板渲染包，对外提供提示词模板目录与渲染器；模板文件本身位于 backend/prompts。"""

from backend.templating.filters import inline_diff, oneline, truncate_middle
from backend.templating.renderer import PROMPT_DIRECTORY, PromptRenderer

__all__ = [
    "PROMPT_DIRECTORY",
    "PromptRenderer",
    "inline_diff",
    "oneline",
    "truncate_middle",
]
