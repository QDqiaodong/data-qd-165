"""Strict local JSON contract and atomic report publication."""
from pathlib import Path
import hashlib, json, os, tempfile

def require(obj,keys):
    if not isinstance(obj,dict) or set(obj)!=set(keys.split()):raise ValueError('expected object fields: '+keys)

def ident(v):
    if not isinstance(v,str) or not v or v!=v.strip() or len(v)>128 or any(ord(c)<32 for c in v):
        raise ValueError('expected nonblank identifier, <=128 characters')
    return v

def number(v,minimum=0,maximum=10**9):
    if type(v) is not int or not minimum<=v<=maximum:raise ValueError('integer outside allowed bounds')
    return v

def rows(value,limit=20000):
    if not isinstance(value,list) or len(value)>limit:raise ValueError('expected bounded array')
    return value

def read(path):
    with Path(path).open('rb') as stream:raw=stream.read(8_000_001)
    if len(raw)>8_000_000:raise ValueError('JSON exceeds 8 MB')
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('duplicate JSON key: '+k)
            result[k]=v
        return result
    def invalid(v):raise ValueError('nonfinite JSON number: '+v)
    data=json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)
    return data,hashlib.sha256(raw).hexdigest()

def save(path,report):
    dest=Path(path)
    if dest.is_symlink():raise ValueError('output cannot be a symlink')
    dest.parent.mkdir(parents=True,exist_ok=True);temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=dest.parent,delete=False) as stream:
            temp=stream.name;json.dump(report,stream,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2);stream.write('\n')
        os.replace(temp,dest)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)
