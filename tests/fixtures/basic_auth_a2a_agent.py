"""The reference A2A agent behind basic authentication.

A test fixture for credential handling, not an independent agent. It
answers only when the request carries argv[2]:argv[3] as basic auth, and
everything else gets 401.
"""
import base64
import sys

import uvicorn

from nandatown.a2a_adapter import build_a2a_app

port, user, password = int(sys.argv[1]), sys.argv[2], sys.argv[3]
inner = build_a2a_app(f"http://127.0.0.1:{port}")
expected = b"Basic " + base64.b64encode(f"{user}:{password}".encode())


async def app(scope, receive, send):
    if scope["type"] == "http" and dict(scope.get("headers") or []).get(
            b"authorization") != expected:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})
        return
    await inner(scope, receive, send)


uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
