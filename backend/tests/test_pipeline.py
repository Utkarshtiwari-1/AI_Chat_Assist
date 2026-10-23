import io
import pytest
from pypdf import PdfWriter
from fastapi.testclient import TestClient

from app.main import app
from app.services.crawler import is_allowed_host, normalize_url, register_official_host
from app.services.document_loader import chunk_text, extract_document_metadata, extract_html, extract_pdf_bytes
from app.services.knowledge import KnowledgeBase


def test_html_extraction_removes_navigation_and_preserves_headings():
    result = extract_html('<html><head><title>Sample</title></head><body><nav>Menu</nav><main><h1>Overview</h1><p>Test text</p><script>bad()</script></main><footer>Bottom</footer></body></html>', 'https://www.nitrr.ac.in/about')
    assert result['title'] == 'Sample'
    assert 'Overview' in result['text'] and 'Test text' in result['text']
    assert 'Menu' not in result['text'] and 'Bottom' not in result['text']
    assert result['text'].startswith('# Overview')


def test_explicit_session_and_publication_date_metadata_only():
    metadata=extract_document_metadata('Academic Session: 2025-26. Published on: 12 August 2025.')
    assert metadata=={'academic_session':'2025-26','published_date':'12 August 2025'}
    assert extract_document_metadata('Fetched today; no explicit issue date.')=={}


def test_pdf_extraction_has_page_numbers_and_reports_scanned():
    # A tiny text-bearing PDF fixture; pypdf is the fallback on hosts where the
    # PyMuPDF native module is unavailable or blocked by endpoint policy.
    content=b'BT /F1 12 Tf 72 720 Td (Synthetic institute document) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream']
    text_pdf=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(text_pdf));text_pdf.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(text_pdf);text_pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:text_pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    text_pdf.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    pages, needs_ocr = extract_pdf_bytes(bytes(text_pdf))
    assert not needs_ocr and pages[0]['page_number'] == 1 and 'Synthetic' in pages[0]['text']
    writer=PdfWriter();writer.add_blank_page(width=612,height=792);blank=io.BytesIO();writer.write(blank)
    assert extract_pdf_bytes(blank.getvalue())[1]


def test_chunking_obeys_size_and_overlap():
    chunks = chunk_text(('Paragraph with useful information. ' * 25) + '\nSecond section content.', 140, 30)
    assert len(chunks) > 1 and all(len(c) <= 140 for c in chunks)
    assert chunks[0][-12:] in chunks[1]


class FakeModel:
    def encode(self, texts, normalize_embeddings=True):
        return [[float(len(t)), 1.0] for t in texts]


class FakeCollection:
    def __init__(self): self.rows = {}
    def count(self): return len(self.rows)
    def get(self, where=None, include=None):
        ids=[i for i,r in self.rows.items() if not where or r['metadata'].get('source_id')==where.get('source_id')]
        return {'ids':ids}
    def delete(self, ids):
        for i in ids: self.rows.pop(i, None)
    def upsert(self, ids, documents, metadatas, embeddings):
        for i,d,m,e in zip(ids,documents,metadatas,embeddings): self.rows[i]={'document':d,'metadata':m,'embedding':e}
    def query(self, query_embeddings, n_results, include):
        rows=list(self.rows.items())[:n_results]
        return {'ids':[[i for i,_ in rows]],'documents':[[r['document'] for _,r in rows]],'metadatas':[[r['metadata'] for _,r in rows]],'distances':[[0.1 for _ in rows]]}


def fake_kb(tmp_path):
    kb=KnowledgeBase.__new__(KnowledgeBase)
    from app.config import get_settings
    kb.settings=get_settings(); kb._lock=__import__('threading').RLock(); kb._model=FakeModel(); kb._collection=FakeCollection();kb._client=None
    kb.data_dir=tmp_path;kb.raw_dir=tmp_path/'raw';kb.extracted_dir=tmp_path/'extracted';kb.registry_path=tmp_path/'sources.json'
    kb.raw_dir.mkdir();kb.extracted_dir.mkdir()
    return kb


def test_stable_chunk_ids_duplicate_prevention_and_changed_source_replacement(tmp_path):
    kb=fake_kb(tmp_path)
    doc={'url':'https://www.nitrr.ac.in/synthetic','title':'Synthetic','type':'html','text':'Synthetic program information. This is not a real NIT Raipur fact.'}
    first=kb.ingest(doc); ids=set(kb._collection.rows)
    again=kb.ingest(doc)
    assert first['id']==again['id'] and again['status']=='unchanged' and set(kb._collection.rows)==ids
    changed={**doc,'text':'Updated synthetic program information. This remains a testing document.'}
    kb.ingest(changed)
    assert set(kb._collection.rows).isdisjoint(ids) or set(kb._collection.rows)!=ids
    assert len(kb._collection.rows)==1


