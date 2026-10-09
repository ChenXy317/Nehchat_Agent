"""
FastAPI 服务入口 — Nehchat Agent。

负责初始化中间件、注册路由并托管前端静态文件。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import FRONTEND_DIR, ALLOWED_ORIGINS, WEB_USER, WEB_PASSWORD
from auth import BasicAuthMiddleware
from accounts import AccountManager
from clients import AIClient
from session_manager import SlotManager
from state import init as init_state
from routes.slots import router as slots_router
from routes.chat import router as chat_router
from routes.models import router as models_router
from routes.backgrounds import router as backgrounds_router, file_router as backgrounds_file_router
from routes.catalog import router as catalog_router
from routes.auth import router as auth_router
from routes.admin import router as admin_router
from routes.uploads import router as uploads_router

# ── Logging ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ai_client = None
    slot_mgr = None
    account_mgr = None
    try:
        ai_client = AIClient()
        slot_mgr = SlotManager()
        account_mgr = AccountManager(slot_mgr.pool)
        init_state(ai_client, slot_mgr, account_mgr)
        try:
            from attachments import cleanup_stale
            cleanup_stale()
        except Exception:
            logger.warning("清理临时附件失败", exc_info=True)
        logger.info("服务初始化完成")
    except RuntimeError as e:
        logger.critical(f"服务启动失败: {e}")
        raise
    except Exception as e:
        logger.critical(f"服务初始化时发生意外错误: {e}", exc_info=True)
        raise
    yield
    # ── 关闭清理 ──
    try:
        if ai_client is not None:
            await ai_client.close()
    except Exception:
        logger.warning("关闭 AIClient 时出错", exc_info=True)
    try:
        if slot_mgr is not None:
            slot_mgr.close()
    except Exception:
        logger.warning("关闭数据库连接池时出错", exc_info=True)
    logger.info("服务已关闭")


app = FastAPI(title="Nehchat Agent - 存档版", lifespan=lifespan)

# ── 可选访问鉴权（CORS 之外层：先注册则位于 CORS 内层） ──
if WEB_USER and WEB_PASSWORD:
    app.add_middleware(BasicAuthMiddleware, username=WEB_USER, password=WEB_PASSWORD)
    logger.info("已启用访问鉴权（HTTP Basic）")
else:
    logger.info("未设置 WEB_USER/WEB_PASSWORD，运行于无鉴权模式")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register routers ──

app.include_router(slots_router)
app.include_router(chat_router)
app.include_router(models_router)
app.include_router(catalog_router)
app.include_router(backgrounds_router)
app.include_router(backgrounds_file_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(uploads_router)


# ── 全局异常处理器（返回结构化错误） ──


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": "error", "message": str(exc.detail), "detail": ""},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "服务器内部错误",
            "detail": "",
        },
    )


# ── Serve frontend static files ──
# 只托管 Vite 生产构建产物，避免把源码或 node_modules 暴露出去
dist_dir = FRONTEND_DIR / "dist"
if dist_dir.is_dir() and any(dist_dir.iterdir()):
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")
    logger.info(f"已挂载前端静态文件: {dist_dir}")
else:
    logger.error(
        "未找到 frontend/dist，请先在 frontend 目录执行 npm run build；"
        "开发时请使用 Vite（默认 5173）"
    )


if __name__ == "__main__":
    import uvicorn

    # 生产环境默认关闭 reload，需要时通过 UVICORN_RELOAD=1 开启
    reload_enabled = os.environ.get("UVICORN_RELOAD", "").strip().lower() in (
        "1", "true", "yes",
    )
    host = os.environ.get("UVICORN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    uvicorn.run("main:app", host=host, port=8000, reload=reload_enabled)
