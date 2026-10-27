"""公开论文正文解析：PDF 保留物理页码，HTML 保留段落与标题；不调用模型生成原文。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin

from lxml import etree, html
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from backend.config import PaperSettings
from backend.papers.http import PaperResponse

_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "li",
    "table",
}  # HTML 正文块。


@dataclass(frozen=True)
class PaperBlock:
    """一个可定位的提取文本块。"""

    id: str  # 当前内容版本内稳定的块编号。
    text: str  # 提取的原文。
    page: int | None  # PDF 物理页码，从 1 开始；HTML 为 None。
    section: str  # HTML 中最近的标题；PDF 无法确定时为空。


@dataclass(frozen=True)
class PaperText:
    """解析结果，包括可读范围与进一步获取正文的入口。"""

    title: str  # 文件元数据或网页提供的标题，缺失时为空。
    format: str  # pdf 或 html。
    read_status: str  # full_text、partial_text、html_text 或 abstract_only。
    blocks: list[PaperBlock]  # 可读取的正文块。
    limitations: list[str]  # 截断、扫描页与解析局限。
    pdf_urls: list[str]  # HTML 声明的 PDF 入口，供阅读器继续尝试。


def extract_paper(response: PaperResponse, settings: PaperSettings) -> PaperText:
    """根据内容识别并解析 PDF 或 HTML。

    Args:
        response: 下载完成的资源。
        settings: PDF 解析页数与内容流大小限制。

    Returns:
        保留定位信息的正文与读取状态。

    Raises:
        ValueError: 内容类型不支持或无法提取文字。
    """
    if response.content.lstrip().startswith(b"%PDF-"):
        return _pdf_text(response.content, settings)
    if "html" in response.content_type or response.content.lstrip().startswith(
        (b"<", b"<!")
    ):
        return _html_text(response.content, response.url)
    raise ValueError("论文入口未返回 PDF 或 HTML")


def _pdf_text(content: bytes, settings: PaperSettings) -> PaperText:
    """提取 PDF 的文本层；输入为文件内容及解析限制，输出含页码的正文。"""
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("论文 PDF 已加密，无法提取公开正文")
    blocks: list[PaperBlock] = []
    limitations = ["PDF 文本提取可能遗漏图像、公式或表格结构；页码为文件物理页码。"]
    incomplete = len(reader.pages) > settings.max_pdf_pages
    if incomplete:
        limitations.append(f"PDF 超过解析上限，仅提取前 {settings.max_pdf_pages} 页。")
    for index in range(min(len(reader.pages), settings.max_pdf_pages)):
        try:
            page = reader.pages[index]
            stream = page.get_contents()
            if (
                stream is not None
                and len(stream.get_data()) > settings.max_pdf_stream_bytes
            ):
                incomplete = True
                limitations.append(f"第 {index + 1} 页内容流超过解析上限，已跳过。")
                continue
            text = (page.extract_text() or "").strip()
        except (PdfReadError, ValueError) as error:
            incomplete = True
            limitations.append(
                f"第 {index + 1} 页解析失败，已跳过：{type(error).__name__}。"
            )
            continue
        if not text:
            incomplete = True
            limitations.append(f"第 {index + 1} 页没有可提取的文本，可能需要 OCR。")
            continue
        blocks.append(PaperBlock(f"P{index + 1}", text, index + 1, ""))
    if not blocks:
        # TODO: 接入可配置 OCR 后补充扫描论文；当前明确报告无可读文本。
        raise ValueError("PDF 没有可提取的文本层，可能是扫描件")
    metadata = reader.metadata or {}
    return PaperText(
        str(metadata.get("/Title") or ""),
        "pdf",
        "partial_text" if incomplete else "full_text",
        blocks,
        limitations,
        [],
    )


def _html_text(content: bytes, url: str) -> PaperText:
    """提取 HTML 中的正文区域；输入为网页字节及地址，输出段落和 PDF 入口。"""
    try:
        document = html.fromstring(content, base_url=url)
    except (etree.ParserError, etree.XMLSyntaxError) as error:
        raise ValueError("论文网页无法解析为 HTML") from error
    titles = document.xpath("//meta[@name='citation_title']/@content | //title/text()")
    title = " ".join(str(titles[0]).split()) if titles else ""
    pdf_urls = list(
        dict.fromkeys(
            urljoin(url, str(link))
            for link in document.xpath(
                "//meta[@name='citation_pdf_url']/@content | //link[@type='application/pdf']/@href"
            )
        )
    )
    for element in document.xpath(
        "//script|//style|//nav|//footer|//header|//aside|//form|//noscript"
    ):
        element.drop_tree()
    containers = document.xpath(
        "//article | //main | //*[@role='main'] | "
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' ltx_document ')]"
    )
    root = containers[0] if containers else document
    section = ""
    blocks: list[PaperBlock] = []
    for element in root.iter():
        if element.tag not in _BLOCK_TAGS:
            continue
        if any(parent.tag in _BLOCK_TAGS for parent in element.iterancestors()):
            continue
        if element.tag == "table":
            text = "\n".join(
                " | ".join(
                    " ".join(cell.text_content().split())
                    for cell in row.xpath("./th|./td")
                )
                for row in element.xpath(".//tr")
            ).strip()
        else:
            text = " ".join(element.text_content().split())
        if not text:
            continue
        if re.fullmatch(r"h[1-6]", element.tag):
            section = text
        blocks.append(PaperBlock(f"H{len(blocks) + 1}", text, None, section))
    abstract_nodes = document.xpath(
        "//*[@id='abstract' or contains(concat(' ', normalize-space(@class), ' '), ' abstract ')]"
    )
    has_body = any(
        re.search(
            r"\b(introduction|methods?|results?|discussion|conclusions?)\b|引言|方法|结果|讨论|结论",
            block.section,
            flags=re.IGNORECASE,
        )
        for block in blocks
    )
    if abstract_nodes and not has_body:
        abstract = " ".join(abstract_nodes[0].text_content().split())
        if abstract:
            return PaperText(
                title,
                "html",
                "abstract_only",
                [PaperBlock("H1", abstract, None, "Abstract")],
                [],
                pdf_urls,
            )
    if not blocks:
        raise ValueError("网页没有可提取的论文文本")
    # TODO: 按数据源提供结构化 HTML 适配器，以确认正文覆盖范围并保留更完整的公式。
    return PaperText(
        title,
        "html",
        "html_text",
        blocks,
        ["已提取网页文字，尚未确认覆盖完整论文；动态内容、图像及公式可能未被提取。"],
        pdf_urls,
    )
