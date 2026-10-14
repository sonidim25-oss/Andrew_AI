"""Controller-only private browser RPC. Account values never enter model tools."""
import json
import socket
import struct
from .browser_protocol import MAX_WIRE, validate_action
from .runner_client import _read_exactly

class BrowserClient:
    def __init__(self,socket_path='/run/andrew/browser.sock'):
        self.socket_path = socket_path

    def run(self,request):
        validate_action(request,private=True)
        data = json.dumps(request,separators=(',',':')).encode()
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
                conn.settimeout(50)
                conn.connect(self.socket_path)
                conn.sendall(struct.pack('!I',len(data))+data)
                size = struct.unpack('!I',_read_exactly(conn,4))[0]
                if not 0 < size <= MAX_WIRE: raise ValueError()
                result = json.loads(_read_exactly(conn,size))
                if not isinstance(result,dict) or result.get('status') not in {'ok','error'}: raise ValueError()
                return result
        except Exception:
            raise ValueError('private browser unavailable') from None
