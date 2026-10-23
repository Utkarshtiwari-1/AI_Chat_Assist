import importlib.util
import os
import sys
from urllib.request import urlopen


def main():
    print('NIT Raipur AI Assistant setup check')
    print(f'Python {sys.version.split()[0]}')
    modules = {'fastapi':'FastAPI', 'chromadb':'ChromaDB', 'sentence_transformers':'Sentence Transformers', 'bs4':'BeautifulSoup', 'requests':'Requests'}
    missing=[]
    for module,label in modules.items():
        ok=importlib.util.find_spec(module) is not None
        print(f"{'OK' if ok else 'MISSING'}  {label}")
        if not ok: missing.append(label)
    try:
        import fitz  # noqa: F401
        print('OK  PyMuPDF native module')
    except Exception as exc:
        print(f'BLOCKED  PyMuPDF native module could not load: {exc}')
        try:
            import pypdf  # noqa: F401
            print('OK  Pure-Python pypdf fallback is available')
        except ImportError:
            print('MISSING  Install pypdf fallback or check Windows Application Control / endpoint protection policy.')
    ollama=os.getenv('OLLAMA_BASE_URL','http://localhost:11434').rstrip('/')
    try:
        with urlopen(ollama+'/api/tags',timeout=2) as response:
            import json
            models=[m.get('name','') for m in json.load(response).get('models',[])]
        name=os.getenv('OLLAMA_CHAT_MODEL','llama3.2:3b')
        print(f"OK  Ollama reachable; configured model {'available' if any(m.split(':')[0]==name.split(':')[0] for m in models) else 'not found'} ({name})")
        if not any(m.split(':')[0]==name.split(':')[0] for m in models): print(f'    Run: ollama pull {name}')
    except Exception:
        print('MISSING  Ollama is not reachable. Install Ollama, start it, then run: ollama pull llama3.2:3b')
    if missing:
        print('Install backend dependencies: python -m pip install -r backend/requirements.txt')
    print('Embedding models download from Hugging Face on first indexing run and require internet access.')
    print('Data is stored in ./data; do not commit downloaded documents or Chroma files.')
    return bool(missing)


if __name__=='__main__': raise SystemExit(main())
