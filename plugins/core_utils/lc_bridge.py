# -*- coding: utf-8 -*-
"""LangChain 工具 AUP 桥接（沙盒验证版，2026-11-15）。

设计原则：
1. AUP 契约优先：对外只暴露带 @aup_tool 的函数，返回五字段 AUPResult。
2. 生产零框架依赖：搜索走 stdlib(urllib)，PDF 走独立 pypdf；模块导入时不 import LangChain。
3. LangChain 仅作可选后端：函数内部惰性导入，需显式设置 LC_BRIDGE_ALLOW_LANGCHAIN=1 才启用。
4. 入参硬校验：query 长度/结果数、PDF 路径根目录/后缀/体积全部在进入后端前拦截。

沙盒验证结论：DuckDuckGo 在本机出网被阻断，搜索后端改为 Bing HTML（LangChain
RequestsGetTool 与 stdlib 两条通道均实测可用）。
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import base64
import gzip
import html as html_lib
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from aup_core import AUPError, AUPResult, aup_tool


_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_BING_ENDPOINT = "https://www.bing.com/search"
_MAX_QUERY_CHARS = 200
_MAX_RESULTS = 5
_MAX_PDF_BYTES = 30 * 1024 * 1024
_MAX_PDF_PAGES = 20
_MAX_FILE_CHARS = 10000
_SUPPORTED_FILE_EXTENSIONS = {
    ".py", ".sh", ".json", ".txt", ".md", ".csv", ".pdf", ".docx", ".xlsx",
}
_DEFAULT_USER_PDF_ROOT = _ROOT + "/data/users"
_DEFAULT_PUBLIC_PDF_ROOT = _ROOT + "/data/public_uploads"
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_HTTP_TIMEOUT = 8.0


class BridgeError(Exception):
    """桥接层内部错误；由 AUP 包装层收敛为 AUPError。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _bing_url(query: str, max_results: int) -> str:
    params = {
        "q": query,
        "count": str(max(10, max_results * 2)),
        "setlang": "zh-Hans",
        "cc": "CN",
    }
    return _BING_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _unwrap_bing_url(url: str) -> str:
    """把 Bing /ck/a 跳转链接还原为真实目标地址。"""
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.bing.com" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        qs = urllib.parse.parse_qs(parsed.query)
        raw = (qs.get("u") or [""])[0]
        if raw.startswith("a1"):
            payload = raw[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                target = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
            except Exception:
                target = ""
            if target.startswith("http"):
                return target
    return url


def parse_bing_results(html_text: str, max_results: int) -> List[Dict[str, str]]:
    """解析 Bing 结果页：标题/真实链接/摘要。仅依赖 stdlib。"""
    blocks = re.split(r'<li[^>]+class="[^"]*b_algo[^"]*"', html_text or "", flags=re.I)
    results: List[Dict[str, str]] = []
    seen = set()
    for block in blocks[1:]:
        block = re.split(r"</li>", block, maxsplit=1, flags=re.I)[0]
        m = re.search(
            r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.I | re.S,
        )
        if not m:
            continue
        href = html_lib.unescape(m.group(1)).strip()
        title = _strip_tags(m.group(2))
        url = _unwrap_bing_url(href)
        if not title or not url.startswith("http") or url in seen:
            continue
        snippet_match = re.search(
            r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', block,
            flags=re.I | re.S,
        ) or re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        seen.add(url)
        results.append({"title": title[:300], "url": url[:1000], "snippet": snippet[:500]})
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# 纯 Python 实现（生产默认后端，不依赖 LangChain）
# ---------------------------------------------------------------------------
def native_bing_search(query: str, max_results: int = 3) -> Dict[str, Any]:
    """stdlib 版联网搜索：urllib 取 Bing HTML + 正则解析。"""
    html_text = _http_get(_bing_url(query, max_results))
    results = parse_bing_results(html_text, max_results)
    if not results:
        raise BridgeError("SEARCH_EMPTY", "Bing 页面未解析到结果（可能被风控或页面改版）")
    return {
        "engine": "bing",
        "backend": "native",
        "query": query,
        "count": len(results),
        "results": results,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _current_user_id() -> str:
    """读取本次请求的真实用户 ID；缺失或格式异常一律视为未识别。"""
    try:
        from plugins.core_utils.trust import get_context
        uid = str((get_context() or {}).get("user_id") or "").strip()
    except Exception:
        return ""
    return uid if _USER_ID_RE.fullmatch(uid) else ""


def _pdf_allowed_roots() -> List[str]:
    """返回本次请求允许访问的 PDF 根目录。

    默认只允许公共上传区与 data/users/<当前用户ID>/；LC_PDF_ROOT 仅作为
    受控测试覆盖，生产环境不要设置。
    """
    override = os.environ.get("LC_PDF_ROOT", "").strip()
    if override:
        return [os.path.realpath(os.path.expanduser(override))]
    roots = [os.path.realpath(_DEFAULT_PUBLIC_PDF_ROOT)]
    uid = _current_user_id()
    if uid:
        roots.append(os.path.realpath(os.path.join(_DEFAULT_USER_PDF_ROOT, uid)))
    return roots


def _is_under_root(root: str, real: str) -> bool:
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:
        return False


def _resolve_pdf_path(path: str) -> str:
    """入参硬校验：用户目录/公共区限制、存在性、后缀、体积。"""
    if not isinstance(path, str) or not path.strip():
        raise BridgeError("INVALID_ARGUMENT", "path 不能为空")
    real = os.path.realpath(os.path.expanduser(path.strip()))
    roots = _pdf_allowed_roots()
    if not any(_is_under_root(root, real) for root in roots):
        raise BridgeError(
            "PATH_NOT_ALLOWED",
            "PDF 路径必须位于 data/users/<当前用户ID>/ 或 data/public_uploads/ 之下",
        )
    if not os.path.isfile(real):
        raise BridgeError("FILE_NOT_FOUND", "PDF 文件不存在")
    if not real.lower().endswith(".pdf"):
        raise BridgeError("INVALID_ARGUMENT", "仅支持 .pdf 文件")
    if os.path.getsize(real) > _MAX_PDF_BYTES:
        raise BridgeError("FILE_TOO_LARGE", "PDF 超过 30MB 上限")
    return real


def _resolve_file_path(path: str) -> str:
    """通用文件入参校验：目录白名单、存在性、扩展名、体积。"""
    if not isinstance(path, str) or not path.strip():
        raise BridgeError("INVALID_ARGUMENT", "path 不能为空")
    real = os.path.realpath(os.path.expanduser(path.strip()))
    roots = _pdf_allowed_roots()
    if not any(_is_under_root(root, real) for root in roots):
        raise BridgeError(
            "PATH_NOT_ALLOWED",
            "文件路径必须位于 data/users/<当前用户ID>/ 或 data/public_uploads/ 之下",
        )
    if not os.path.isfile(real):
        raise BridgeError("FILE_NOT_FOUND", "文件不存在")
    ext = Path(real).suffix.lower()
    if ext not in _SUPPORTED_FILE_EXTENSIONS:
        raise BridgeError(
            "INVALID_FILE_TYPE",
            "仅支持 .py/.sh/.json/.txt/.md/.pdf/.docx/.xlsx/.csv 文件",
        )
    if os.path.getsize(real) > _MAX_PDF_BYTES:
        raise BridgeError("FILE_TOO_LARGE", "文件超过 30MB 上限")
    return real


def _read_text(real: str) -> str:
    raw = Path(real).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _extract_docx_text(real: str) -> str:
    """stdlib 极简 DOCX 正文提取：读取 word/document.xml 的段落文本。"""
    with zipfile.ZipFile(real) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: List[str] = []
    for para in root.findall(".//w:p", ns):
        texts = [node.text or "" for node in para.findall(".//w:t", ns)]
        text = "".join(texts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def _extract_xlsx_text(real: str) -> str:
    """stdlib 极简 XLSX 提取：共享字符串 + 各 worksheet 单元格文本。"""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared: List[str] = []
    with zipfile.ZipFile(real) as z:
        names = z.namelist()
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(".//s:si", ns):
                shared.append("".join(n.text or "" for n in si.findall(".//s:t", ns)))
        sheets = sorted(n for n in names
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        parts: List[str] = []
        for index, sheet_name in enumerate(sheets, start=1):
            root = ET.fromstring(z.read(sheet_name))
            rows: List[str] = []
            for row in root.findall(".//s:row", ns):
                cells: List[str] = []
                for cell in row.findall(".//s:c", ns):
                    ref = cell.get("r") or ""
                    ctype = cell.get("t") or ""
                    value_node = cell.find("s:v", ns)
                    value = value_node.text if value_node is not None else ""
                    if ctype == "s" and value:
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                    elif not value:
                        inline = cell.find(".//s:t", ns)
                        if inline is not None:
                            value = inline.text or ""
                    if value:
                        cells.append("%s=%s" % (ref, value))
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append("[Sheet%d]\n%s" % (index, "\n".join(rows)))
    return "\n\n".join(parts).strip()


def native_file_extract(path: str, max_chars: int = 8000) -> Dict[str, Any]:
    """文本/代码直接读；DOCX/XLSX 极简提取；PDF 复用 pypdf。"""
    real = _resolve_file_path(path)
    ext = Path(real).suffix.lower()
    if ext == ".pdf":
        data = native_pdf_extract(real, 5)
        data["kind"] = "pdf"
        data["note"] = "PDF 文本提取；扫描件需要 OCR"
        return data
    if ext == ".docx":
        full = _extract_docx_text(real)
        note = "DOCX 仅做极简正文提取；仅支持内容提取，暂不支持直接操作复杂文档"
        kind = "docx"
    elif ext == ".xlsx":
        full = _extract_xlsx_text(real)
        note = "XLSX 仅做极简单元格提取；仅支持内容提取，暂不支持直接操作复杂文档"
        kind = "xlsx"
    else:
        full = _read_text(real)
        note = "文本/代码类文件直接读取，未做额外解析"
        kind = "text"
    if not full:
        raise BridgeError("EMPTY_DOCUMENT", "文件无可提取文本")
    chars = len(full)
    return {
        "backend": "native",
        "kind": kind,
        "path": real,
        "bytes": os.path.getsize(real),
        "chars": chars,
        "text": full[:max_chars],
        "truncated": chars > max_chars,
        "note": note,
    }



def native_pdf_extract(path: str, max_pages: int = 5) -> Dict[str, Any]:
    """pypdf 版 PDF 文本提取（独立依赖，不经过 LangChain）。"""
    real = _resolve_pdf_path(path)
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise BridgeError("PDF_BACKEND_MISSING", "缺少 pypdf，无法执行纯 Python 提取") from exc
    reader = PdfReader(real)
    total = len(reader.pages)
    parts: List[str] = []
    for page in reader.pages[:max_pages]:
        text = page.extract_text() or ""
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            parts.append(text)
    full = "\n\n".join(parts).strip()
    if not full:
        raise BridgeError("EMPTY_DOCUMENT", "PDF 无可提取文本（可能是扫描件，需要 OCR）")
    return {
        "backend": "native",
        "path": real,
        "pages_total": total,
        "pages_read": min(total, max_pages),
        "chars": len(full),
        "text": full[:8000],
        "truncated": len(full) > 8000,
    }


# ---------------------------------------------------------------------------
# 可选 LangChain 后端（惰性导入，仅桥接验证/回退时启用）
# ---------------------------------------------------------------------------
def langchain_bing_search(query: str, max_results: int = 3) -> Dict[str, Any]:
    """LangChain RequestsGetTool 版搜索。依赖 langchain-community。"""
    from langchain_community.tools import RequestsGetTool
    from langchain_community.utilities.requests import RequestsWrapper

    tool = RequestsGetTool(
        requests_wrapper=RequestsWrapper(headers={"User-Agent": _UA,
                                                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"}),
        allow_dangerous_requests=True,  # URL 由本函数固定构造，不接收外部任意地址
    )
    html_text = tool.run(_bing_url(query, max_results))
    results = parse_bing_results(str(html_text), max_results)
    if not results:
        raise BridgeError("SEARCH_EMPTY", "LangChain 通道未解析到 Bing 结果")
    return {
        "engine": "bing",
        "backend": "langchain",
        "query": query,
        "count": len(results),
        "results": results,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def langchain_pdf_extract(path: str, max_pages: int = 5) -> Dict[str, Any]:
    """LangChain PyPDFLoader 版 PDF 提取。依赖 langchain-community + pypdf。"""
    real = _resolve_pdf_path(path)
    from langchain_community.document_loaders import PyPDFLoader

    docs = PyPDFLoader(real).load()
    total = len(docs)
    text = "\n\n".join((d.page_content or "").strip() for d in docs[:max_pages]).strip()
    if not text:
        raise BridgeError("EMPTY_DOCUMENT", "PDF 无可提取文本")
    return {
        "backend": "langchain",
        "path": real,
        "pages_total": total,
        "pages_read": min(total, max_pages),
        "chars": len(text),
        "text": text[:8000],
        "truncated": len(text) > 8000,
    }


# ---------------------------------------------------------------------------
# AUP 对外工具
# ---------------------------------------------------------------------------
_SEARCH_SCHEMA = {
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "搜索关键词，1-200 字符"},
        "max_results": {"type": "integer", "description": "返回条数，1-5，默认 3"},
    },
}
_PDF_SCHEMA = {
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "PDF 绝对路径，必须位于 data/users/<当前用户ID>/ 或 data/public_uploads/ 之下"},
        "max_pages": {"type": "integer", "description": "最多读取页数，1-20，默认 5"},
    },
}


def _normalize_limits(query: Any, max_results: Any) -> tuple[str, int]:
    q = str(query or "").strip()
    if not q:
        raise BridgeError("INVALID_ARGUMENT", "query 不能为空")
    if len(q) > _MAX_QUERY_CHARS:
        raise BridgeError("INVALID_ARGUMENT", "query 超过 %d 字符" % _MAX_QUERY_CHARS)
    try:
        n = int(max_results)
    except Exception as exc:
        raise BridgeError("INVALID_ARGUMENT", "max_results 必须为整数") from exc
    return q, max(1, min(n, _MAX_RESULTS))


@aup_tool(
    name="lc_web_search",
    description="联网搜索首选工具：Bing HTML 返回标题/链接/摘要；普通新闻/实时信息本轮只调用它一次，禁止再用 web_search 或 search_and_read 重复检索",
    category="info_retrieval",
    is_danger=False,
    params_schema=_SEARCH_SCHEMA,
    suggestion_hint="搜索失败时检查网络出口或稍后重试",
)
def lc_web_search(query: str, max_results: int = 3) -> AUPResult:
    chain = ["lc_web_search:enter"]
    try:
        q, n = _normalize_limits(query, max_results)
    except BridgeError as exc:
        chain.append("lc_web_search:validate:rejected")
        raise AUPError(getattr(exc, "code", "INVALID_ARGUMENT"), str(exc),
                       fallback_chain=chain,
                       suggestion="query 需为 1-200 字符，max_results 需为 1-5") from exc
    chain.append("lc_web_search:validate:ok")
    try:
        data = native_bing_search(q, n)
        chain.append("lc_web_search:native:ok")
        return AUPResult(status="ok", data=data, error_code="0", fallback_chain=chain)
    except Exception as exc:
        chain.append("lc_web_search:native:failed:%s" % type(exc).__name__)
        if os.environ.get("LC_BRIDGE_ALLOW_LANGCHAIN") == "1":
            try:
                data = langchain_bing_search(q, n)
                chain.append("lc_web_search:langchain:ok")
                return AUPResult(status="ok", data=data, error_code="0", fallback_chain=chain)
            except Exception as lc_exc:
                chain.append("lc_web_search:langchain:failed:%s" % type(lc_exc).__name__)
        raise AUPError("TOOL_EXECUTION_FAILED", "联网搜索失败: %s" % exc,
                       detail={"query": q}, fallback_chain=chain,
                       suggestion="检查网络出口是否可访问 Bing，或稍后重试") from exc


@aup_tool(
    name="lc_pdf_extract",
    description="提取 PDF 文本（仅限当前用户沙盒或公共上传区；路径白名单 + 页数/体积限制）；pypdf 主通道",
    category="document",
    is_danger=False,
    params_schema=_PDF_SCHEMA,
    suggestion_hint="PDF 解析失败时检查路径、文件完整性或是否为扫描件",
)
def lc_pdf_extract(path: str, max_pages: int = 5) -> AUPResult:
    chain = ["lc_pdf_extract:enter"]
    try:
        real = _resolve_pdf_path(path)
        try:
            n = int(max_pages)
        except Exception as exc:
            raise BridgeError("INVALID_ARGUMENT", "max_pages 必须为整数") from exc
        n = max(1, min(n, _MAX_PDF_PAGES))
    except BridgeError as exc:
        chain.append("lc_pdf_extract:validate:rejected")
        raise AUPError(getattr(exc, "code", "INVALID_ARGUMENT"), str(exc),
                       fallback_chain=chain,
                       suggestion="path 需为 data/users/<当前用户ID>/ 或 data/public_uploads/ 下的 .pdf 文件，max_pages 1-20") from exc
    chain.append("lc_pdf_extract:validate:ok")
    try:
        data = native_pdf_extract(real, n)
        chain.append("lc_pdf_extract:pypdf:ok")
        return AUPResult(status="ok", data=data, error_code="0", fallback_chain=chain)
    except Exception as exc:
        chain.append("lc_pdf_extract:pypdf:failed:%s" % type(exc).__name__)
        if os.environ.get("LC_BRIDGE_ALLOW_LANGCHAIN") == "1":
            try:
                data = langchain_pdf_extract(real, n)
                chain.append("lc_pdf_extract:langchain:ok")
                return AUPResult(status="ok", data=data, error_code="0", fallback_chain=chain)
            except Exception as lc_exc:
                chain.append("lc_pdf_extract:langchain:failed:%s" % type(lc_exc).__name__)
        raise AUPError("TOOL_EXECUTION_FAILED", "PDF 提取失败: %s" % exc,
                       detail={"path": real}, fallback_chain=chain,
                       suggestion="检查 PDF 是否加密/损坏；扫描件需先 OCR") from exc


_FILE_SCHEMA = {
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "文件绝对路径，必须位于 data/users/<当前用户ID>/ 或 data/public_uploads/ 之下；支持 .py/.sh/.json/.txt/.md/.pdf/.docx/.xlsx/.csv"},
        "max_chars": {"type": "integer", "description": "最多返回字符数，1-10000，默认 8000"},
    },
}


@aup_tool(
    name="lc_file_extract",
    description="读取当前用户沙盒中的文本、代码、CSV、DOCX、XLSX 文件；DOCX/XLSX 仅做内容提取，不支持直接编辑或保留复杂格式",
    category="document",
    is_danger=False,
    params_schema=_FILE_SCHEMA,
    suggestion_hint="读取失败时检查文件是否在 data/users/<当前用户ID>/ 或 data/public_uploads/ 下，以及文件是否损坏",
)
def lc_file_extract(path: str, max_chars: int = 8000) -> AUPResult:
    chain = ["lc_file_extract:enter"]
    try:
        real = _resolve_file_path(path)
        try:
            n = int(max_chars)
        except Exception as exc:
            raise BridgeError("INVALID_ARGUMENT", "max_chars 必须为整数") from exc
        n = max(1, min(n, _MAX_FILE_CHARS))
    except BridgeError as exc:
        chain.append("lc_file_extract:validate:rejected")
        raise AUPError(getattr(exc, "code", "INVALID_ARGUMENT"), str(exc),
                       fallback_chain=chain,
                       suggestion="path 需为 data/users/<当前用户ID>/ 或 data/public_uploads/ 下的受支持文件，max_chars 1-10000") from exc
    chain.append("lc_file_extract:validate:ok")
    try:
        data = native_file_extract(real, n)
        chain.append("lc_file_extract:native:ok")
        return AUPResult(status="ok", data=data, error_code="0", fallback_chain=chain)
    except Exception as exc:
        chain.append("lc_file_extract:native:failed:%s" % type(exc).__name__)
        raise AUPError("TOOL_EXECUTION_FAILED", "文件读取失败: %s" % exc,
                       detail={"path": real}, fallback_chain=chain,
                       suggestion="检查文件是否损坏或编码异常；DOCX/XLSX 仅支持内容提取") from exc
