"""Render Web Service: CureNow ダッシュボード (Basic Auth + Supabase Edge Function proxy)."""
import base64, os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).parent
USER = os.environ.get("DASHBOARD_USER", "curenow")
PASS = os.environ.get("DASHBOARD_PASS", "")
SUPABASE_REFRESH = os.environ.get(
    "SUPABASE_REFRESH_URL",
    "https://kxmhgmeiosbkrnaygobe.supabase.co/functions/v1/dashboard/refresh",
)
CRON_SECRET = os.environ.get("DASHBOARD_CRON_SECRET", "")
PORT = int(os.environ.get("PORT", "10000"))

MIME = {".html": "text/html; charset=utf-8", ".json": "application/json",
        ".jpg": "image/jpeg", ".png": "image/png", ".css": "text/css", ".js": "application/javascript"}


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _auth(self):
        if not PASS:
            return True  # auth disabled (dev mode)
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

    def _serve_file(self, rel):
        p = BASE / rel
        if not p.is_file() or BASE not in p.resolve().parents and p.resolve() != p:
            self.send_error(404); return
        ext = p.suffix.lower()
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth():
            return self._unauth()
        path = self.path.split("?")[0].lstrip("/")
        if path == "" or path == "index.html":
            return self._serve_file("index.html")
        if path == "data.json":
            return self._serve_file("data.json")
        if path.startswith("images/"):
            return self._serve_file(path)
        if path == "healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        self.send_error(404)

    def do_POST(self):
        if not self._auth():
            return self._unauth()
        path = self.path.split("?")[0].lstrip("/")
        if path == "refresh":
            # proxy to Supabase Edge Function with cron secret (bypass その Basic Auth)
            try:
                req = urllib.request.Request(
                    SUPABASE_REFRESH,
                    method="POST",
                    headers={"x-cron-secret": CRON_SECRET, "Content-Type": "application/json"},
                    data=b"{}",
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = r.read()
                # データを refresh 後、Supabase Storage から data.json + images を pull して local 更新
                # (Render disk は ephemeral なので毎回再 pull)
                self._pull_storage()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.end_headers()
                self.wfile.write(f"upstream {e.code}: {e.read().decode(errors='replace')[:500]}".encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(f"refresh error: {e}".encode())
            return
        self.send_error(404)

    def _pull_storage(self):
        """Supabase Storage public URL (via Edge Function) から data.json + images を pull"""
        base_url = SUPABASE_REFRESH.rsplit("/", 1)[0]  # .../dashboard
        # data.json
        try:
            req = urllib.request.Request(
                f"{base_url}/data.json",
                headers={"Authorization": "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                (BASE / "data.json").write_bytes(r.read())
        except Exception as e:
            print(f"[warn] pull data.json: {e}")


def main():
    print(f"[server] listening on 0.0.0.0:{PORT} (auth={'on' if PASS else 'OFF'})")
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
