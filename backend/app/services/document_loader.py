import hashlib
import io
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def extract_html(html: str, url: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else urlparse(url).netloc
    main = soup.find("main") or soup.find("article") or soup.body or soup
    blocks = []
    for node in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th"], recursive=True):
        value = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if value and (not blocks or blocks[-1] != value):
            if node.name in {"h1", "h2", "h3", "h4"}:
                value = f"{'#' * int(node.name[1])} {value}"
            blocks.append(value)
    text = "\n".join(blocks).strip()
    return {"title": title, "text": text, "content_hash": digest(text)}


def extract_document_metadata(text: str) -> dict:
    metadata = {}
    session = re.search(r"(?:academic\s+)?(?:session|year)\s*[:\-]?\s*((?:20)\d{2}\s*[-–/]\s*(?:20)?\d{2})", text, re.I)
    if session:
        metadata["academic_session"] = re.sub(r"\s+", "", session.group(1)).replace("–", "-").replace("/", "-")
    date_pattern = r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})"
    published = re.search(rf"(?:published\s+on|issue\s+date|issued\s+on|dated)\s*[:\-]?\s*{date_pattern}", text, re.I)
    if published:
        metadata["published_date"] = published.group(1).strip()
    return metadata


def extract_pdf_bytes(content: bytes) -> tuple[list[dict], bool]:
    try:
        import fitz
        doc = fitz.open(stream=content, filetype="pdf")
        pages = [{"page_number": i + 1, "text": page.get_text("text").strip()} for i, page in enumerate(doc)]
    except (ImportError, OSError):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("Neither PyMuPDF nor the pure-Python pypdf fallback is available. Install backend/requirements.txt.") from exc
        reader = PdfReader(io.BytesIO(content), strict=False)
        pages = [{"page_number": i + 1, "text": (page.extract_text() or "").strip()} for i, page in enumerate(reader.pages)]
    pages = [p for p in pages if p["text"]]
    return pages, len(pages) == 0


def extract_pdf(path: str | Path) -> tuple[list[dict], bool]:
    return extract_pdf_bytes(Path(path).read_bytes())


def fetch_source(url: str, timeout: int = 15, max_bytes: int = 20_000_000) -> tuple[bytes, str, str]:
    last_error = None
    response = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=timeout, headers={"User-Agent": "NITRaipurAssistant/1.0 (local academic information crawler)"}, stream=True, allow_redirects=False)
            if response.status_code not in {408, 425, 429} and response.status_code < 500:
                break
            last_error = requests.HTTPError(f"Temporary HTTP status {response.status_code}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.5 * (2 ** attempt))
    if response is None:
        raise last_error or requests.RequestException("Request failed")
    if 300 <= response.status_code < 400:
        raise ValueError("Redirects are not followed; verify the destination URL is an allowed official host.")
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
    if content_type not in {"text/html", "application/pdf", "application/xhtml+xml"}:
        raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("File exceeds the configured download limit.")
        chunks.append(chunk)
    return b"".join(chunks), content_type, response.headers.get("Last-Modified", "")


def chunk_text(text: str, size: int = 900, overlap: int = 180) -> list[str]:
    if size < 100 or overlap < 0 or overlap >= size:
        raise ValueError("chunk size must be >=100 and overlap must be between 0 and size")
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    if not paragraphs:
        return []
    chunks, current = [], ""
    for para in paragraphs:
        units = []
        for sentence in (re.split(r"(?<=[.!?])\s+", para) if len(para) > size else [para]):
            if len(sentence) <= size:
                units.append(sentence)
                continue
            piece = ""
            for word in sentence.split():
                candidate = f"{piece} {word}".strip()
                if len(candidate) <= size:
                    piece = candidate
                else:
                    if piece: units.append(piece)
                    piece = word[:size]
                    remainder = word[size:]
                    while remainder:
                        units.append(piece)
                        piece = remainder[:size]
                        remainder = remainder[size:]
            if piece: units.append(piece)
        for unit in units:
            candidate = f"{current}\n{unit}".strip() if current else unit
            if len(candidate) <= size:
                current = candidate
            else:
                if current: chunks.append(current)
                carry = current[-overlap:] if overlap else ""
                if len(carry) + len(unit) + 1 > size:
                    carry = carry[-max(0, size - len(unit) - 1):] if size - len(unit) - 1 else ""
                current = f"{carry}\n{unit}".strip() if carry else unit
    if current:
        chunks.append(current)
    return [c for c in chunks if c]
