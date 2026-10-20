"""Canonical recovery identities shared by controller and private runner protocol."""
import hashlib
import json
import math
import re
from decimal import Decimal

FIELDS={'store_id','agent_id','conversation_id','session_id','cycle_id','client_message_id','consumer_id','input_sha','owner_revision','config_sha'}
ID=re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._:-]{0,255}\Z')
HASH=re.compile(r'[a-f0-9]{64}\Z')


def canonical(value):
    if value is None:return 'null'
    if value is True:return 'true'
    if value is False:return 'false'
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,int):
        if abs(value)>2**53-1:raise ValueError('Invalid recovery number')
        return str(value)
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Invalid recovery number')
        if value==0:return '0'
        if 1e-6<=abs(value)<1e21:return format(Decimal(repr(value)),'f').rstrip('0').rstrip('.') if '.' in format(Decimal(repr(value)),'f') else format(Decimal(repr(value)),'f')
        result=repr(value).lower();mantissa,exponent=result.split('e')
        return mantissa.rstrip('0').rstrip('.')+'e'+('+' if int(exponent)>=0 else '-')+str(abs(int(exponent)))
    if isinstance(value,list):return '['+','.join(map(canonical,value))+']'
    if isinstance(value,dict) and all(isinstance(key,str) for key in value):return '{'+','.join(json.dumps(key,ensure_ascii=False)+':'+canonical(value[key]) for key in sorted(value,key=lambda item:item.encode('utf-16-be',errors='surrogatepass')))+'}'
    raise ValueError('Invalid recovery value')


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def valid_id(value):return isinstance(value,str) and bool(ID.fullmatch(value)) and '..' not in value


def validate_binding(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise ValueError('Invalid recovery binding')
    for key,item in value.items():
        if key.endswith('_sha'):
            if not isinstance(item,str) or not HASH.fullmatch(item):raise ValueError('Invalid recovery binding')
        elif not valid_id(item):raise ValueError('Invalid recovery binding')
    return value

