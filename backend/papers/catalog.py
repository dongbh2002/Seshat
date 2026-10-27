"""论文题录接口与 OpenAlex 实现：统一标识、摘要和公开正文入口，便于替换检索源。"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from backend.config import PaperSettings
from backend.papers.http import PaperHttp


@dataclass(frozen=True)
class PaperRecord:
    """一篇论文的来源题录；正文入口不表示正文已经读取。"""

    paper_id: str  # 检索源内的论文标识。
    title: str  # 来源提供的标题。
    authors: list[str]  # 来源提供的作者名单。
    year: int | None  # 发表年份，未知时为 None。
    doi: str | None  # DOI 地址，未提供时为 None。
    url: str  # 论文的题录或落地页地址。
    abstract: str  # 来源摘要，缺失时为空字符串。
    open_access: bool | None  # 检索源是否标记为开放获取；直接链接未知时为 None。
    fulltext_urls: list[str]  # 可尝试的公开 PDF 或 HTML 入口，按顺序排列。

    def metadata(self, settings: PaperSettings) -> dict[str, Any]:
        """生成有界题录，明确摘要与作者列表是否被截断。

        Args:
            settings: 摘要、作者展示上限。

        Returns:
            全部题录字段，加 abstract_truncated、authors_truncated 两个截断标识。
        """
        data = asdict(self)
        data["abstract"] = self.abstract[: settings.abstract_max_chars]
        data["authors"] = self.authors[: settings.max_authors]
        data["abstract_truncated"] = len(self.abstract) > settings.abstract_max_chars
        data["authors_truncated"] = len(self.authors) > settings.max_authors
        return data


class PaperSource(ABC):
    """统一题录检索与按标识查询接口。"""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int,
        cursor: str | None,
        year_from: int | None,
        year_to: int | None,
        open_access_only: bool,
    ) -> tuple[int, list[PaperRecord], str | None]:
        """检索论文。

        Args:
            query: 检索词。
            limit: 本页条数。
            cursor: 上次检索返回的分页标识，None 为第一页。
            year_from: 包含的起始年份，None 为不限制。
            year_to: 包含的结束年份，None 为不限制。
            open_access_only: 是否只检索开放获取论文。

        Returns:
            总条数、题录列表及下一页标识。
        """

    @abstractmethod
    def get(self, reference: str) -> PaperRecord:
        """根据检索源标识或 DOI 查询题录。

        Args:
            reference: 论文标识或 DOI。

        Returns:
            对应论文题录。
        """


class OpenAlexSource(PaperSource):
    """通过 OpenAlex Works API 获取题录和开放获取位置。"""

    def __init__(
        self,
        http: PaperHttp,
        settings: PaperSettings,
        api_key: str | None,
    ) -> None:
        """初始化题录源，不立即联网。

        Args:
            http: 共用网络读取器。
            settings: API 地址与展示配置。
            api_key: 显式配置的密钥，None 为匿名访问。

        Returns:
            None。
        """
        self.http = http  # 网络读取器。
        self.settings = settings  # 检索配置。
        self._headers = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )  # 认证头。

    def search(
        self,
        query: str,
        *,
        limit: int,
        cursor: str | None,
        year_from: int | None,
        year_to: int | None,
        open_access_only: bool,
    ) -> tuple[int, list[PaperRecord], str | None]:
        """执行题录搜索；输入输出含义见 PaperSource.search。"""
        params: dict[str, str | int] = {
            "search": query,
            "per_page": limit,
            "cursor": cursor or "*",
        }
        filters: list[str] = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from:04d}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to:04d}-12-31")
        if open_access_only:
            filters.append("is_oa:true")
        if filters:
            params["filter"] = ",".join(filters)
        data = self._request("works", params)
        results = data.get("results")
        meta = data.get("meta")
        if not isinstance(results, list) or not isinstance(meta, dict):
            raise TypeError("OpenAlex 返回的题录格式无效")
        papers = [_record(item) for item in results if isinstance(item, dict)]
        next_cursor = meta.get("next_cursor") if papers else None
        return int(meta["count"]), papers, next_cursor

    def get(self, reference: str) -> PaperRecord:
        """查询 OpenAlex 标识或 DOI；reference 为标识，返回题录。"""
        normalized = reference.strip()
        if re.fullmatch(r"W\d+", normalized, re.IGNORECASE):
            identifier = normalized.upper()
        elif urlsplit(normalized).hostname == "openalex.org":
            identifier = urlsplit(normalized).path.strip("/")
            if not re.fullmatch(r"W\d+", identifier, re.IGNORECASE):
                raise ValueError("OpenAlex 论文标识无效")
        else:
            doi = re.sub(
                r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
                "",
                normalized,
                flags=re.IGNORECASE,
            )
            if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                raise ValueError("请提供 OpenAlex 标识、DOI 或公开论文地址")
            identifier = f"https://doi.org/{doi}"
        return _record(self._request(f"works/{quote(identifier, safe='')}", None))

    def _request(
        self, path: str, params: dict[str, str | int] | None
    ) -> dict[str, Any]:
        """请求一个题录端点。

        Args:
            path: API 相对路径。
            params: 查询参数，None 表示没有参数。

        Returns:
            解析后的 JSON 对象。

        Raises:
            ValueError: 服务返回无效 JSON。
            TypeError: 服务返回的 JSON 不是对象。
        """
        response = self.http.get(
            f"{self.settings.api_base_url.rstrip('/')}/{path}",
            params=params,
            headers=self._headers,
        )
        data = json.loads(response.content)
        if not isinstance(data, dict):
            raise TypeError("OpenAlex 返回内容不是 JSON 对象")
        return data


def _record(data: dict[str, Any]) -> PaperRecord:
    """把 OpenAlex Work 转为统一题录。

    Args:
        data: Work API 返回对象。

    Returns:
        统一题录，正文入口只取标记为开放获取的位置。
    """
    locations = [data.get("best_oa_location"), *(data.get("locations") or [])]
    urls: list[str] = []
    for field in ("pdf_url", "landing_page_url"):
        for location in locations:
            if isinstance(location, dict) and location.get("is_oa"):
                url = location.get(field)
                if isinstance(url, str) and url not in urls:
                    urls.append(url)
    open_access = data.get("open_access") or {}
    oa_url = open_access.get("oa_url")
    if open_access.get("is_oa") and isinstance(oa_url, str) and oa_url not in urls:
        urls.append(oa_url)
    abstract_index = data.get("abstract_inverted_index") or {}
    words = sorted(
        (position, word)
        for word, positions in abstract_index.items()
        for position in positions
    )
    primary = data.get("primary_location") or {}
    paper_id = str(data["id"])
    return PaperRecord(
        paper_id=paper_id.rsplit("/", 1)[-1],
        title=str(data.get("title") or ""),
        authors=[
            str(item["author"]["display_name"])
            for item in (data.get("authorships") or [])
            if isinstance(item, dict) and (item.get("author") or {}).get("display_name")
        ],
        year=data.get("publication_year"),
        doi=data.get("doi"),
        url=primary.get("landing_page_url") or paper_id,
        abstract=" ".join(word for _, word in words),
        open_access=bool(open_access.get("is_oa")),
        fulltext_urls=urls,
    )
