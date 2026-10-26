"""
防盗找回 App 云端后端（独立部署版）
====================================
- 仅挂载 anti_theft router：/anti-theft/* 与 /ws/anti-theft
- 独立 SQLite: anti_theft.db（由 anti_theft.init_anti_theft_db 创建）
- 控制端 CTRL_TOKEN 鉴权，手机端注册 token 鉴权
- 原与 voicechat 共用一个 FastAPI 进程（D:/ai-voice/relay/main.py），
  2026-08-17 起拆分：本文件为防盗独立入口，voicechat 另起服务器部署。

部署（生产，宝塔进程守护管理器）：
    /www/wwwroot/antitheft-relay/relay/venv/bin/uvicorn main:app \
        --host 127.0.0.1 --port 8001 --workers 1
Nginx 反代 relay.example.com -> 127.0.0.1:8001（路径 /anti-theft/* 不变）。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from anti_theft import router as at_router, init_anti_theft_db, _migrate_normalize_sender_titles


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建库 + 目录（照片/相册）。原 on_event("startup") 写法在 Starlette>=1.0 已移除，改用 lifespan。
    init_anti_theft_db()
    # 一次性迁移：把 at_notify 表里尾部带 "(数字..消息)" 后缀的 title 清洗干净，避免同一好友被分裂成多个 sender。
    _migrate_normalize_sender_titles()
    yield


app = FastAPI(title="Anti-Theft Relay")
app.include_router(at_router)


if __name__ == "__main__":
    import uvicorn

    # 本地调试用；生产由 supervisor 拉起上面的命令
    uvicorn.run(app, host="127.0.0.1", port=8001, workers=1)
