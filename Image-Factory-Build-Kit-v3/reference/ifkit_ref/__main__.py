import argparse
import json
from .demo import run_demo

p=argparse.ArgumentParser(description='Image Factory offline development reference; no live calls')
s=p.add_subparsers(dest='command',required=True)
d=s.add_parser('demo')
d.add_argument('--out',required=True)
a=p.parse_args()
try:
    result=run_demo(a.out)
except Exception as exc:
    p.exit(1,f'{type(exc).__name__}: {exc}\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
