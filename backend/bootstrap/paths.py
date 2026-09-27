"""装配使用的路径常量；配置中的相对路径以项目根目录为基准。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根目录。
TENANT_PACKS_ROOT = (  # 各租户、用户文档工作目录的根目录。
    PROJECT_ROOT / "backend" / "data_agent" / "tenant_packs"
)
