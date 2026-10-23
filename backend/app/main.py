import truststore

# Use the operating system's trusted roots (including Windows' certificate
# store) for requests. TLS verification remains enabled.
truststore.inject_into_ssl()

import logging
import threading
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .schemas.api import ChatRequest, ChatResponse, ChatSource, JobResponse, UrlSourceRequest
from .services.knowledge import KB
from .services.rag_pipeline import generate_answer
from .services.crawler import normalize_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title="NIT Raipur AI Assistant API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"])
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _start_job(kind, worker):
    job_id = uuid4().hex
    job = {"id": job_id, "status": "queued", "kind": kind,
           "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
           "progress": {}, "error": None, "result": None}
    with _jobs_lock: _jobs[job_id] = job

    def run():
        with _jobs_lock: job["status"] = "running"
        try:
            result = worker(lambda progress: job["progress"].update(progress))
            job["result"] = result
            job["status"] = "completed"
        except Exception as exc:
            log.exception("Background job %s failed", job_id)
            job["status"] = "failed"
            job["error"] = str(exc)[:500]
        finally:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=run, daemon=True, name=f"job-{kind}-{job_id[:6]}").start()
    return job


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "nit-raipur-ai-assistant"}


@app.get("/api/status")
def status():
    try: return KB.status()
    except Exception as exc: raise HTTPException(503, detail=f"Unable to inspect local services: {exc}")


@app.get("/api/stats")
def stats():
    try: return KB.stats()
    except Exception as exc: raise HTTPException(503, detail=str(exc))


@app.get("/api/chat/suggestions")
def suggestions():
    return {"suggestions": ["What is the admission process for B.Tech?", "Where can I find the CSE syllabus?",
                            "What are the hostel admission rules?", "What are the current academic calendar notices?",
                            "Which departments offer postgraduate programs?"]}


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    try: hits = KB.search(payload.question)
    except Exception as exc:
        log.exception("Retrieval failed")
        raise HTTPException(503, detail=f"Knowledge base is unavailable. Check local embedding dependencies and index setup: {exc}")
    if not hits:
        return ChatResponse(answer="I couldn't find sufficiently relevant information in the indexed sources to answer that. Try a more specific question, or add/crawl an official NIT Raipur source first.", sources=[], evidence_sufficient=False)
    try:
        answer = generate_answer(payload.question, hits)
    except Exception as exc:
        log.exception("Ollama answer generation failed")
        raise HTTPException(503, detail="Retrieved sources are available, but the local Ollama model could not answer. Start Ollama and pull the configured model.")
    sources = [ChatSource(id=h["id"], title=h.get("source_title") or "Untitled source", url=h.get("source_url") or "",
                          passage=h["text"], page_number=h.get("page_number"), published_date=h.get("published_date"),
                          academic_session=h.get("academic_session")) for h in hits]
    return ChatResponse(answer=answer, sources=sources, evidence_sufficient=True)


@app.get("/api/sources")
def sources():
    return {"sources": KB.registry()}


@app.post("/api/sources/url")
def add_url(payload: UrlSourceRequest):
    try: normalized = normalize_url(str(payload.url))
    except ValueError as exc: raise HTTPException(400, detail=str(exc))
    return _start_job("add-url", lambda progress: KB.ingest_url(normalized))


@app.post("/api/sources/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if file.content_type != "application/pdf" and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, detail="Upload a PDF file.")
    body = await file.read(settings.crawler_max_file_size + 1)
    if len(body) > settings.crawler_max_file_size:
        raise HTTPException(413, detail="File exceeds configured maximum size.")
    if not body.startswith(b"%PDF-"):
        raise HTTPException(400, detail="Upload is not a valid PDF.")
    filename = file.filename or "upload.pdf"
    return _start_job("upload-pdf", lambda progress: KB.ingest_pdf_upload(filename, body))


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    if not KB.remove(source_id): raise HTTPException(404, detail="Source not found")
    return {"removed": source_id}


@app.post("/api/ingestion/crawl", response_model=JobResponse)
def start_crawl():
    job = _start_job("crawl", lambda progress: KB.run_crawl(progress))
    return job


@app.post("/api/ingestion/incremental", response_model=JobResponse)
def incremental():
    job = _start_job("incremental", lambda progress: KB.run_crawl(progress))
    return job


@app.post("/api/ingestion/reindex", response_model=JobResponse)
def reindex():
    job = _start_job("reindex", lambda progress: KB.reindex())
    return job


@app.post("/api/ingestion/retry", response_model=JobResponse)
def retry_failed():
    failed = list(dict.fromkeys(item.get("url") for item in KB.failed_sources() if item.get("url")))
    if not failed: raise HTTPException(409, detail="There are no recorded failed crawl sources to retry.")
    job = _start_job("retry", lambda progress: KB.run_crawl(progress, seeds=failed))
    return job


@app.get("/api/ingestion/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock: job = _jobs.get(job_id)
    if not job: raise HTTPException(404, detail="Job not found")
    return job