def test_metadata_and_remove_obsolete_source_chunks(tmp_path):
    kb=fake_kb(tmp_path)
    row=kb.ingest({'url':'https://www.nitrr.ac.in/test.pdf','title':'Synthetic PDF','type':'pdf','pages':[{'page_number':3,'text':'Synthetic page content for testing.'}]})
    record=next(iter(kb._collection.rows.values()))
    assert record['metadata']['page_number']==3 and record['metadata']['source_url'].endswith('test.pdf')
    assert kb.remove(row['id']) and kb._collection.count()==0 and kb.registry()==[]


def test_vector_retrieval_applies_threshold_and_returns_metadata(tmp_path):
    kb=fake_kb(tmp_path)
    kb.ingest({'url':'https://www.nitrr.ac.in/synthetic','title':'Synthetic','text':'Only a synthetic testing passage, not real institute information.'})
    assert kb.search('synthetic testing question')


def test_vector_retrieval_rejects_passages_below_configured_threshold(tmp_path, monkeypatch):
    kb=fake_kb(tmp_path)
    kb.ingest({'url':'https://www.nitrr.ac.in/synthetic','title':'Synthetic','text':'Synthetic testing passage.'})
    monkeypatch.setattr(kb.settings,'relevance_threshold',1.1)
    assert kb.search('unrelated question')==[]


def test_persistent_chroma_collection_round_trip(tmp_path, monkeypatch):
    from app.config import Settings
    kb=fake_kb(tmp_path)
    kb.settings=Settings(chroma_persist_dir=str(tmp_path/'chroma'),data_dir=str(tmp_path))
    kb._collection=None;kb._client=None
    monkeypatch.setattr(kb.settings,'relevance_threshold',0.0)
    kb.ingest({'url':'https://www.nitrr.ac.in/chroma-synthetic','title':'Synthetic Chroma fixture','text':'A synthetic passage stored in the local persistent vector collection.'})
    assert kb.collection().count()==1
    results=kb.search('synthetic persistent vector collection')
    assert results and results[0]['source_title']=='Synthetic Chroma fixture'


def test_url_allowlist_blocks_external_hosts_and_credentials():
    assert is_allowed_host('www.nitrr.ac.in') and not is_allowed_host('academics.nitrr.ac.in')
    assert register_official_host('academics.nitrr.ac.in','www.nitrr.ac.in')
    assert is_allowed_host('academics.nitrr.ac.in')
    assert not is_allowed_host('nitrr.ac.in.evil.example') and not is_allowed_host('example.com')
    with pytest.raises(ValueError): normalize_url('https://example.com')
    with pytest.raises(ValueError): normalize_url('https://user@www.nitrr.ac.in')
    assert normalize_url('https://www.nitrr.ac.in/page#section')=='https://www.nitrr.ac.in/page'


def test_api_health_and_request_validation():
    client=TestClient(app)
    assert client.get('/api/health').json()['status']=='ok'
    assert client.post('/api/chat',json={'question':'x'}).status_code==422
    assert client.post('/api/chat',json={'question':'x'*2100}).status_code==422
    assert client.post('/api/sources/url',json={'url':'https://example.com'}).status_code==400
    assert client.post('/api/sources/upload',files={'file':('payload.exe',b'nope','application/octet-stream')}).status_code==415


def test_synthetic_end_to_end_chat_has_citation(monkeypatch,tmp_path):
    kb=fake_kb(tmp_path)
    kb.ingest({'url':'https://www.nitrr.ac.in/synthetic','title':'Synthetic testing page','text':'Synthetic fact for test only: the example building is named Cedar Hall.'})
    monkeypatch.setattr('app.main.KB',kb)
    monkeypatch.setattr('app.main.generate_answer',lambda q,hits:'The synthetic test page names Cedar Hall [1].')
    response=TestClient(app).post('/api/chat',json={'question':'What name appears in the synthetic example building text?'})
    assert response.status_code==200
    data=response.json()
    assert data['evidence_sufficient'] and data['sources'][0]['title']=='Synthetic testing page'
    assert data['sources'][0]['url']=='https://www.nitrr.ac.in/synthetic'
    assert '[1]' in data['answer']
