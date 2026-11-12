"""在解析 JSON 之前限制请求体大小（含分块传输），防止超大请求占满内存。"""

import asyncio

from fastapi.responses import JSONResponse


class BodyLimit:
    def __init__(self, app, max_bytes: int, receive_timeout: float = 10, prefix: str | None = None):
        self.app, self.max_bytes = app, max_bytes
        self.receive_timeout, self.prefix = receive_timeout, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (
            self.prefix is not None and not scope["path"].startswith(self.prefix)
        ):
            return await self.app(scope, receive, send)
        body = bytearray()
        deadline = asyncio.get_running_loop().time() + self.receive_timeout
        while True:
            try:
                message = await asyncio.wait_for(
                    receive(), max(0, deadline - asyncio.get_running_loop().time())
                )
            except TimeoutError:
                return await JSONResponse({"error": "body_timeout"}, status_code=408)(
                    scope, receive, send
                )
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                return await JSONResponse(
                    {"error": "body_limit", "message": "request body too large"}, status_code=413
                )(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)
