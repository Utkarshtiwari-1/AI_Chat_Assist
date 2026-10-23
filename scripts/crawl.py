import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.services.knowledge import KB

if __name__=='__main__':
    result=KB.run_crawl(lambda p: print(p))
    print(result)
