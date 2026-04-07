#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import os
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WATCH_EXTS = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
RELOAD_SNIPPET = """
<script>
(function () {
  try {
    const es = new EventSource('/__reload');
    es.onmessage = () => window.location.reload();
  } catch (err) {
    console.warn('Live reload unavailable', err);
  }
})();
</script>
""".strip()


def walk_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in WATCH_EXTS:
            yield path


class ReloadState:
    def __init__(self, root: Path):
        self.root = root
        self.clients = set()
        self.lock = threading.Lock()
        self.snapshot = self._snapshot()

    def _snapshot(self):
        return {
            str(path.relative_to(self.root)): path.stat().st_mtime_ns
            for path in walk_files(self.root)
        }

    def poll(self):
        while True:
            current = self._snapshot()
            if current != self.snapshot:
                self.snapshot = current
                self.broadcast("reload")
            time.sleep(0.75)

    def broadcast(self, message: str):
        payload = f"data: {message}\n\n".encode("utf-8")
        with self.lock:
            dead = []
            for handler in list(self.clients):
                try:
                    handler.wfile.write(payload)
                    handler.wfile.flush()
                except Exception:
                    dead.append(handler)
            for handler in dead:
                self.clients.discard(handler)


class PreviewHandler(SimpleHTTPRequestHandler):
    server_version = "PriceListPreview/1.0"

    def translate_path(self, path: str) -> str:
        rel = path.split("?", 1)[0].split("#", 1)[0]
        if rel in ("", "/"):
            rel = "/index.html"
        full = (ROOT / rel.lstrip("/")).resolve()
        if ROOT in full.parents or full == ROOT or str(full).startswith(str(ROOT)):
            return str(full)
        return str(ROOT / "index.html")

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__reload":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.server.reload_state.clients.add(self)
            try:
                while True:
                    time.sleep(1)
                    if self.wfile.closed:
                        break
            finally:
                self.server.reload_state.clients.discard(self)
            return
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        mime, _ = mimetypes.guess_type(str(path))
        if path.suffix.lower() == ".html":
            data = path.read_text(encoding="utf-8")
            if "</body>" in data:
                data = data.replace("</body>", f"{RELOAD_SNIPPET}</body>", 1)
            else:
                data += RELOAD_SNIPPET
            encoded = data.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            return _MemoryFile(encoded)

        encoded = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return _MemoryFile(encoded)


class _MemoryFile:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._offset
        start = self._offset
        end = min(len(self._data), start + size)
        self._offset = end
        return self._data[start:end]

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description="Local live-reload preview for the pricing landing page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("application/javascript", ".js")

    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    server.reload_state = ReloadState(ROOT)

    watcher = threading.Thread(target=server.reload_state.poll, daemon=True)
    watcher.start()

    print(f"Live preview running at http://{args.host}:{args.port}/")
    print(f"Serving from {ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
