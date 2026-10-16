#!/usr/bin/env python3
"""Developer proof with temporary Unix sockets and the invoking user's rootless Podman.
No privileged host firewall/production workspace claim. Runs a real 130-second job across the former 120-second deadline.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'prototype'))
from andrewbot.runner_service import RunnerService,RunnerSocketServer,WorkerConfig
from andrewbot.runner_client import RunnerClient
from andrewbot.browser_client import BrowserClient
from andrewbot.browser_service import BrowserServer,BrowserProcess
from andrewbot.egress_proxy import EgressProxy


def main(image):
    with tempfile.TemporaryDirectory(prefix='andrew-isolation-') as directory:
        root=Path(directory); projects=root/'projects'; projects.mkdir()
        ready=threading.Event(); loop=asyncio.new_event_loop(); errors=[]
        proxy_socket=str(root/'egress.sock')
        config=WorkerConfig(image,str(projects),proxy_socket)
        browser_process=BrowserProcess(config)
        async def serve():
            try:
                proxy=await asyncio.start_unix_server(EgressProxy().handle_request,path=proxy_socket)
                runner=await asyncio.start_unix_server(RunnerSocketServer(RunnerService(config)).handle,path=str(root/'runner.sock'))
                browser=await asyncio.start_unix_server(BrowserServer(browser_process).handle,path=str(root/'browser.sock'))
                ready.set()
                try: await asyncio.Event().wait()
                finally:
                    for server in (proxy,runner,browser): server.close(); await server.wait_closed()
            except BaseException as error:
                if not isinstance(error,asyncio.CancelledError): errors.append(type(error).__name__)
                ready.set()
        def thread_main():
            asyncio.set_event_loop(loop)
            task=loop.create_task(serve())
            try: loop.run_forever()
            finally:
                task.cancel(); loop.run_until_complete(asyncio.gather(task,return_exceptions=True)); loop.close()
        thread=threading.Thread(target=thread_main,daemon=True); thread.start(); ready.wait(10)
        try:
            if errors: raise RuntimeError('local harness startup failed')
            spec=importlib.util.spec_from_file_location('acceptance',Path(__file__).with_name('verify-runtime.py'))
            module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            module.runner=RunnerClient(str(root/'runner.sock'),140)
            module.BrowserClient=lambda: BrowserClient(str(root/'browser.sock'))
            module.main(strict_workspace=False)
        finally:
            browser_process.close(); loop.call_soon_threadsafe(loop.stop); thread.join(10)

if __name__=='__main__': main(sys.argv[1])
