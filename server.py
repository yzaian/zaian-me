#!/usr/bin/env python3
import os, json, uuid, mimetypes, base64
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

PORT = int(os.environ.get('PORT', 4001))
BASE = os.path.dirname(os.path.abspath(__file__))
CONTENT_FILE = os.path.join(BASE, 'content.json')
UPLOAD_DIR   = os.path.join(BASE, 'assets', 'portfolio')

GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO', 'yzaian/zaian-me')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

def check_auth(handler):
    """Returns True if the request has valid admin credentials."""
    if not ADMIN_PASSWORD:
        return True  # no password set — allow (local dev)
    auth = handler.headers.get('Authorization', '')
    if auth.startswith('Basic '):
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            _, pwd = decoded.split(':', 1)
            return pwd == ADMIN_PASSWORD
        except Exception:
            pass
    return False

def require_auth(handler):
    """Send 401 if not authenticated."""
    handler.send_response(401)
    handler.send_header('WWW-Authenticate', 'Basic realm="Admin"')
    handler.send_header('Content-Length', '0')
    handler.end_headers()
    return False

def github_push(path, file_bytes, message):
    """Push a file to GitHub to make changes persistent."""
    if not GITHUB_TOKEN:
        return
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
    except Exception as e:
        print(f'GitHub push failed: {e}')

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
            with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.send_json(data)
        elif parsed.path.startswith('/admin'):
            if not check_auth(self):
                require_auth(self)
                return
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        if not check_auth(self):
            require_auth(self)
            return
        parsed = urlparse(self.path)
        ct = self.headers.get('Content-Type', '')
        cl = int(self.headers.get('Content-Length', 0))

        if parsed.path == '/api/content':
            body = self.rfile.read(cl)
            data = json.loads(body.decode('utf-8'))
            with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            github_push('content.json', body, 'Update content via dashboard')
            self.send_json({'ok': True})

        elif parsed.path == '/api/upload':
            filename, file_bytes = parse_multipart(self.rfile, ct, cl)
            if not filename or file_bytes is None:
                self.send_json({'error': 'no file'}, 400)
                return
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

if __name__ == '__main__':
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    print(f'  Dashboard: http://localhost:{PORT}/admin/')
    print(f'  Website:   http://localhost:{PORT}/')
    HTTPServer(('', PORT), Handler).serve_forever()
