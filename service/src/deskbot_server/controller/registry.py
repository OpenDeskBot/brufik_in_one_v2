"""显式模块注册；重复路由和漏声明均在监听前拒绝。"""

from __future__ import annotations

from fastapi.routing import APIRoute, APIWebSocketRoute


def register_routers(app, modules):
    known = getattr(app.state, "registered_routes", {})
    pending = []
    for module in modules:
        router = getattr(module, "router", module)
        for route in router.routes:
            if not isinstance(route, (APIRoute, APIWebSocketRoute)):
                continue
            endpoint = route.endpoint
            identity = f"{endpoint.__module__}.{endpoint.__qualname__}"
            if getattr(endpoint, "__access_kind__", None) is None:
                raise ValueError(f"missing access declaration: {route.path} ({identity})")
            methods = route.methods if isinstance(route, APIRoute) else {"WEBSOCKET"}
            for method in methods:
                key = (method, route.path)
                if key in known:
                    raise ValueError(f"duplicate route {method} {route.path}: {known[key]} / {identity}")
                known[key] = identity
        pending.append(router)
    app.state.registered_routes = known
    for router in pending:
        app.include_router(router)
