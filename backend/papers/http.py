"""论文网络读取：限制响应大小、请求时间与重定向，并只访问公开 HTTP 地址。"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from backend.config import PaperSettings


@dataclass(frozen=True)
class PaperResponse:
    """已完成下载的资源；不保留认证请求头。"""

    content: bytes  # 解压后的响应内容。
    url: str  # 最终资源地址。
    content_type: str  # 响应声明的 MIME 类型。


class PaperHttp:
    """给题录源与正文阅读器共用的、有界网络读取器。"""

    def __init__(self, settings: PaperSettings) -> None:
        """初始化读取器。

        Args:
            settings: 请求限速、时间、大小和重定向配置。

        Returns:
            None。
        """
        self.settings = settings  # 显式网络配置。
        self._lock = threading.Lock()  # 串行分配请求开始时间。
        self._last_request = 0.0  # 最近请求的单调时间。

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PaperResponse:
        """下载公开资源，逐跳校验地址，跨源跳转时丢弃认证头。

        Args:
            url: HTTP(S) 地址，不能包含账号、密码或非公开主机。
            params: 首次请求的查询参数。
            headers: 首次请求附加头，仅在同源跳转时保留。

        Returns:
            内容、最终地址及 MIME 类型。

        Raises:
            ValueError: 地址或资源大小超出允许范围。
            OSError: 请求失败、被限流或跳转次数超限。
            TimeoutError: 下载超出时间上限。
        """
        request_url = httpx.URL(url)
        if params is not None:
            request_url = request_url.copy_merge_params(params)
        request_headers = {"User-Agent": self.settings.user_agent, **(headers or {})}
        deadline = time.monotonic() + self.settings.request_timeout_seconds
        for _ in range(self.settings.max_redirects + 1):
            validate_public_url(str(request_url))
            with self._lock:
                delay = self.settings.request_interval_seconds - (
                    time.monotonic() - self._last_request
                )
                if delay > 0:
                    time.sleep(delay)
                self._last_request = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("论文资源下载超时")
            try:
                with httpx.stream(
                    "GET",
                    request_url,
                    headers=request_headers,
                    timeout=remaining,
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        target = response.headers.get("location")
                        if not target:
                            raise OSError("论文资源跳转缺少目标地址")
                        next_url = request_url.join(target)
                        if (next_url.scheme, next_url.host, next_url.port) != (
                            request_url.scheme,
                            request_url.host,
                            request_url.port,
                        ):
                            request_headers = {"User-Agent": self.settings.user_agent}
                        request_url = next_url
                        continue
                    if response.status_code == 429:
                        raise OSError("论文服务请求受限（429），请稍后重试")
                    if response.is_error:
                        raise OSError(f"论文服务返回 HTTP {response.status_code}")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() > deadline:
                            raise TimeoutError("论文资源下载超时")
                        if len(content) + len(chunk) > self.settings.max_download_bytes:
                            raise ValueError("论文资源超过 papers.max_download_bytes")
                        content.extend(chunk)
                    return PaperResponse(
                        bytes(content),
                        str(response.url),
                        response.headers.get("content-type", "").lower(),
                    )
            except httpx.HTTPError as error:
                # 不输出 httpx 的完整请求信息，避免认证参数进入工具结果。
                raise OSError(f"论文资源请求失败: {type(error).__name__}") from error
        raise OSError("论文资源重定向次数超过配置上限")


def validate_public_url(url: str) -> None:
    """校验公开 HTTP(S) 地址及解析到的所有 IP。

    Args:
        url: 待访问的地址。

    Returns:
        None。

    Raises:
        ValueError: 协议、凭证、端口或解析地址不符合边界。
        OSError: 主机无法解析。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("论文资源必须使用 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("论文地址不能包含认证信息或非标准端口")
    addresses = socket.getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise ValueError("论文工具只允许访问公开网络地址")
