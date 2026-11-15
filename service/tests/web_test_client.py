"""Flask 风格测试客户端包装（基于 Starlette TestClient）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Resp:
    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers
        self.data = resp.content
        self.content = resp.content
        self.text = resp.text
        self.content_type = resp.headers.get("content-type", "")

    def get_data(self, as_text: bool = False) -> bytes | str:
        if as_text:
            return self.text
        return self.data

    @property
    def is_json(self) -> bool:
        ctype = (self.headers.get("content-type") or "").lower()
        return "application/json" in ctype

    def get_json(self) -> Any:
        return self._resp.json()

    def json(self) -> Any:
        return self._resp.json()


def _normalize_kwargs(kwargs: dict) -> dict:
    """Flask ``content_type=`` → httpx ``headers``。"""
    out = dict(kwargs)
    ctype = out.pop("content_type", None)
    # multipart 由 files= 自动设置 Content-Type；勿手动强制
    if ctype and "multipart/form-data" not in str(ctype).lower():
        headers = dict(out.get("headers") or {})
        headers.setdefault("Content-Type", ctype)
        out["headers"] = headers
    return out


def _split_flask_multipart(data) -> tuple[dict | None, dict | None]:
    """Flask ``data={field: (BytesIO, filename)}`` → httpx ``data`` + ``files``。"""
    if not isinstance(data, dict):
        return data, None
    form: dict = {}
    files: dict = {}
    for key, val in data.items():
        if isinstance(val, (tuple, list)) and val and hasattr(val[0], "read"):
            fileobj = val[0]
            filename = val[1] if len(val) > 1 else key
            content_type = val[2] if len(val) > 2 else None
            if content_type:
                files[key] = (filename, fileobj, content_type)
            else:
                files[key] = (filename, fileobj)
        else:
            form[key] = val
    return (form or None), (files or None)


class WebTestClient:
    """浏览器页面回归客户端：加载静态壳、取 JSON、执行真实前端渲染器。

    原生 HTTP/权限契约测试使用 FastAPI TestClient；这里保留已有 UI 内容断言。
    """

    def __init__(self, app: FastAPI):
        self._client = TestClient(app)

    def get(self, url: str, *, follow_redirects: bool = False, **kwargs) -> _Resp:
        response = self._client.get(url, follow_redirects=follow_redirects, **_normalize_kwargs(kwargs))
        if response.headers.get("X-Deskbot-Page") != "static":
            return _Resp(response)
        page_url = httpx.URL(str(response.request.url))
        data_url = "/api/pages" + page_url.path
        if page_url.query:
            data_url += "?" + page_url.query.decode()
        data = self._client.get(data_url, headers={"Accept": "application/json"}, follow_redirects=False)
        payload = data.json()
        redirect = payload.get("redirect")
        if data.status_code == 401:
            redirect = "/login?next=" + page_url.path
        elif data.status_code == 403:
            redirect = "/home"
        if redirect:
            return _Resp(httpx.Response(302, headers={"Location": redirect}, request=data.request))
        if not data.is_success or "template" not in payload:
            return _Resp(data)
        shared = Path(__file__).resolve().parents[1] / "src/deskbot_server/public/shared"
        program = """const fs=require('fs');const root=process.argv[1];
const renderer=require(root+'/page-renderer.js');
const views=JSON.parse(fs.readFileSync(root+'/views.json','utf8'));
process.stdout.write(renderer.render(views,JSON.parse(fs.readFileSync(0,'utf8'))));"""
        rendered = subprocess.run(
            ["node", "-e", program, str(shared)], input=json.dumps(payload), text=True, capture_output=True, check=True
        )
        return _Resp(
            httpx.Response(
                data.status_code,
                content=rendered.stdout,
                headers={**dict(data.headers), "content-type": "text/html"},
                request=data.request,
            )
        )

    def post(self, url: str, *, data=None, json=None, follow_redirects: bool = False, **kwargs) -> _Resp:
        kw = _normalize_kwargs(kwargs)
        form, files = _split_flask_multipart(data)
        if files is not None:
            return _Resp(
                self._client.post(url, data=form, files=files, json=json, follow_redirects=follow_redirects, **kw)
            )
        return _Resp(self._client.post(url, data=data, json=json, follow_redirects=follow_redirects, **kw))

    def put(self, url: str, *, data=None, json=None, follow_redirects: bool = False, **kwargs) -> _Resp:
        return _Resp(
            self._client.put(url, data=data, json=json, follow_redirects=follow_redirects, **_normalize_kwargs(kwargs))
        )

    def patch(self, url: str, *, data=None, json=None, follow_redirects: bool = False, **kwargs) -> _Resp:
        return _Resp(
            self._client.patch(
                url, data=data, json=json, follow_redirects=follow_redirects, **_normalize_kwargs(kwargs)
            )
        )

    def delete(self, url: str, *, follow_redirects: bool = False, **kwargs) -> _Resp:
        return _Resp(self._client.delete(url, follow_redirects=follow_redirects, **_normalize_kwargs(kwargs)))
