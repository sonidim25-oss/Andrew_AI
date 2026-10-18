"""Real local SDK fixture with private temporary state and loopback provider only."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'prototype'))
from andrewbot.appserver import AppServerClient
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.store import Store


class Fixture:
    def __init__(self, release, data_dir, provider=None, extra_environment=None):
        self.release, self.root = Path(release), Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider_calls = []
        fixture = self
        class FakeProvider:
            def complete(self, payload):
                fixture.provider_calls.append(payload)
                if provider is None:
                    raise AssertionError('Unexpected fixture model request')
                return provider(payload)
        self.store = Store(self.root / 'controller.sqlite3')
        self.model_gateway = ModelGateway(FakeProvider(), self.store, 'fixture-token')
        self.gateway = GatewayThread(make_server(self.model_gateway, port=0))
        self.gateway.start()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.url = f'ws://127.0.0.1:{self.port}/ws'
        self.environment = {**os.environ, 'HOME':str(self.root), 'LETTA_HOME':str(self.root / 'settings'),
            'LETTA_LOCAL_BACKEND_DIR':str(self.root / 'backend'), 'DO_NOT_TRACK':'1',
            **(extra_environment or {})}
        self.process, self.client = None, None
        try:
            self.restart()
        except BaseException:
            self.stop()
            raise

    def restart(self):
        self.kill()
        with (self.root / 'server.log').open('a') as log:
            self.process = subprocess.Popen(['node', str(self.release / 'letta.js'), 'server', '--backend', 'local',
                '--listen', f'ws://127.0.0.1:{self.port}', '--openai-api'], cwd=self.root,
                env=self.environment, stdout=log, stderr=log)
        deadline = time.monotonic() + 25
        while True:
            if self.process.poll() is not None:
                raise AssertionError('Fixture backend exited: ' + (self.root / 'server.log').read_text()[-1500:])
            try:
                self.client = AppServerClient(self.url, timeout=3)
                return
            except ConnectionRefusedError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Fixture startup timeout')
                time.sleep(.05)

    def provider_calls_for(self, effect_id):
        return sum(payload.get('_fixture_effect_id') == effect_id for payload in self.provider_calls)

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.client is not None:
            self.client.close()
            self.client = None

    def stop(self):
        self.kill()
        self.gateway.close()
        self.store.close()


def launch_release(release, data_dir, provider=None, extra_environment=None):
    return Fixture(release, data_dir, provider, extra_environment)
