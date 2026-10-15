#!/usr/bin/env python3
"""Real rootless proof that a supplementary group can authorize a socket.
Uses only temporary sockets and subordinate ownership, no root privilege.
A temporary named-group ACL distinguishes supplementary access from owner/primary access.
The test container's primary GID differs deliberately; production stays 1000:1000.
"""
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import json
import ctypes

class Handler(socketserver.BaseRequestHandler):
    def handle(self): pass

def main(image):
    with tempfile.TemporaryDirectory(prefix='andrew-group-proof-') as root:
        path=Path(root)/'proxy.sock'
        server=socketserver.UnixStreamServer(str(path),Handler)
        os.chmod(path,0o660)
        secondary=next(g for g in os.getgroups() if g!=os.getgid())
        acl=ctypes.CDLL('libacl.so.1',use_errno=True)
        acl.acl_from_text.argtypes=[ctypes.c_char_p]; acl.acl_from_text.restype=ctypes.c_void_p
        acl.acl_set_file.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_void_p]
        acl.acl_free.argtypes=[ctypes.c_void_p]
        value=acl.acl_from_text(f'u::rw-,g::---,g:{secondary}:rw-,m::rw-,o::---'.encode())
        try:
            assert value and acl.acl_set_file(os.fsencode(path),0x8000,value)==0
        finally: acl.acl_free(value)
        subprocess.run(['podman','unshare','chown','1:0',str(path)],check=True,stdout=subprocess.DEVNULL)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        args=['podman','--runtime=/usr/bin/crun','run','--rm','--network=none','--read-only',
              '--userns=keep-id:uid=1000,gid=1000','--user=1000:1001','--cap-drop=ALL',
              '--security-opt=no-new-privileges','--timeout=10','--log-driver=none','--pull=never',
              '--mount',f'type=bind,src={path},dst=/run/proxy.sock,ro=true','--entrypoint','python3']
        code="import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/proxy.sock'); s.close()"
        try:
            without=subprocess.run(args+[image,'-c',code],capture_output=True,timeout=15)
            with_groups=subprocess.run(args+['--group-add=keep-groups',image,'-c',code],capture_output=True,timeout=15)
            assert without.returncode!=0,'fixture must deny access without supplementary group: '+without.stdout.decode()
            assert with_groups.returncode==0,'supplementary group must authorize socket access'
            print(json.dumps({'without_supplementary_group':'denied','with_keep_groups':'passed'}))
        finally: server.shutdown(); server.server_close(); thread.join(2)

if __name__=='__main__': main(sys.argv[1])
