"""FastAPI 入口。

端口 5273（用户指定，127.0.0.1 其他端口都被占了；5273–5276 实测空闲）。

启动：
    cd adsdesk/api && python3 -m uvicorn main:app --host 127.0.0.1 --port 5273 --reload
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent          # adsdesk/api
ADS_DESK = HERE.parent                          # adsdesk  （让 `api` 这个包能 import）
ROOT = HERE.parents[1]                          # 项目根
for _p in (str(ADS_DESK), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.db import WriteDenied  # noqa: E402
from api.routes import agents, approve, chat, cockpit, memorial, meta  # noqa: E402

app = FastAPI(
    title="广告工作台",
    version="0.1.0",
    description="审批台 + 驾驶舱。只读事实与规则输出，只写审批留痕，绝不写广告后台。",
)

# vite dev 在 5274，生产同源不需要；两个都放行以免调试时踩 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5273",
        "http://127.0.0.1:5274",
        "http://localhost:5274",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(WriteDenied)
async def _write_denied(request: Request, exc: WriteDenied):
    """越界写入统一 403，并说清是哪张表/哪一列，方便前端提示而不是静默失败。"""
    return JSONResponse(status_code=403, content={"ok": False, "detail": str(exc)})


app.include_router(meta.router, prefix="/api/meta", tags=["meta"])
app.include_router(memorial.router, prefix="/api/memorial", tags=["memorial"])
app.include_router(approve.router, prefix="/api/approve", tags=["approve"])
app.include_router(cockpit.router, prefix="/api/cockpit", tags=["cockpit"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])


@app.get("/api/health")
def health():
    from api.db import DB_PATH
    return {"ok": True, "db": str(DB_PATH), "db_exists": DB_PATH.exists()}


# 前端构建产物（npm run build 后落在 adsdesk/web/dist）
DIST = ROOT / "adsdesk" / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(DIST / "index.html")