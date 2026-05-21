#!/usr/bin/env python3
import os, json, uuid, mimetypes, base64, hashlib, time, secrets
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, urlencode
import urllib.request

SESSIONS = set()  # in-memory session tokens

PORT = int(os.environ.get('PORT', 4001))
BASE = os.path.dirname(os.path.abspath(__file__))
CONTENT_FILE = os.path.join(BASE, 'content.json')
UPLOAD_DIR   = os.path.join(BASE, 'assets', 'portfolio')

GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO', 'yzaian/zaian-me')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

CLOUDINARY_CLOUD = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_KEY   = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_SECRET= os.environ.get('CLOUDINARY_API_SECRET', '')

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

def supabase_get_content():
    """Read content from Supabase. Returns (data, error)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, 'Supabase not configured'
    try:
        url = f'{SUPABASE_URL}/rest/v1/site_content?id=eq.main&select=data'
        req = urllib.request.Request(url, headers={
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read())
            if rows:
                return rows[0]['data'], ''
            return None, 'No row found in site_content'
    except Exception as e:
        return None, f'Supabase read error: {e}'

def supabase_save_content(data):
    """Save content to Supabase. Returns (ok, error)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False, 'Supabase not configured'
    try:
        url = f'{SUPABASE_URL}/rest/v1/site_content?id=eq.main'
        body = json.dumps({'data': data}).encode()
        req = urllib.request.Request(url, data=body, method='PATCH', headers={
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'return=minimal',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, ''
    except Exception as e:
        return False, f'Supabase write error: {e}'

def cloudinary_upload(file_bytes, original_filename):
    if not CLOUDINARY_CLOUD:
        return None
    ts = str(int(time.time()))
    sig_str = f'timestamp={ts}{CLOUDINARY_SECRET}'
    signature = hashlib.sha1(sig_str.encode()).hexdigest()
    ext = os.path.splitext(original_filename)[1].lstrip('.') or 'png'
    mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                'gif': 'image/gif', 'webp': 'image/webp'}
    mime = mime_map.get(ext.lower(), 'image/png')
    b64 = base64.b64encode(file_bytes).decode()
    data_uri = f'data:{mime};base64,{b64}'
    post_data = urlencode({
        'file': data_uri,
        'api_key': CLOUDINARY_KEY,
        'timestamp': ts,
        'signature': signature,
    }).encode()
    try:
        url = f'https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/image/upload'
        req = urllib.request.Request(url, data=post_data)
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result.get('secure_url')
    except Exception as e:
        print(f'Cloudinary upload failed: {e}')
        return None

def check_auth(handler):
    if not ADMIN_PASSWORD:
        return True  # no password set — allow (local dev)
    token = handler.headers.get('X-Admin-Token', '')
    if token and token in SESSIONS:
        return True
    # Also check cookie (for GET requests)
    for part in handler.headers.get('Cookie', '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == 'admin_session' and value in SESSIONS:
            return True
    return False

def require_auth(handler):
    """Redirect browser to login page, or return 401 JSON for API calls."""
    is_api = handler.headers.get('X-Admin-Token') is not None or \
             handler.headers.get('Content-Type', '').startswith('application/json')
    if not is_api and handler.command == 'GET':
        handler.send_response(302)
        handler.send_header('Location', '/admin/login.html')
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    else:
        body = json.dumps({'error': 'unauthorized'}).encode()
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', len(body))
        handler.end_headers()
        handler.wfile.write(body)
    return False

def github_push(path, file_bytes, message):
    """Push a file to GitHub to make changes persistent. Returns (ok: bool, error: str)."""
    if not GITHUB_TOKEN:
        return False, 'GITHUB_TOKEN not configured on server'
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
    }
    # Get current SHA (needed to update existing file)
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read())['sha']
    except Exception:
        pass
    body = {'message': message, 'content': base64.b64encode(file_bytes).decode()}
    if sha:
        body['sha'] = sha
    try:
        req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method='PUT')
        urllib.request.urlopen(req)
        return True, ''
    except Exception as e:
        err = str(e)
        print(f'GitHub push failed: {err}')
        return False, err

