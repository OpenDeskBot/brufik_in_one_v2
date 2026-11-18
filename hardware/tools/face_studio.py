#!/usr/bin/env python3
"""Local expression editor: preview, validate, save JSON and generate firmware assets."""
from __future__ import annotations
import argparse
import hashlib
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from face_assets import SOURCE, OUTPUT, ROOT, compile_editor_document, generate, write_if_changed

STATIC = Path(__file__).with_suffix('')


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def make_handler(source, output, token):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, data, content_type='application/json; charset=utf-8'):
            if isinstance(data, str):
                data = data.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/data.js':
                raw = source.read_bytes()
                doc = json.loads(raw)
                bundle = compile_editor_document(doc)
                config = dict(token=token, revision=revision(raw), version=bundle['version'])
                self.reply(200, 'const FACE_REVISED='+json.dumps(doc,ensure_ascii=False)+
                           '; const FACE_ORIGINAL=structuredClone(FACE_REVISED); const STUDIO='+
                           json.dumps(config)+';', 'text/javascript; charset=utf-8')
            elif path == '/revised.json':
                self.reply(200, source.read_bytes())
            elif path in ('/', '/index.html', '/app.js', '/style.css'):
                name = 'index.html' if path == '/' else path[1:]
                mime = {'html':'text/html','js':'text/javascript','css':'text/css'}[name.rsplit('.',1)[1]]
                self.reply(200, (STATIC/name).read_bytes(), mime+'; charset=utf-8')
            else:
                self.reply(404, '{}')

        def do_POST(self):
            # Loopback-only server, per-session secret and same-origin write protection.
            expected_origin = f'http://127.0.0.1:{self.server.server_port}'
            if (self.path != '/save' or self.headers.get('X-Studio-Token') != token or
                    self.headers.get('Origin') != expected_origin):
                self.reply(403, json.dumps({'error':'仅允许当前本地编辑页保存'}))
                return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 2_000_000:
                    raise ValueError('表情文件大小需在 2 MB 以内')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('保存内容必须是对象')
                doc = body.get('document')
                bundle = compile_editor_document(doc)
                raw = source.read_bytes()
                if body.get('revision') != revision(raw):
                    self.reply(409, json.dumps({'error':'源文件已被其他操作修改，请先下载当前草稿，再刷新页面合并。'}))
                    return
                # Keep the full source, including phonemes; backups survive repeated edits.
                backup = ROOT/'hardware/.pio/face-backups'/f'{revision(raw)}.json'
                if not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(raw)
                write_if_changed(source, json.dumps(doc,ensure_ascii=False,indent=2)+'\n')
                generate(source,output)
                self.reply(200, json.dumps(dict(version=bundle['version'],revision=revision(source.read_bytes()))))
            except (ValueError, TypeError, KeyError, OSError) as exc:
                self.reply(400, json.dumps({'error':str(exc)},ensure_ascii=False))
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,default=SOURCE)
    ap.add_argument('--output',type=Path,default=OUTPUT)
    ap.add_argument('--port',type=int,default=8975)
    args = ap.parse_args()
    generate(args.source,args.output)
    server = HTTPServer(('127.0.0.1',args.port),make_handler(args.source,args.output,secrets.token_urlsafe(32)))
    print(f'Expression Studio: http://127.0.0.1:{server.server_port}/',flush=True)
    print(f'Source: {args.source}\nSave also generates firmware resources. Ctrl+C to stop.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
