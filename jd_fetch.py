from __future__ import annotations

import ipaddress
import os
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class JDFetchError(RuntimeError):
    pass


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise JDFetchError("职位链接只支持公开的HTTP/HTTPS地址。")
    if parsed.username or parsed.password:
        raise JDFetchError("职位链接不能包含账号信息。")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise JDFetchError("无法解析职位链接，请改为粘贴JD或上传截图。") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise JDFetchError("出于安全原因，不能读取本机或内网地址。")


class _SafeRedirect(HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        _validate_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def fetch_jd_text(url: str) -> str:
    if os.getenv("ENABLE_JD_URL_FETCH", "false").lower() not in {"1", "true", "yes"}:
        raise JDFetchError("职位链接读取当前未启用，请粘贴JD或上传截图。")
    _validate_public_url(url)
    timeout = max(1, min(int(os.getenv("JD_FETCH_TIMEOUT_SECONDS", "8")), 15))
    max_bytes = max(100_000, min(int(os.getenv("JD_FETCH_MAX_BYTES", "2000000")), 3_000_000))
    request = Request(url, headers={"User-Agent": "CareerAgentBeta/0.3"})
    try:
        with build_opener(_SafeRedirect()).open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "text/plain"}:
                raise JDFetchError("该链接不是可读取的职位文本页面。")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise JDFetchError("职位页面内容过大，请直接粘贴JD。")
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
    except JDFetchError:
        raise
    except Exception as exc:
        raise JDFetchError("无法直接读取该链接，请粘贴JD或上传截图。") from exc
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        text = " ".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    result = "\n".join(dict.fromkeys(lines))
    if len(result) < 80:
        raise JDFetchError("未能提取足够的职位信息，请粘贴JD或上传截图。")
    return result[:50_000]
