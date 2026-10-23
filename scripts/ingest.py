import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.services.knowledge import KB

parser=argparse.ArgumentParser(description='Ingest an allowed official URL or local PDF into the persistent index.')
parser.add_argument('--url')
parser.add_argument('--pdf')
args=parser.parse_args()
if args.url: print(KB.ingest_url(args.url))
elif args.pdf:
    path=Path(args.pdf)
    print(KB.ingest_pdf_upload(path.name,path.read_bytes()))
else: parser.error('Provide --url or --pdf')
