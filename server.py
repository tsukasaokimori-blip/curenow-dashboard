"""Render Web Service: CureNow ダッシュボード.

- HTML は local (index.html、デザイン更新時はリポ再 push で反映)
- data.json + images は Supabase Storage (curenow-dashboard bucket) から都度 fetch
  (pg_cron + /refresh ボタン がそこを更新するので、ここから読むと常に最新)
- /refresh は Supabase Edge Function に proxy (Meta API → Storage 更新)
- in-memory cache 60秒で Supabase Storage 負荷軽減
"""
import base64, os, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).parent
USER = os.environ.get("DASHBOARD_USER", "curenow")
PASS = os.environ.get("DASHBOARD_PASS", "")
SUPABASE_REFRESH = os.environ.get(
    "SUPABASE_REFRESH_URL",
    "https://kxmhgmeiosbkrnaygobe.supabase.co/functions/v1/dashboard/refresh",
)
SUPABASE_PROJECT = os.environ.get("SUPABASE_PROJECT_REF", "kxmhgmeiosbkrnaygobe")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "curenow-dashboard")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
CRON_SECRET = os.environ.get("DASHBOARD_CRON_SECRET", "")
PORT = int(os.environ.get("PORT", "10000"))

CACHE_TTL = 60  # seconds
_cache = {}  # key -> (expires_at, data, content_type)

MIME_LOCAL = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript"}
MIME_REMOTE = {".json": "application/json", ".jpg": "image/jpeg", ".png": "image/png"}


def _storage_url(path):
    return f"https://{SUPABASE_PROJECT}.supabase.co/storage/v1/object/{SUPABASE_BUCKET}/{path}"


def _fetch_storage(path):
    """Supabase Storage から service_role auth で取得、60秒 cache。"""
    now = time.time()
    if path in _cache:
        exp, data, ct = _cache[path]
        if exp > now:
            return data, ct
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY not set")
    req = urllib.request.Request(
        _storage_url(path),
        headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        ct = r.headers.get("Content-Type") or "application/octet-stream"
    # Supabase Storage は HTML/JSON を text/plain にdowngradeするので、拡張子から推定し直す
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ct = MIME_REMOTE.get(ext, ct)
    _cache[path] = (now + CACHE_TTL, data, ct)
    return data, ct


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _auth(self):
        if not PASS:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, p = base64.b64decode(h[6:]).decode().split(":", 1)
            return u == USER and p == PASS
        except Exception:
            return False

    def _unauth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="CureNow Dashboard"')
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def _serve_local(self, rel):
        p = (BASE / rel).resolve()
        if not str(p).startswith(str(BASE.resolve())) or not p.is_file():
            self.send_error(404); return
        ext = p.suffix.lower()
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME_LOCAL.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def _serve_remote(self, path):
        try:
            data, ct = _fetch_storage(path)
        except urllib.error.HTTPError as e:
            self.send_response(e.code); self.end_headers()
            self.wfile.write(f"Storage {e.code}: {e.read()[:200].decode(errors='replace')}".encode())
            return
        except Exception as e:
            self.send_response(502); self.end_headers()
            self.wfile.write(f"Storage error: {e}".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth():
            return self._unauth()
        path = self.path.split("?")[0].lstrip("/")
        if path == "" or path == "index.html":
            return self._serve_local("index.html")
        if path == "data.json" or path.startswith("images/"):
            return self._serve_remote(path)
        if path == "healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        self.send_error(404)

    def do_POST(self):
        if not self._auth():
            return self._unauth()
        path = self.path.split("?")[0].lstrip("/")
        if path == "refresh":
            try:
                req = urllib.request.Request(
                    SUPABASE_REFRESH,
                    method="POST",
                    headers={"x-cron-secret": CRON_SECRET, "Content-Type": "application/json"},
                    data=b"{}",
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = r.read()
                # invalidate cache so next GET pulls fresh Storage contents
                _cache.clear()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                self.send_response(e.code); self.end_headers()
                self.wfile.write(f"upstream {e.code}: {e.read().decode(errors='replace')[:500]}".encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(f"refresh error: {e}".encode())
            return
        self.send_error(404)


def main():
    print(f"[server] listening on 0.0.0.0:{PORT} auth={'on' if PASS else 'OFF'} bucket={SUPABASE_BUCKET}")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
