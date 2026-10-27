"""论文正文阅读：解析公开入口，缓存提取结果，并用内容版本与游标提供无遗漏续读。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pypdf.errors import PdfReadError

from backend.config import PaperSettings
from backend.papers.catalog import PaperRecord, PaperSource
from backend.papers.http import PaperHttp
from backend.papers.text import extract_paper

_CACHE_SCHEMA = 1  # 提取缓存格式版本。


class PaperReader:
    """按启动身份缓存正文；输出范围与源文件完整性分别标记。"""

    def __init__(
        self,
        source: PaperSource,
        http: PaperHttp,
        settings: PaperSettings,
        directory: Path,
    ) -> None:
        """初始化论文阅读器，不创建目录或立即联网。

        Args:
            source: 题录查询接口。
            http: 正文下载器。
            settings: 读取与缓存配置。
            directory: 当前租户、用户的缓存目录。

        Returns:
            None。
        """
        self.source = source  # 查询题录及公开入口。
        self.http = http  # 下载公开资源。
        self.settings = settings  # 正文展示及缓存配置。
        self.directory = directory  # 按身份隔离的提取缓存。

    def read(
        self, reference: str, *, cursor: str | None, max_chars: int
    ) -> dict[str, Any]:
        """分段读取已检索论文、DOI 或用户提供的公开链接。

        Args:
            reference: OpenAlex 标识、DOI、论文落地页或 PDF/HTML 地址。
            cursor: 上次返回的 next_cursor；None 从头读取。
            max_chars: 本次提取正文的字符上限。

        Returns:
            paper：有界题录；source_url：实际来源；revision：资源哈希；
            format：pdf/html；read_status：全文、部分文本、网页文本、仅摘要或不可用；
            blocks：id/text/page/section/start_offset/end_offset；
            next_cursor：续读标识；eof：是否到达已提取内容末尾；
            limitations：解析局限；errors：各失败入口的原因。

        Raises:
            ValueError: 游标非法或来源内容已更新。
        """
        reference = reference.strip()
        cache = self._cache_path(reference)
        document = self._load_cache(cache, reference)
        if document is None:
            document = self._fetch(reference)
            if document["blocks"]:
                self._save_cache(cache, document)
        block_index, offset = 0, 0
        if cursor is not None:
            match = re.fullmatch(r"([0-9a-f]{64}):(\d+):(\d+)", cursor)
            if not match:
                raise ValueError("cursor 无效，请使用 read_paper 返回的 next_cursor")
            if match[1] != document["revision"]:
                raise ValueError("论文来源内容已更新，请从头读取，不能混用旧游标")
            block_index, offset = int(match[2]), int(match[3])
            if block_index >= len(document["blocks"]) or offset >= len(
                document["blocks"][block_index]["text"]
            ):
                raise ValueError("cursor 超出已提取的正文范围")
        selected: list[dict[str, Any]] = []
        remaining = max_chars
        while block_index < len(document["blocks"]) and remaining > 0:
            block = document["blocks"][block_index]
            end = min(len(block["text"]), offset + remaining)
            selected.append(
                {
                    **block,
                    "text": block["text"][offset:end],
                    "start_offset": offset,
                    "end_offset": end,
                }
            )
            remaining -= end - offset
            if end < len(block["text"]):
                offset = end
                break
            block_index += 1
            offset = 0
        eof = block_index >= len(document["blocks"])
        return {
            "paper": PaperRecord(**document["paper"]).metadata(self.settings),
            "source_url": document["source_url"],
            "revision": document["revision"],
            "format": document["format"],
            "read_status": document["read_status"],
            "blocks": selected,
            "next_cursor": None
            if eof
            else f"{document['revision']}:{block_index}:{offset}",
            "eof": eof,
            "limitations": document["limitations"],
            "errors": document["errors"],
        }

    def _fetch(self, reference: str) -> dict[str, Any]:
        """获取并提取正文。

        Args:
            reference: 论文标识、DOI 或公开地址。

        Returns:
            可缓存的提取结果；失败时保留题录并标记 unavailable。
        """
        parsed = urlsplit(reference)
        if parsed.scheme in {"http", "https"} and parsed.hostname not in {
            "openalex.org",
            "doi.org",
            "dx.doi.org",
        }:
            url = reference
            if parsed.hostname in {
                "arxiv.org",
                "www.arxiv.org",
                "export.arxiv.org",
            } and parsed.path.startswith("/abs/"):
                url = urlunsplit(
                    (
                        "https",
                        "arxiv.org",
                        parsed.path.replace("/abs/", "/pdf/", 1),
                        "",
                        "",
                    )
                )
            paper = PaperRecord(
                reference, "", [], None, None, reference, "", None, [url]
            )
        else:
            paper = self.source.get(reference)
        result: dict[str, Any] = {
            "schema": _CACHE_SCHEMA,
            "reference": reference,
            "fetched_at": time.time(),
            "paper": asdict(paper),
            "source_url": None,
            "revision": None,
            "format": None,
            "read_status": "unavailable",
            "blocks": [],
            "limitations": [],
            "errors": [],
        }
        urls = deque(paper.fulltext_urls)
        visited: set[str] = set()
        fallback: dict[str, Any] | None = None
        while urls and len(visited) < self.settings.max_fulltext_sources:
            url = urls.popleft()
            if url in visited:
                continue
            visited.add(url)
            try:
                response = self.http.get(url)
                text = extract_paper(response, self.settings)
                if not paper.title and text.title:
                    paper = replace(paper, title=text.title)
                document = {
                    **result,
                    "paper": asdict(paper),
                    "source_url": response.url,
                    "revision": hashlib.sha256(response.content).hexdigest(),
                    "format": text.format,
                    "read_status": text.read_status,
                    "blocks": [asdict(block) for block in text.blocks],
                    "limitations": text.limitations,
                }
                if text.read_status in {"full_text", "partial_text"}:
                    return document
                if fallback is None or text.read_status == "html_text":
                    fallback = document
                urls.extendleft(reversed(text.pdf_urls))
                if text.read_status == "html_text" and not text.pdf_urls:
                    return document
            except (OSError, ValueError, PdfReadError) as error:
                result["errors"].append({"url": url, "error": str(error)})
        if fallback is not None:
            return {**fallback, "errors": result["errors"]}
        result["limitations"].append(
            "未获取到可读取的公开正文；题录摘要不代表已读全文。"
        )
        return result

    def _cache_path(self, reference: str) -> Path:
        """按 reference 计算固定缓存路径；输入为论文标识，输出为用户目录内的 JSON 路径。"""
        return self.directory / f"{hashlib.sha256(reference.encode()).hexdigest()}.json"

    def _load_cache(self, path: Path, reference: str) -> dict[str, Any] | None:
        """读取未过期缓存。

        Args:
            path: 当前用户缓存文件。
            reference: 预期论文标识。

        Returns:
            有效缓存；缺失、过期或格式不符时为 None，由调用方重新下载。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["schema"] != _CACHE_SCHEMA or data["reference"] != reference:
                return None
            if time.time() - data["fetched_at"] > self.settings.cache_ttl_seconds:
                return None
            PaperRecord(**data["paper"])
            if not isinstance(data["blocks"], list) or not data["revision"]:
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cache(self, path: Path, document: dict[str, Any]) -> None:
        """原子保存提取结果。

        Args:
            path: 当前用户缓存文件。
            document: 题录、来源、正文块及读取状态。

        Returns:
            None。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
