"""Demo proxy — 禁止注册，其余全部转发到生产站。"""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

UPSTREAM = os.getenv("UPSTREAM_URL", "https://astock.fly.dev")

BLOCKED_PATHS = {"/api/v1/auth/register"}

app = FastAPI(title="astock-demo-proxy")
client: httpx.AsyncClient | None = None


@app.get("/health")
async def health():
    return {"status": "ok", "app": "astock-demo-proxy"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    yield
    await client.aclose()


app.router.lifespan_context = lifespan


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(request: Request, path: str):
    if request.method == "OPTIONS":
        return Response(status_code=204)

    # 禁止注册
    if request.url.path.rstrip("/") in BLOCKED_PATHS:
        return JSONResponse(
            status_code=403,
            content={"code": 403, "message": "演示环境不支持注册，请联系管理员", "data": None},
        )

    # 构建上游请求
    target_url = f"{UPSTREAM}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "transfer-encoding", "accept-encoding")}
    headers["x-forwarded-for"] = request.client.host if request.client else "unknown"

    body = await request.body()

    try:
        resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={
                k: v for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "content-encoding")
            },
        )
    except httpx.RequestError as e:
        return JSONResponse(
            status_code=502,
            content={"code": 502, "message": f"后端不可用: {e}", "data": None},
        )
