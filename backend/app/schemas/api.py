from pydantic import BaseModel, Field, HttpUrl


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class ChatSource(BaseModel):
    id: str
    title: str
    url: str
    passage: str
    page_number: int | None = None
    published_date: str | None = None
    academic_session: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    evidence_sufficient: bool


class UrlSourceRequest(BaseModel):
    url: HttpUrl


class JobResponse(BaseModel):
    id: str
    status: str
    kind: str
    started_at: str
    finished_at: str | None = None
    progress: dict = {}
    error: str | None = None
