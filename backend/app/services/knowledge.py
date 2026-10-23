import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

import requests

from ..config import get_settings
from .crawler import SEED_URLS, category_for, crawl, normalize_url
from .document_loader import chunk_text, digest, extract_document_metadata, extract_html, extract_pdf_bytes, fetch_source

log = logging.getLogger(__name__)


class KnowledgeBase:
    def __init__(self):
        self.settings = get_settings()
        self._lock = threading.RLock()
        self._model = None
        self._collection = None
        self._client = None
        self.data_dir = self.settings.path(self.settings.data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.extracted_dir = self.data_dir / "extracted"
        self.registry_path = self.data_dir / "sources.json"
        self.failures_path = self.data_dir / "crawl_failures.json"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def registry(self) -> list[dict]:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_registry(self, rows: list[dict]):
        temp = self.registry_path.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.registry_path)

    def failed_sources(self) -> list[dict]:
        try: return json.loads(self.failures_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError): return []

    def model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:
                raise RuntimeError("Sentence Transformers is unavailable. Install backend/requirements.txt and retry.") from exc
            self._model = SentenceTransformer(self.settings.embedding_model)
        return self._model

    def collection(self):
        if self._collection is None:
            try:
                import chromadb
            except Exception as exc:
                raise RuntimeError("ChromaDB is unavailable. Install backend/requirements.txt and retry.") from exc
            path = self.settings.path(self.settings.chroma_persist_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(path))
            self._collection = self._client.get_or_create_collection("nit_raipur", metadata={"hnsw:space": "cosine"})
        return self._collection

    @staticmethod
    def _id(source_id: str, content_hash: str, index: int) -> str:
        return digest(f"{source_id}:{content_hash}:{index}")

    def ingest(self, doc: dict, kind: str | None = None) -> dict:
        url = doc["url"]
        source_id = digest(url)[:24]
        title = doc.get("title") or url
        doc_kind = kind or doc.get("type", "html")
        fetched = datetime.now(timezone.utc).isoformat()
        all_chunks = []
        pages = doc.get("pages")
        if pages:
            for page in pages:
                for chunk in chunk_text(page["text"], self.settings.chunk_size, self.settings.chunk_overlap):
                    all_chunks.append((chunk, page.get("page_number"), None))
        else:
            for index, chunk in enumerate(chunk_text(doc.get("text", ""), self.settings.chunk_size, self.settings.chunk_overlap)):
                all_chunks.append((chunk, None, self._section(chunk)))
        if not all_chunks:
            raise ValueError("No extractable text found; the source was not indexed.")
        raw_bytes = doc.get("raw_bytes")
        if raw_bytes:
            ext = ".pdf" if doc_kind == "pdf" else ".html"
            (self.raw_dir / f"{source_id}{ext}").write_bytes(raw_bytes)
        full_content = "\n".join(x[0] for x in all_chunks)
        content_hash = digest(full_content)
        chunk_ids = [self._id(source_id, digest(chunk), i) for i, (chunk, _, _) in enumerate(all_chunks)]
        now_source = next((s for s in self.registry() if s["id"] == source_id), None)
        if now_source and now_source.get("content_hash") == content_hash:
            now_source["last_ingested"] = fetched
            self.save_registry(self.registry())
            return {**now_source, "status": "unchanged", "chunks": len(chunk_ids)}
        model = self.model()
        encoded = model.encode([x[0] for x in all_chunks], normalize_embeddings=True)
        embeddings = encoded.tolist() if hasattr(encoded, "tolist") else encoded
        metadata = []
        for i, (chunk, page, section) in enumerate(all_chunks):
            md = {"source_id": source_id, "source_url": url, "source_title": title, "source_type": doc_kind,
                  "category": doc.get("category", "local"), "fetched_at": fetched, "content_hash": digest(chunk), "chunk_index": i}
            if page is not None: md["page_number"] = int(page)
            if section: md["section_title"] = section
            if doc.get("published_date"): md["published_date"] = str(doc["published_date"])
            if doc.get("academic_session"): md["academic_session"] = str(doc["academic_session"])
            metadata.append(md)
        with self._lock:
            collection = self.collection()
            old = collection.get(where={"source_id": source_id}, include=[])
            collection.upsert(ids=chunk_ids, documents=[x[0] for x in all_chunks], metadatas=metadata, embeddings=embeddings)
            obsolete = [item for item in old["ids"] if item not in set(chunk_ids)]
            if obsolete:
                collection.delete(ids=obsolete)
            rows = [s for s in self.registry() if s["id"] != source_id]
            source = {"id": source_id, "url": url, "title": title, "category": doc.get("category", "local"),
                      "type": doc_kind, "status": "indexed", "content_hash": content_hash, "chunks": len(chunk_ids),
                      "last_ingested": fetched, "published_date": doc.get("published_date"), "academic_session": doc.get("academic_session")}
            rows.append(source)
            self.save_registry(rows)
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", source_id)
            (self.extracted_dir / f"{safe_id}.txt").write_text(full_content, encoding="utf-8")
            return source

    @staticmethod
    def _section(chunk: str) -> str | None:
        for line in chunk.splitlines():
            if line.lstrip().startswith("# "):
                return line.lstrip("# ").strip()[:120] or None
        return None

    def ingest_url(self, url: str) -> dict:
        normalized = normalize_url(url)
        body, ctype, _ = fetch_source(normalized, self.settings.request_timeout, self.settings.crawler_max_file_size)
        if ctype == "application/pdf" or normalized.lower().endswith(".pdf"):
            pages, needs_ocr = extract_pdf_bytes(body)
            if needs_ocr:
                raise ValueError("PDF has no extractable text; OCR is needed")
            page_text = "\n".join(page["text"] for page in pages)
            return self.ingest({"url": normalized, "title": normalized.rsplit("/", 1)[-1], "type": "pdf", "pages": pages, "raw_bytes": body, "category": category_for(normalized), **extract_document_metadata(page_text)})
        if ctype not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"Unsupported content type: {ctype or 'unknown'}")
        extracted = extract_html(body.decode("utf-8", errors="replace"), normalized)
        return self.ingest({"url": normalized, "title": extracted["title"], "type": "html", "text": extracted["text"], "raw_bytes": body, "category": category_for(normalized), **extract_document_metadata(extracted["text"])})

    def ingest_pdf_upload(self, name: str, body: bytes) -> dict:
        if len(body) > self.settings.crawler_max_file_size:
            raise ValueError("File exceeds configured maximum size")
        if not body.startswith(b"%PDF-"):
            raise ValueError("Upload is not a valid PDF")
        pages, needs_ocr = extract_pdf_bytes(body)
        if needs_ocr:
            raise ValueError("PDF has no extractable text; OCR is needed")
        file_id = digest(name + digest(body.decode("latin1")))[:24]
        page_text = "\n".join(page["text"] for page in pages)
        return self.ingest({"url": f"local://{file_id}/{Path(name).name}", "title": Path(name).name, "type": "pdf", "pages": pages, "raw_bytes": body, **extract_document_metadata(page_text)}, "pdf")

    def search(self, question: str, top_k: int | None = None) -> list[dict]:
        collection = self.collection()
        if collection.count() == 0:
            return []
        encoded = self.model().encode([question], normalize_embeddings=True)
        encoded = encoded.tolist() if hasattr(encoded, "tolist") else encoded
        embedding = encoded[0]
        result = collection.query(query_embeddings=[embedding], n_results=min(top_k or self.settings.retrieval_top_k, collection.count()), include=["documents", "metadatas", "distances"])
        hits = []
        for doc, meta, distance, ident in zip(result["documents"][0], result["metadatas"][0], result["distances"][0], result["ids"][0]):
            score = max(0.0, 1.0 - float(distance))
            if score >= self.settings.relevance_threshold:
                hits.append({"id": ident, "text": doc, "score": score, **meta})
        deduped = []
        for hit in hits:
            tokens = set(hit["text"].lower().split())
            similar = False
            for other in deduped:
                other_tokens = set(other["text"].lower().split())
                overlap = len(tokens & other_tokens) / max(1, min(len(tokens), len(other_tokens)))
                if hit["text"] in other["text"] or other["text"] in hit["text"] or overlap >= 0.82:
                    similar = True
                    break
            if not similar:
                deduped.append(hit)
        return deduped

    def status(self) -> dict:
        ollama = {"available": False, "model_available": False, "error": None}
        try:
            response = requests.get(self.settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=2)
            response.raise_for_status()
            ollama["available"] = True
            tags = response.json().get("models", [])
            configured = self.settings.ollama_chat_model if ":" in self.settings.ollama_chat_model else self.settings.ollama_chat_model + ":latest"
            ollama["model_available"] = any((x.get("name", "") if ":" in x.get("name", "") else x.get("name", "") + ":latest") == configured for x in tags)
        except Exception as exc:
            ollama["error"] = "Ollama is not running or not reachable. Install Ollama, start it, and pull the configured model."
        rows = self.registry()
        chunks = sum(s.get("chunks", 0) for s in rows if s.get("status") == "indexed")
        try:
            count = self.collection().count()
        except Exception:
            count = chunks
        configured_embedding = Path(self.settings.embedding_model).expanduser()
        if configured_embedding.exists():
            embedding_available = True
        else:
            cache = Path(os.getenv("HF_HUB_CACHE") or (Path(os.getenv("HF_HOME")) / "hub" if os.getenv("HF_HOME") else Path.home() / ".cache" / "huggingface" / "hub"))
            cache_name = "models--" + self.settings.embedding_model.replace("/", "--")
            embedding_available = (cache / cache_name).exists()
        return {"backend": "ok", "ollama": ollama, "model": self.settings.ollama_chat_model,
                "embedding_model": self.settings.embedding_model, "documents": len(rows), "chunks": count,
                "embedding_model_available": embedding_available,
                "last_ingestion": max((s.get("last_ingested", "") for s in rows), default=None) or None,
                "knowledge_base_ready": count > 0}

    def stats(self) -> dict:
        rows = self.registry()
        count = sum(x.get("chunks", 0) for x in rows)
        return {"documents": len(rows), "chunks": count, "categories": _counts(rows, "category"), "statuses": _counts(rows, "status")}

    def remove(self, source_id: str) -> bool:
        rows = self.registry()
        source = next((r for r in rows if r["id"] == source_id), None)
        if not source:
            return False
        try:
            c = self.collection()
            found = c.get(where={"source_id": source_id}, include=[])
            if found["ids"]: c.delete(ids=found["ids"])
        except Exception:
            pass
        self.save_registry([r for r in rows if r["id"] != source_id])
        (self.extracted_dir / f"{source_id}.txt").unlink(missing_ok=True)
        (self.raw_dir / f"{source_id}.pdf").unlink(missing_ok=True)
        (self.raw_dir / f"{source_id}.html").unlink(missing_ok=True)
        return True

    def reindex(self):
        rows = self.registry()
        self.collection()
        self._client.delete_collection("nit_raipur")
        self._collection = None
        self.save_registry([])
        for row in rows:
            source_id = row["id"]
            raw_pdf = self.raw_dir / f"{source_id}.pdf"
            raw_html = self.raw_dir / f"{source_id}.html"
            if raw_pdf.exists():
                body = raw_pdf.read_bytes()
                pages, needs_ocr = extract_pdf_bytes(body)
                if needs_ocr: raise ValueError(f"PDF requires OCR: {row['title']}")
                page_text = "\n".join(page["text"] for page in pages)
                self.ingest({"url": row["url"], "title": row["title"], "type": "pdf", "pages": pages, "raw_bytes": body, "category": row.get("category", "local"), **extract_document_metadata(page_text)}, "pdf")
            elif raw_html.exists():
                body = raw_html.read_bytes()
                extracted = extract_html(body.decode("utf-8", errors="replace"), row["url"])
                self.ingest({"url": row["url"], "title": row["title"], "type": "html", "text": extracted["text"], "raw_bytes": body, "category": row.get("category", "institute"), **extract_document_metadata(extracted["text"])})
            elif row["url"].startswith("http"):
                self.ingest_url(row["url"])
            else:
                raise ValueError(f"Original file is missing for {row['title']}; upload it again.")
        return {"reindexed": len(rows)}

    def run_crawl(self, progress: Callable | None = None, seeds=None):
        settings = self.settings
        configured_seeds = [item.strip() for item in settings.crawler_seed_urls.split(",") if item.strip()]
        docs, failures = crawl(seeds or configured_seeds or SEED_URLS, settings.crawler_max_pages, settings.crawler_max_depth,
                               settings.request_timeout, settings.crawler_max_file_size, progress)
        success, errors, skipped = [], failures, []
        for doc in docs:
            try:
                result = self.ingest(doc)
                (skipped if result.get("status") == "unchanged" else success).append({"url": doc["url"], "title": result["title"]})
            except Exception as exc:
                errors.append({"url": doc["url"], "reason": str(exc)[:500]})
            if progress:
                progress({"visited": len(docs), "queued": 0, "indexed": len(success), "skipped": len(skipped), "failed": len(errors)})
        old_failures = {row.get("url"): row for row in self.failed_sources()}
        for item in success + skipped: old_failures.pop(item["url"], None)
        for item in errors: old_failures[item.get("url")] = item
        self.failures_path.write_text(json.dumps(list(old_failures.values()), indent=2), encoding="utf-8")
        return {"discovered": len(docs), "processed": success, "skipped": skipped, "failures": errors}


def _counts(rows, key):
    out = {}
    for row in rows: out[row.get(key, "unknown")] = out.get(row.get(key, "unknown"), 0) + 1
    return out


KB = KnowledgeBase()
