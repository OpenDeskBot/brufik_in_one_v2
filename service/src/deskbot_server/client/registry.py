"""按配置复用无状态客户端；密钥只参与摘要，不进入日志或对外结果。"""

from __future__ import annotations

import hashlib
import inspect
from collections import OrderedDict
from threading import Lock

_clients = OrderedDict()
_lock = Lock()


def shared_client(factory, *args, **kwargs):
    digest = hashlib.sha256(repr((args, sorted(kwargs.items()))).encode()).hexdigest()
    key = (factory, digest)
    with _lock:
        if key not in _clients:
            _clients[key] = factory(*args, **kwargs)
            if len(_clients) > 128:
                _clients.popitem(last=False)
        _clients.move_to_end(key)
        return _clients[key]


async def aclose_clients():
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
    from deskbot_server.client.tts.doubao import aclose_pools

    await aclose_pools()
