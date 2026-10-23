import re
import time
import gzip
import urllib.robotparser
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .document_loader import extract_document_metadata, extract_html, extract_pdf_bytes, fetch_source

SEED_URLS = ["https://www.nitrr.ac.in/"]
ALLOWED_HOSTS = {"nitrr.ac.in", "www.nitrr.ac.in"}
USER_AGENT = "NITRaipurAssistant/1.0 (local academic information crawler)"
RELEVANCE = re.compile(r"admission|academic|syllab|ordinance|calendar|notice|hostel|fee|department|faculty|placement|research|library|student|welfare|program|course|exam|scholar|annual|download|circular|\.(pdf)$", re.I)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname or not is_allowed_host(parsed.hostname):
        raise ValueError("URL host is outside the official NIT Raipur allowlist")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("Credentials and nonstandard ports are not allowed")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = "&".join(sorted(q for q in parsed.query.split("&") if q and not q.lower().startswith(("utm_", "fbclid="))))
    return urldefrag(urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, "")))[0]


def is_allowed_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in ALLOWED_HOSTS


def register_official_host(host: str, discovered_from: str) -> bool:
    host, source = host.lower().rstrip("."), discovered_from.lower().rstrip(".")
    if source in ALLOWED_HOSTS and (host == "nitrr.ac.in" or host.endswith(".nitrr.ac.in")):
        ALLOWED_HOSTS.add(host)
        return True
    return False


def category_for(url: str) -> str:
    value = url.lower()
    for category, terms in {
        "admissions": ("admission", "josaa", "csab", "seat", "fee"),
        "academics": ("academic", "syllab", "ordinance", "course", "calendar", "exam", "program"),
        "student-life": ("hostel", "student", "welfare", "library", "club"),
        "departments": ("department", "faculty", "laborator"),
        "placements": ("placement", "training"),
        "research": ("research", "innovation"),
        "notices": ("notice", "circular", "announcement"),
    }.items():
        if any(term in value for term in terms):
            return category
    return "institute"


def discover_sitemaps(base: str, timeout: int = 12) -> list[str]:
    urls = [urljoin(base, "/sitemap.xml")]
    rp = urllib.robotparser.RobotFileParser()
    try:
        response = requests.get(urljoin(base, "/robots.txt"), timeout=timeout, headers={"User-Agent": USER_AGENT}, allow_redirects=False)
        if response.ok:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    raw_candidate = line.split(":", 1)[1].strip()
                    register_official_host(urlparse(raw_candidate).hostname or "", urlparse(base).hostname or "")
                    candidate = normalize_url(raw_candidate)
                    if urlparse(candidate).path.lower().endswith(".xml"):
                        urls.append(candidate)
    except requests.RequestException:
        pass
    found, seen_sitemaps = [], set()
    pending = list(dict.fromkeys(urls))
    while pending and len(seen_sitemaps) < 20:
        sitemap = pending.pop(0)
        if sitemap in seen_sitemaps: continue
        seen_sitemaps.add(sitemap)
        try:
            resp = requests.get(sitemap, timeout=timeout, headers={"User-Agent": USER_AGENT}, allow_redirects=False)
            if not resp.ok:
                continue
            if len(resp.content) > 10_000_000:
                continue
            payload = gzip.decompress(resp.content) if urlparse(sitemap).path.lower().endswith(".gz") else resp.content
            if len(payload) > 10_000_000:
                continue
            root = ET.fromstring(payload)
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
                    try:
                        candidate_host = urlparse(node.text.strip()).hostname or ""
                        register_official_host(candidate_host, urlparse(sitemap).hostname or "")
                        normalized = normalize_url(node.text.strip())
                        if urlparse(normalized).path.lower().endswith((".xml", ".xml.gz")):
                            if len(pending) < 20: pending.append(normalized)
                        else:
                            if len(found) < 5000: found.append(normalized)
                    except ValueError:
                        continue
        except (requests.RequestException, ET.ParseError, OSError):
            continue
    return found


def crawl(seeds: list[str], max_pages: int, max_depth: int, timeout: int, max_file_size: int, progress=None) -> tuple[list[dict], list[dict]]:
    queue = deque((normalize_url(url), 0) for url in seeds)
    for seed in list(seeds):
        try:
            for found in discover_sitemaps(seed, timeout):
                if RELEVANCE.search(found):
                    queue.append((found, 1))
        except ValueError:
            pass
    queued, visited, documents, failures = set(), set(), [], []
    rp_cache = {}
    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited or depth > max_depth:
            continue
        visited.add(url)
        if progress:
            progress({"visited": len(visited), "queued": len(queue), "indexed": len(documents), "failed": len(failures), "current_url": url})
        parsed = urlparse(url)
        robots_blocked = False
        try:
            if parsed.netloc in rp_cache:
                rp = rp_cache[parsed.netloc]
                if rp is False:
                    robots_blocked = True
                    rp = None
            else:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    robots = requests.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=timeout,
                                          headers={"User-Agent": USER_AGENT}, allow_redirects=False)
                    if robots.ok:
                        rp.parse(robots.text.splitlines())
                    elif robots.status_code == 404:
                        rp = None
                    else:
                        robots_blocked = True
                        rp = False
                        failures.append({"url": url, "reason": f"Could not verify robots.txt (HTTP {robots.status_code}); skipped conservatively"})
                except requests.RequestException as exc:
                    robots_blocked = True
                    rp = False
                    failures.append({"url": url, "reason": f"Could not verify robots.txt; skipped conservatively: {exc}"})
                rp_cache[parsed.netloc] = rp
            if robots_blocked:
                continue
            if rp and not rp.can_fetch(USER_AGENT, url):
                failures.append({"url": url, "reason": "Disallowed by robots.txt"})
                continue
            if len(visited) > 1:
                delay = rp.crawl_delay(USER_AGENT) if rp else None
                time.sleep(float(delay) if delay is not None else 0.15)
            content, ctype, modified = fetch_source(url, timeout, max_file_size)
            if len(content) > max_file_size:
                raise ValueError("File exceeds configured maximum size")
            if ctype == "application/pdf" or urlparse(url).path.lower().endswith(".pdf"):
                pages, needs_ocr = extract_pdf_bytes(content)
                if needs_ocr:
                    failures.append({"url": url, "reason": "PDF has no extractable text; OCR is needed"})
                    continue
                title = urlparse(url).path.rsplit("/", 1)[-1] or "PDF document"
                page_text = "\n".join(page["text"] for page in pages)
                documents.append({"url": url, "title": title, "type": "pdf", "pages": pages, "raw_bytes": content, "category": category_for(url), "last_modified": modified or None, **extract_document_metadata(page_text)})
            else:
                info = extract_html(content.decode("utf-8", errors="replace"), url)
                if info["text"]:
                    documents.append({"url": url, "title": info["title"], "type": "html", "text": info["text"], "raw_bytes": content, "category": category_for(url), "last_modified": modified or None, **extract_document_metadata(info["text"])})
                if depth < max_depth:
                    soup = BeautifulSoup(content, "html.parser")
                    for link in soup.find_all("a", href=True):
                        try:
                            absolute = urljoin(url, link["href"])
                            register_official_host(urlparse(absolute).hostname or "", parsed.hostname or "")
                            target = normalize_url(absolute)
                        except ValueError:
                            continue
                        if target not in visited and target not in queued and (target.lower().endswith(".pdf") or RELEVANCE.search(target) or depth == 0):
                            queue.append((target, depth + 1))
                            queued.add(target)
        except Exception as exc:
            failures.append({"url": url, "reason": str(exc)[:500]})
    return documents, failures
