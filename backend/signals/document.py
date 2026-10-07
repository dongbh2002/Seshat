"""单个 DOCX 版本的多种修订视图与内容指纹，按需解析并缓存，供版本匹配与两版比较共用。"""

from __future__ import annotations

import hashlib
import json
import zlib
from functools import cached_property
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from backend.utils.docx import DocumentBlock, DocxParser, build_heading_paths

REJECT_ALL: frozenset[str] = frozenset()  # 视图参数：拒绝全部修订，即修改前原文。
_SHINGLE_SIZE = 5  # 内容指纹使用的连续字符片段长度。
_SAMPLE_MODULUS = 4  # 只保留哈希值能被该数整除的片段，控制指纹大小。
_TITLE_MAX_CHARS = 60  # 版本标题保留的最大字符数。


class DocxVersion:
    """一个 DOCX 文件在不同修订视图下的内容块，以及据此计算的标题、指纹与内容键。"""

    def __init__(self, path: Path) -> None:
        """初始化版本，解析在首次访问时进行。

        Args:
            path: DOCX 文件路径，通常是版本快照。

        Returns:
            None。
        """
        self.path = path  # DOCX 文件路径。
        self._views: dict[frozenset[str], dict[str, str]] = {}  # 部分接受视图的缓存。

    @cached_property
    def blocks(self) -> list[DocumentBlock]:
        """接受全部修订视图下的内容块，含修订作者与批注。

        Returns:
            按文档顺序排列的内容块。

        Raises:
            ValueError: 文件不是有效 DOCX。
        """
        return self._parse(None)

    def view(self, accepted_authors: frozenset[str] | None) -> dict[str, str]:
        """返回指定修订视图下每个内容块的正文。

        Args:
            accepted_authors: None 接受全部修订；``REJECT_ALL`` 拒绝全部修订；
                其余只接受这些作者的修订、拒绝其他作者的修订。

        Returns:
            块 ID 到正文的映射；同一文档各视图的块 ID 一致。

        Raises:
            ValueError: 文件不是有效 DOCX。
        """
        if accepted_authors is None:
            return {block.block_id: block.text for block in self.blocks}
        if accepted_authors not in self._views:
            self._views[accepted_authors] = {
                block.block_id: block.text for block in self._parse(accepted_authors)
            }
        return self._views[accepted_authors]

    @cached_property
    def revision_authors(self) -> list[str]:
        """文档中全部修订的作者。

        Returns:
            按首次出现顺序去重的作者名。
        """
        return list(
            dict.fromkeys(
                revision["author"]
                for block in self.blocks
                for revision in block.revisions
            )
        )

    @cached_property
    def comment_authors(self) -> list[str]:
        """文档中全部批注的作者。

        Returns:
            按首次出现顺序去重的作者名。
        """
        return list(
            dict.fromkeys(
                comment["author"] for block in self.blocks for comment in block.comments
            )
        )

    @cached_property
    def heading_paths(self) -> dict[str, str]:
        """每个内容块所在的标题路径。

        Returns:
            块 ID 到标题路径的映射。
        """
        return dict(
            zip(
                (block.block_id for block in self.blocks),
                build_heading_paths(self.blocks),
            )
        )

    @cached_property
    def title(self) -> str:
        """便于用户辨认的版本标题：首个非空内容块，论文题目通常位于此处。

        Returns:
            合并空白并截断后的文本；文档为空时为空字符串。
        """
        texts = (" ".join(block.text.split()) for block in self.blocks)
        return next((text for text in texts if text), "")[:_TITLE_MAX_CHARS]

    @cached_property
    def fingerprint(self) -> frozenset[int]:
        """内容指纹：接受与拒绝两种视图下正文的字符片段哈希抽样。

        两种视图都计入，使开修订改得再多也不影响与修改前版本的匹配。

        Returns:
            片段哈希集合。
        """
        hashes: set[int] = set()
        for texts in (self.view(None), self.view(REJECT_ALL)):
            for text in texts.values():
                normalized = "".join(text.split())
                pieces = (
                    normalized[index : index + _SHINGLE_SIZE]
                    for index in range(max(1, len(normalized) - _SHINGLE_SIZE + 1))
                )
                for piece in pieces:
                    if not piece:
                        continue
                    value = zlib.crc32(piece.encode("utf-8"))
                    if value % _SAMPLE_MODULUS == 0:
                        hashes.add(value)
        return frozenset(hashes)

    @cached_property
    def content_key(self) -> str:
        """内容键：两种视图的正文与批注完全相同的版本内容键相同（如仅在 Word 中另存）。

        Returns:
            SHA-256 十六进制字符串。
        """
        content = [
            list(self.view(None).values()),
            list(self.view(REJECT_ALL).values()),
            [
                [comment["author"], comment["text"]]
                for block in self.blocks
                for comment in block.comments
            ],
        ]
        return hashlib.sha256(
            json.dumps(content, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _parse(self, accepted_authors: frozenset[str] | None) -> list[DocumentBlock]:
        """按修订视图解析内容块。

        Args:
            accepted_authors: 取值含义同 ``view``。

        Returns:
            按文档顺序排列的内容块。

        Raises:
            ValueError: 文件不是有效 DOCX。
        """
        if accepted_authors is None:
            mode, authors = "accepted", None
        elif not accepted_authors:
            mode, authors = "rejected", None
        else:
            mode, authors = "accepted", accepted_authors
        try:
            with ZipFile(self.path) as archive:
                return DocxParser(archive).parse_blocks(mode, accepted_authors=authors)
        except BadZipFile as error:
            raise ValueError(f"文件不是有效的 DOCX: {self.path}") from error


def fingerprint_similarity(first: frozenset[int], second: frozenset[int]) -> float:
    """计算两个内容指纹的 Jaccard 相似度。

    Args:
        first: 第一个指纹。
        second: 第二个指纹。

    Returns:
        [0, 1] 之间的相似度；任一指纹为空时为 0。
    """
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)
