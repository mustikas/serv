#!/usr/bin/env python3
"""Pull files from a LAN mailbox and delete them after a matching hash.

  python download.py hp

Saves into from_hp/. Server: http://hp.lan:8009/
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

CHUNK_SIZE = 4 * 1024 * 1024
SERVER_PORT = 8009
BAR_WIDTH = 28
STATUS_HASHING = 'Hashing...'
STATUS_DELETING = ' Deleting...'
STATUS_DONE = ' Done.'
STATUS_FAILED = ' Failed.'
STATUS_WIDTH = max(
    len(STATUS_HASHING + STATUS_DELETING + STATUS_DONE),
    len(STATUS_HASHING + STATUS_DELETING + STATUS_FAILED),
)
STATUS_GAP = 2


def new_hasher():
    return hashlib.blake2b(digest_size=16)


def hash_file(filepath):
    digest = new_hasher()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def open_url(url, timeout, data=None, method=None, headers=None):
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    last_error = None
    for attempt in range(3):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            time.sleep(0.3 * (attempt + 1))
    raise last_error


def list_files(url):
    with open_url(url + 'filelist.json', timeout=30) as response:
        return json.loads(response.read().decode())


def fmt_size(n):
    m = n / 1048576
    if m >= 100:
        return f'{m:.0f}M'
    if m >= 10:
        return f'{m:.1f}M'
    if m >= 1:
        return f'{m:.2f}M'
    k = n / 1024
    if k >= 1:
        return f'{k:.1f}K'
    return f'{n}B'


def bar_glyphs():
    enc = (sys.stdout.encoding or '').lower().replace('-', '')
    if enc.startswith('utf') or enc == 'cp65001':
        return '█', '░'
    return '#', '-'


def left_column_width():
    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(20, cols - STATUS_WIDTH - STATUS_GAP)


def show_progress(done, total):
    filled_ch, empty_ch = bar_glyphs()
    if total:
        frac = min(done / total, 1.0)
        filled = int(BAR_WIDTH * frac)
        bar = filled_ch * filled + empty_ch * (BAR_WIDTH - filled)
        line = f'[{bar}] {100 * frac:3.0f}% {fmt_size(done)}/{fmt_size(total)}'
    else:
        line = f'{fmt_size(done)}'
    width = left_column_width()
    print('\r' + line[:width].ljust(width), end='', flush=True)


def download_file(url, dest_path):
    """Download to dest_path.part, then replace dest_path."""
    part_path = dest_path + '.part'
    try:
        with open_url(url, timeout=300) as response:
            total_size = int(response.headers.get('Content-Length', 0))
            done = 0
            with open(part_path, 'wb') as f:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    show_progress(done, total_size)
                show_progress(done, total_size)
                f.flush()
                os.fsync(f.fileno())
        os.replace(part_path, dest_path)
    except Exception:
        print()
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise


def delete_file_on_server(file_url, file_hash):
    data = json.dumps({'hash': file_hash}).encode()
    try:
        with open_url(
            file_url,
            timeout=30,
            data=data,
            method='DELETE',
            headers={'Content-Type': 'application/json'},
        ) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False


def parse_args():
    parser = argparse.ArgumentParser(description='Download files from a host.')
    parser.add_argument(
        'host',
        help='Host name used in URL and download dir',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    server_url = f'http://{args.host}.lan:{SERVER_PORT}/'
    download_dir = f'from_{args.host}'

    os.makedirs(download_dir, exist_ok=True)

    files = list_files(server_url)
    if not files:
        print("No files found on server.")
        return

    for filename in files:
        file_url = urljoin(server_url, filename)
        dest_path = os.path.join(download_dir, filename)
        print(filename)
        try:
            download_file(file_url, dest_path)
        except Exception as e:
            print(f'Failed to download: {e}')
            continue

        print(STATUS_HASHING, end='', flush=True)
        file_hash = hash_file(dest_path)
        print(STATUS_DELETING, end='', flush=True)
        if delete_file_on_server(file_url, file_hash):
            print(STATUS_DONE)
        else:
            print(STATUS_FAILED)


if __name__ == '__main__':
    main()
