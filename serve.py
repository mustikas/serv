"""Serve the current directory as a LAN mailbox.

  python serve.py

Listens on port 8009. Clients GET files, then DELETE with a matching hash.

wget https://raw.githubusercontent.com/mustikas/serv/main/serve.py
wget https://raw.githubusercontent.com/mustikas/serv/main/download.py
"""
import hashlib
import http.server
import json
import os
import socket
import socketserver
import time
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


def lan_hostname():
    name = socket.gethostname().strip()
    if not name:
        return None
    host = f'{name}.lan'
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    return host


class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def log_action(self, message):
        stamp = time.strftime('%H:%M:%S')
        print(f'{stamp}  {self.client_address[0]}  {message}', flush=True)

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
                self.log_action(f'list {len(files)} file(s)')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f'Error listing files: {e}'.encode('utf-8'))
                self.log_action(f'list failed: {e}')
            return

        relpath = urllib.parse.unquote(parsed_path.path.lstrip('/'))
        full_path = resolve_in_directory(DIRECTORY, relpath)
        if full_path is None or not os.path.isfile(full_path):
            self.log_action(f'GET {relpath!r} not found')
            self.send_error(404)
            return

        try:
            identity = file_identity(full_path)
        except OSError:
            self.log_action(f'GET {relpath!r} not found')
            self.send_error(404)
            return

        file_size = identity[1]
        self.log_action(f'sending {relpath} ({file_size} bytes)')
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
            self.log_action(f'send aborted {relpath} ({sent}/{file_size} bytes)')
            return

        if sent == file_size:
            HASH_CACHE[full_path] = (*identity, digest.hexdigest())
            self.log_action(f'sent {relpath}')
        else:
            self.log_action(f'send incomplete {relpath} ({sent}/{file_size} bytes)')

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
            self.log_action(f'DELETE {filepath!r} path escapes')
            return

        try:
            data = json.loads(body)
            client_hash = data.get('hash')
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Invalid JSON')
            self.log_action(f'DELETE {filepath!r} invalid JSON')
            return

        if not os.path.isfile(full_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'File not found')
            self.log_action(f'DELETE {filepath!r} not found')
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
            self.log_action(f'DELETE {filepath!r} hash failed: {e}')
            return

        if client_hash != file_hash:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'Hash mismatch, delete forbidden')
            self.log_action(f'DELETE {filepath!r} hash mismatch')
            return

        try:
            os.remove(full_path)
            HASH_CACHE.pop(full_path, None)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'File deleted')
            self.log_action(f'deleted {filepath}')
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'Error deleting file: {e}'.encode())
            self.log_action(f'DELETE {filepath!r} failed: {e}')

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    os.chdir(DIRECTORY)
    with socketserver.TCPServer(("", PORT), CustomHandler) as httpd:
        ip = local_ipv4()
        name = lan_hostname()
        if name:
            print(f"Serving HTTP on {name}:{PORT} and {ip}:{PORT} (directory: {DIRECTORY})")
        else:
            print(f"Serving HTTP on {ip}:{PORT} (directory: {DIRECTORY})")
        httpd.serve_forever()
