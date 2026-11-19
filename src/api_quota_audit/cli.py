DESCRIPTION='Exact fixed-window API usage and shared-capacity retention audits'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .chargeback import chargeback_analyze
from .model import analyze
from .reconcile import reconcile_analyze
from .retention import retention_analyze

def _same_file(source, output):
    """Reject the same path, hard links and symlink/path aliases."""
    try:
        if source.resolve() == output.resolve():
            return True
    except OSError:
        pass
    try:
        return output.exists() and output.samefile(source)
    except OSError:
        return False

def run(source, mode):
    data,digest=read(source)
    if mode=='chargeback':
        return {'schema_version':4,'mode':'chargeback','source_sha256':digest,'result':chargeback_analyze(data)}
    if mode=='retention':
        return {'schema_version':2,'mode':'retention','source_sha256':digest,'result':retention_analyze(data)}
    if mode=='reconcile':
        return {'schema_version':3,'mode':'reconcile','source_sha256':digest,'result':reconcile_analyze(data)}
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mode',choices=('audit','retention','reconcile','chargeback'),default='audit')
    args=parser.parse_args()
    try:
        if _same_file(args.source,args.output):raise ValueError('input and output must differ')
        report=run(args.source,args.mode);save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
