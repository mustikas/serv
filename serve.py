"""Serve the current directory as a LAN mailbox.

  python serve.py

Listens on port 8009. Clients GET files, then DELETE with a matching hash.
"""
import hashlib
import http.server
import json
import os
import socket
import socketserver
import urllib.parse

PORT = 8009
DIRECTORY = os.path.abspath('.')  # Serve files from current directory
HASH_CHUNK_SIZE = 4 * 1024 * 1024
EXCLUDED = {'download.py', 'serve.py', '.git'}
# path -> (mtime_ns, size, hexdigest) computed while the file was fully sent
HASH_CACHE = {}


def new_hasher():
    return hashlib.blake2b(digest_size=16)


def hash_file(filepath):
    digest = new_hasher()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_in_directory(root, relpath):
    """Absolute path if relpath stays inside root, else None."""
    if not relpath or os.path.isabs(relpath):
        return None
    normalized = os.path.normpath(relpath)
    if normalized in ('.', os.pardir) or normalized.startswith(os.pardir + os.sep):
        return None
    if any(part in EXCLUDED for part in normalized.split(os.sep)):
        return None
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, normalized))
    try:
        common = os.path.commonpath([root_real, full])
    except ValueError:
        return None
    if common != root_real:
        return None
    return full


def file_identity(filepath):
    st = os.stat(filepath)
    return st.st_mtime_ns, st.st_size


def local_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('1.1.1.1', 80))
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/filelist.json':
            # Return JSON list of files (non-recursive)
            try:
                files = [f for f in os.listdir(DIRECTORY)
                         if os.path.isfile(os.path.join(DIRECTORY, f)) and f not in EXCLUDED]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(files).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'Error listing files: {e}'.encode('utf-8'))
            return

        relpath = urllib.parse.unquote(parsed_path.path.lstrip('/'))
        full_path = resolve_in_directory(DIRECTORY, relpath)
        if full_path is None or not os.path.isfile(full_path):
            self.send_error(404)
            return

        try:
            identity = file_identity(full_path)
        except OSError:
            self.send_error(404)
            return

        file_size = identity[1]
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(full_path))
        self.send_header('Content-Length', str(file_size))
        self.end_headers()

        digest = new_hasher()
        sent = 0
        try:
            with open(full_path, 'rb') as f:
                while True:
                    chunk = f.read(HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    self.wfile.write(chunk)
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

        if sent == file_size:
            HASH_CACHE[full_path] = (*identity, digest.hexdigest())

    def do_DELETE(self):
        parsed_path = urllib.parse.urlparse(self.path)
        filepath = urllib.parse.unquote(parsed_path.path.lstrip('/'))
        full_path = resolve_in_directory(DIRECTORY, filepath)

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        if full_path is None:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Path escapes directory')
            return

        try:
            data = json.loads(body)
            client_hash = data.get('hash')
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Invalid JSON')
            return

        if not os.path.isfile(full_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'File not found')
            return

        try:
            identity = file_identity(full_path)
            cached = HASH_CACHE.get(full_path)
            if cached and cached[0] == identity[0] and cached[1] == identity[1]:
                file_hash = cached[2]
            else:
                file_hash = hash_file(full_path)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'Error reading file: {e}'.encode())
            return

        if client_hash != file_hash:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'Hash mismatch, delete forbidden')
            return

        try:
            os.remove(full_path)
            HASH_CACHE.pop(full_path, None)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'File deleted')
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'Error deleting file: {e}'.encode())

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    os.chdir(DIRECTORY)
    with socketserver.TCPServer(("", PORT), CustomHandler) as httpd:
        print(f"Serving HTTP on {local_ipv4()}:{PORT} (directory: {DIRECTORY})")
        httpd.serve_forever()