def parse_multipart(rfile, content_type, content_length):
    boundary = None
    for part in content_type.split(';'):
        part = part.strip()
        if part.startswith('boundary='):
            boundary = part[9:].strip('"')
    if not boundary:
        return None, None
    raw = rfile.read(content_length)
    boundary_bytes = ('--' + boundary).encode()
    parts = raw.split(boundary_bytes)
    for p in parts:
        if b'filename=' not in p:
            continue
        header_end = p.find(b'\r\n\r\n')
        if header_end == -1:
            continue
        headers_raw = p[:header_end].decode('utf-8', errors='replace')
        body = p[header_end+4:]
        if body.endswith(b'\r\n'):
            body = body[:-2]
        filename = None
        for h in headers_raw.split('\r\n'):
            if 'filename=' in h:
                fname_part = [x for x in h.split(';') if 'filename=' in x]
                if fname_part:
                    filename = fname_part[0].split('=')[1].strip().strip('"')
        return filename, body
    return None, None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def log_message(self, fmt, *args):
        pass  # quiet

    def end_headers(self):
        # Prevent browser caching of HTML so deploys are instantly visible
        if self.path.endswith('.html') or self.path == '/' or self.path.endswith('/'):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/content':
            # Try Supabase first; fall back to local file if unavailable
            data, err = supabase_get_content()
            if data is None:
                try:
                    with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            else:
                # Cache to local file for fallback resilience
                try:
                    with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            self.send_json(data)
        elif parsed.path == '/api/health':
            # Verify persistence backend is working
            status = {'backend': 'supabase', 'github_writable': False, 'error': ''}
            if SUPABASE_URL and SUPABASE_KEY:
                data, err = supabase_get_content()
                if data is not None:
                    status['github_writable'] = True  # field name kept for dashboard compatibility
                else:
                    status['error'] = err or 'Supabase unreachable'
            else:
                status['error'] = 'SUPABASE_URL or SUPABASE_KEY env var missing on Render'
            self.send_json(status)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        ct = self.headers.get('Content-Type', '')
        cl = int(self.headers.get('Content-Length', 0))

        if parsed.path == '/api/login':
            body = json.loads(self.rfile.read(cl).decode('utf-8'))
            if ADMIN_PASSWORD and body.get('password') == ADMIN_PASSWORD:
                token = secrets.token_hex(32)
                SESSIONS.add(token)
                resp = json.dumps({'token': token}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(resp))
                self.send_header('Set-Cookie', f'admin_session={token}; Path=/; SameSite=Strict')
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.send_json({'error': 'invalid password'}, 401)
            return

        if parsed.path == '/api/logout':
            token = self.headers.get('X-Admin-Token', '')
            SESSIONS.discard(token)
            self.send_json({'ok': True})
            return

        if not check_auth(self):
            require_auth(self)
            return

        if parsed.path == '/api/content':
            body = self.rfile.read(cl)
            data = json.loads(body.decode('utf-8'))
            # Write to Supabase first (durable). Cache to local file as fallback.
            ok, err = supabase_save_content(data)
            try:
                with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            if ok:
                self.send_json({'ok': True, 'persisted': True})
            else:
                # Save succeeded locally, but NOT persisted to GitHub. Will be lost on redeploy.
                self.send_json({'ok': True, 'persisted': False, 'warning': err or 'GitHub push failed — changes will be lost on server restart'})

        elif parsed.path == '/api/upload':
            filename, file_bytes = parse_multipart(self.rfile, ct, cl)
            if not filename or file_bytes is None:
                self.send_json({'error': 'no file'}, 400)
                return
            cdn_url = cloudinary_upload(file_bytes, filename)
            if cdn_url:
                self.send_json({'path': cdn_url})
            else:
                ext = os.path.splitext(filename)[1] or '.png'
                safe_name = str(uuid.uuid4())[:8] + ext
                dest = os.path.join(UPLOAD_DIR, safe_name)
                with open(dest, 'wb') as f:
                    f.write(file_bytes)
                github_push(f'assets/portfolio/{safe_name}', file_bytes, 'Upload image via dashboard')
                self.send_json({'path': 'assets/portfolio/' + safe_name})

        elif parsed.path == '/api/upload-pdf':
            raw = self.rfile.read(cl)
            boundary = None
            for part in ct.split(';'):
                part = part.strip()
                if part.startswith('boundary='):
                    boundary = part[9:].strip('"')
            if not boundary:
                self.send_json({'error': 'bad request'}, 400); return
            boundary_bytes = ('--' + boundary).encode()
            parts = raw.split(boundary_bytes)
            pdf_bytes = None
            pdf_type = None
            for p in parts:
                if len(p) < 4: continue
                header_end = p.find(b'\r\n\r\n')
                if header_end == -1: continue
                headers_raw = p[:header_end].decode('utf-8', errors='replace')
                body = p[header_end+4:]
                if body.endswith(b'\r\n'): body = body[:-2]
                if 'filename=' in headers_raw:
                    pdf_bytes = body
                elif 'name="type"' in headers_raw:
                    pdf_type = body.decode('utf-8', errors='replace').strip()
            if pdf_bytes is None or pdf_type not in ('portfolio', 'cv'):
                self.send_json({'error': 'invalid'}, 400); return
            names = {'portfolio': 'yahia-zaian-portfolio.pdf', 'cv': 'yahia-zaian-cv.pdf'}
            dest = os.path.join(BASE, 'assets', names[pdf_type])
            with open(dest, 'wb') as f:
                f.write(pdf_bytes)
            github_push(f'assets/{names[pdf_type]}', pdf_bytes, 'Upload PDF via dashboard')
            self.send_json({'ok': True})

        else:
            self.send_json({'error': 'not found'}, 404)

def fetch_latest_content():
    """On startup, pull latest content from Supabase so the local cache is fresh."""
    data, err = supabase_get_content()
    if data is None:
        print(f'  Supabase sync skipped: {err}')
        return
    try:
        with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print('  Content synced from Supabase.')
    except Exception as e:
        print(f'  Failed to cache content locally: {e}')

if __name__ == '__main__':
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fetch_latest_content()
    print(f'  Dashboard: http://localhost:{PORT}/admin/')
    print(f'  Website:   http://localhost:{PORT}/')
    HTTPServer(('', PORT), Handler).serve_forever()
