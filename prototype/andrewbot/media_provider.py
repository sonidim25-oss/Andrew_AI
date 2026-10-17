"""OpenRouter dedicated speech and image APIs; no provider data in exceptions."""
import base64
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OpenRouterMedia:
    def __init__(self, api_key, opener=None):
        self._key = api_key
        self.opener = opener or build_opener(NoRedirect()).open

    def generate(self, kind, payload):
        if not self._key:
            raise ValueError('Media credentials unavailable')
        if kind == 'voice':
            endpoint = 'audio/speech'
            body = {'model': payload['model'], 'input': payload['text'],
                    'voice': payload['voice'], 'response_format': 'mp3'}
        elif kind == 'image':
            endpoint = 'images'
            body = {'model': payload['model'], 'prompt': payload['text'], 'n': 1,
                    'quality': 'low', 'aspect_ratio': '1:1'}
            if payload.get('reference_png'):
                body['input_references'] = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(payload['reference_png']).decode('ascii')}}]
        else:
            raise ValueError('Unsupported media kind')
        request = Request('https://openrouter.ai/api/v1/' + endpoint,
                          data=json.dumps(body).encode(), method='POST',
                          headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=120) as response:
                data = response.read(12 * 1024 * 1024 + 1)
                if len(data) > 12 * 1024 * 1024:
                    raise ValueError()
                if kind == 'voice':
                    mime = response.headers.get('Content-Type', '').split(';')[0]
                    if mime not in {'audio/mpeg', 'audio/mp3', 'application/octet-stream'}:
                        raise ValueError()
                    return data, 'audio/mpeg', None
                result = json.loads(data)
                if len(result['data']) != 1:
                    raise ValueError()
                item = result['data'][0]
                return (base64.b64decode(item['b64_json'], validate=True), item.get('media_type', 'image/png'),
                        (result.get('usage') or {}).get('cost'))
        except Exception:
            raise ValueError('Media provider outcome uncertain') from None


def avatar_jpeg(data):
    """Convert a validated PNG using a finite, protocol-restricted image decoder."""
    import subprocess
    try:
        result = subprocess.run(['/usr/bin/ffmpeg', '-v', 'error', '-protocol_whitelist', 'pipe',
                                 '-f', 'image2pipe', '-c:v', 'png', '-i', 'pipe:0', '-frames:v', '1',
                                 '-vf', 'scale=640:640', '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1'],
                                input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=10, check=True)
        if not 32 <= len(result.stdout) <= 2 * 1024 * 1024 or not result.stdout.startswith(b'\xff\xd8'):
            raise ValueError()
        return result.stdout
    except Exception:
        raise ValueError('Avatar conversion unavailable') from None
