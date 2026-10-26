"""
防盗找回 App 云端模块（纯 WorkManager 轮询唤醒）
- 独立 SQLite: anti_theft.db（与 voicechat 的 ai_voice.db 物理隔离）
- 路由经 APIRouter 挂载到主 app 的 /anti-theft/* 与 /ws/anti-theft
- 多设备按 device_id 隔离；控制端用 CTRL_TOKEN 鉴权，手机用注册 token 鉴权

唤醒模型（纯轮询）：
  手机平时 WorkManager 每 15 分钟 GET /anti-theft/poll?device_id=&token= 拉指令；
  控制端 POST /anti-theft/command 写队列（按 device_id）；
  手机拉到 activate/photo/lock 等指令后执行并上报。
  /ws/anti-theft 为激活后可选实时通道：手机连上后，command 优先经 WS 直推。
"""
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List

# ------------------------- 配置 -------------------------
_BASE = os.path.dirname(os.path.abspath(__file__))
AT_DB_PATH = os.path.join(_BASE, "anti_theft.db")
AT_PHOTOS_DIR = os.path.join(_BASE, "anti_theft_photos")
AT_ALBUMS_DIR = os.path.join(_BASE, "anti_theft_albums")
AT_DB_LOCK = threading.Lock()

# 上传文件大小上限（20MB：前摄照片一般 <5MB，相册缩略图更小；防恶意超大文件撑爆磁盘）
_MAX_UPLOAD_SIZE = 20 * 1024 * 1024

# ------------------------- 启动期自检：Pillow 是否可用 -------------------------
# 宝塔进程守护管理器常用 venv python，跟系统 python3 不是同一个解释器。
# 启动时主动打一行：PIL available / version，让"装错解释器"的问题秒暴露，
# 而不是要等到用户报"图片没旋转"再翻 logcat + daemon 日志去对时间戳。
try:
    from PIL import Image as _PIL_Image  # noqa: F401
    _PIL_VERSION = _PIL_Image.__version__
    print(f"[anti_theft] startup: PIL available, version={_PIL_VERSION}", flush=True)
except Exception as _e:
    _PIL_VERSION = None
    print(f"[anti_theft] startup: PIL NOT available, photo rotation DISABLED: {_e}", flush=True)

# 常见系统/厂商包名前缀（HyperOS/MIUI/高通/原生等未打 FLAG_SYSTEM 的预装组件）
_SYSTEM_PKG_PREFIXES = (
    "android",
    "com.android.",
    "com.google.android.",
    "com.qualcomm.",
    "com.qti.",
    "com.miui.",
    "com.mi.",
    "com.xiaomi.",
    "com.bsp.",
    "com.mobiletools.",
    "com.huawei.",
    "com.samsung.",
    "com.lge.",
    "com.oneplus.",
    "com.coloros.",
    "com.oppo.",
    "com.vivo.",
    "com.realme.",
    "com.heytap.",
    "com.meizu.",
    "com.smartisan.",
    "com.lenovo.",
    "com.zui.",
    "com.motorola.",
    "com.sony.",
    "com.asus.",
    "com.mediatek.",
    "com.sec.",
    "com.sonyericsson.",
    "com.amazon.",
)


def _is_system_pkg(pkg: str) -> bool:
    """按包名判断是否为系统应用（兜底，与安卓端规则一致）。"""
    if not pkg:
        return True
    for p in _SYSTEM_PKG_PREFIXES:
        if pkg == p or pkg.startswith(p):
            return True
    return False


# 高德 Web 服务 Key（仅服务端使用，绝不下发手机端）。逆地理主源；失败回退 Nominatim。
# 从环境变量读取（防代码仓库泄露 key 被盗用产生费用）。
# 部署时必须设置：宝塔 supervisor 环境变量加 AMAP_WEB_KEY=xxx，未配置则回退 Nominatim（较慢）。
AMAP_WEB_KEY = os.environ.get("AMAP_WEB_KEY", "")

# 控制端鉴权 token：环境变量 CTRL_TOKEN > 项目根 .ctrl_token 首行
# 未配置 -> 控制端所有读/写接口拒绝（安全默认，绝不暴露位置给他人）
def _load_ctrl_token():
    t = os.environ.get("CTRL_TOKEN")
    if t:
        return t.strip()
    try:
        with open(os.path.join(_BASE, ".ctrl_token"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


CTRL_TOKEN = _load_ctrl_token()

# 通知监听白名单：只接收微信 / QQ / 短信，其余包名服务端直接丢弃（防御兜底，
# 即使旧版 APK 上报全量通知也只落库这三家）。短信包名因 ROM 而异。
_ALLOWED_NOTIFY_PKGS = {
    "com.tencent.mm",                     # 微信
    "com.tencent.mobileqq",               # QQ
    "com.tencent.qqlite",               # QQ 轻聊
    "com.android.mms",                    # 短信 (MIUI/HyperOS)
    "com.google.android.apps.messaging",  # 短信 (Google Messages)
    "com.samsung.android.messaging",      # 短信 (三星)
}

router = APIRouter()

# 清洗 sender title：剥掉尾部 "（4条新消息）/（4条消息）/（12）" 这类 QQ 风格的未读计数括号，
# 否则同一好友会被拆成多个气泡（如「[特别关心]Zou(4条新消息)」「[特别关心]Zou(3条新消息)」... 算成 4 个 sender）。
_AT_SENDER_PAREN_RE = re.compile(r"[\s\u3000]*[\(（][^\)）]*\d+[^\)）]*[\)）][\s\u3000]*$")

def _normalize_sender_title(t):
    if not t:
        return t
    new = _AT_SENDER_PAREN_RE.sub("", str(t)).strip()
    return new or str(t)

def _migrate_normalize_sender_titles():
    """启动期一次性迁移：把 at_notify 表里尾部带 '(数字..消息)' / '（数字..消息）' 后缀的 title 全部清洗掉，
    这样 senders 聚合、at_notify_read 的 primary key 都不会被历史脏数据分裂。"""
    try:
        with AT_DB_LOCK, at_conn() as c:
            # 只捞标题里含括号（半角或全角）的可疑行，避免历史大表全表扫描
            rows = c.execute("SELECT id, title FROM at_notify WHERE title LIKE '%(%' OR title LIKE '%（%'").fetchall()
            fixed = 0
            for r in rows:
                new = _normalize_sender_title(r["title"])
                if new and new != r["title"]:
                    c.execute("UPDATE at_notify SET title=? WHERE id=?", (new, r["id"]))
                    fixed += 1
            if fixed:
                print(f"[anti_theft] migrated {fixed} at_notify title(s) to normalized sender key")
    except Exception as e:
        print(f"[anti_theft] _migrate_normalize_sender_titles error: {e}")

# ------------------------- SQLite -------------------------
def at_conn():
    c = sqlite3.connect(AT_DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    # 兜底建表：at_notify_read 由 notify-summary 端点引用，如果生产 init 未跑或崩溃过，
    # 每次连接都自检一次（CREATE TABLE IF NOT EXISTS 对已存在表是 O(1) 跳过，开销可忽略）。
    c.execute(
        """CREATE TABLE IF NOT EXISTS at_notify_read(
            device_id TEXT NOT NULL,
            pkg TEXT NOT NULL,
            sender TEXT NOT NULL,
            last_seen_ts INTEGER NOT NULL,
            PRIMARY KEY(device_id, pkg, sender)
        )"""
    )
    return c


def _ensure_at_albums_cols():
    """lazy 自愈：保证 at_albums 表具备 has_original/original_path/original_size 三列。

    背景：init_anti_theft_db 在 lifespan 里只在 main.py 启动时跑一次。如果部署新代码但没重启
    main.py 进程，内存里的 SQL 已带新列（如 SELECT has_original）而磁盘库仍是老 schema，
    会冒 OperationalError: no such column: has_original → 500。这个 helper 让每个访问
    at_albums 的入口在函数体最开头调一次，缺啥加啥，列齐全时 PRAGMA+O(1) 跳过、零 ALTER 开销。
    """
    try:
        with at_conn() as _c:
            _have = {r["name"] for r in _c.execute("PRAGMA table_info(at_albums)").fetchall()}
        for _col, _ty in (("has_original", "INTEGER"), ("original_path", "TEXT"), ("original_size", "TEXT")):
            if _col not in _have:
                with at_conn() as _c:
                    _c.execute(f"ALTER TABLE at_albums ADD COLUMN {_col} {_ty}")
                    print(f"[at_albums] self-heal: ADD COLUMN {_col} {_ty}", flush=True)
    except Exception as _e:
        # 不阻塞主流程：如果 ALTER 失败（比如另一连接锁了表），后续 SELECT 会自然冒 500 给浏览器 detail
        print(f"[at_albums] self-heal skipped: {_e}", flush=True)


def init_anti_theft_db():
    os.makedirs(AT_PHOTOS_DIR, exist_ok=True)
    os.makedirs(AT_ALBUMS_DIR, exist_ok=True)
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_devices(
                device_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                model TEXT,
                remark TEXT,
                owner TEXT,
                created_at INTEGER,
                last_seen INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_locations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                lat REAL, lng REAL, acc REAL, bat INTEGER,
                ts INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_photos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                path TEXT NOT NULL,
                note TEXT,
                ts INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_commands(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                cmd TEXT NOT NULL,
                params TEXT,
                status TEXT DEFAULT 'pending',
                created_at INTEGER,
                dispatched_at INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_albums(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                path TEXT NOT NULL,
                idx INTEGER,
                ts INTEGER
            )"""
        )
        # 增量同步去重：media_id = 手机 MediaStore._id；UNIQUE(device_id, media_id) 兜底
        # 旧记录 media_id 为 NULL，SQLite 允许多个 NULL 互不冲突，故无需迁移旧数据
        # 自愈合迁移：仅当列确实缺失时才 ALTER，且不再静默吞异常
        _album_cols = [r[1] for r in c.execute("PRAGMA table_info(at_albums)").fetchall()]
        if "media_id" not in _album_cols:
            c.execute("ALTER TABLE at_albums ADD COLUMN media_id INTEGER")
        # 原图按需获取：has_original=1 表示该张已缓存高清原图；original_path 为原图服务器路径
        for _col in ("has_original", "original_path", "original_size"):
            if _col not in _album_cols:
                c.execute(f"ALTER TABLE at_albums ADD COLUMN {_col} {'INTEGER' if _col=='has_original' else 'TEXT'}")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_albums_dev_media "
            "ON at_albums(device_id, media_id)"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                ts INTEGER,
                payload TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_notify(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                pkg TEXT,
                title TEXT,
                text TEXT,
                ts INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_notify_read(
                device_id TEXT NOT NULL,
                pkg TEXT NOT NULL,
                sender TEXT NOT NULL,
                last_seen_ts INTEGER NOT NULL,
                PRIMARY KEY(device_id, pkg, sender)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS at_audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                cmd TEXT NOT NULL,
                params TEXT,
                source_ip TEXT,
                channel TEXT,
                ts INTEGER
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_at_loc_device ON at_locations(device_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_at_cmd_device ON at_commands(device_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_at_notify_device ON at_notify(device_id, id DESC)")
        # 定位诊断列（表已存在时增量加，忽略已存在报错）
        for col, ctype in (("provider", "TEXT"), ("fix_age", "INTEGER"), ("satellites", "INTEGER"), ("low_accuracy", "INTEGER"), ("addr", "TEXT")):
            try:
                c.execute(f"ALTER TABLE at_locations ADD COLUMN {col} {ctype}")
            except Exception:
                pass
        # 照片 facing 列（区分前摄/后摄，控制旋转方向）；表已存在时增量加。
        # 用 PRAGMA 检查列是否存在再 ALTER，且打印日志（不再静默吞异常，
        # 否则 facing 列没建会导致 upload 接口 INSERT 报 no such column → 500）。
        try:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(at_photos)")}
            if "facing" not in cols:
                c.execute("ALTER TABLE at_photos ADD COLUMN facing TEXT")
                print("[init] at_photos.facing column added", flush=True)
        except Exception as e:
            print(f"[init] add at_photos.facing failed: {e}", flush=True)


# ------------------------- 鉴权 -------------------------
def _device_by_token(token):
    if not token:
        return None
    with at_conn() as c:
        row = c.execute("SELECT device_id FROM at_devices WHERE token=?", (token,)).fetchone()
    return row["device_id"] if row else None


def device_auth(token: str = Query(None)):
    did = _device_by_token(token)
    if did is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return did


def ctrl_auth(authorization: str = Header(None)):
    if not CTRL_TOKEN:
        raise HTTPException(status_code=500, detail="ctrl token not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing ctrl token")
    if authorization[7:] != CTRL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid ctrl token")
    return True


# ------------------------- 逆地理编码（高德主 + Nominatim 兜底）-------------------------
# 手机上报的是 WGS-84（GPS 原生）。高德为 GCJ-02（火星坐标），需转换后再查，否则偏移数百米。
_geo_cache = OrderedDict()  # "lat,lng(5位)" -> 地址字符串（LRU 缓存：避免重复网络请求 + 内存无限增长）
_GEO_CACHE_MAX = 500        # 缓存条目上限，超出淘汰最久未用


def _http_get_json(url: str, headers: dict = None, timeout: int = 6):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def wgs84_to_gcj02(lng: float, lat: float):
    """WGS-84 -> GCJ-02。高德/腾讯/百度(除百度外)均用 GCJ-02。"""
    a = 6378245.0
    ee = 0.00669342162296594323

    def out_of_china(x, y):
        return not (73.66 < x < 135.05 and 3.86 < y < 53.55)

    def transform_lat(x, y):
        ret = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20 * math.sin(6.0 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
        ret += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
        ret += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
        return ret

    def transform_lng(x, y):
        ret = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20 * math.sin(6.0 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
        ret += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
        ret += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
        return ret

    if out_of_china(lng, lat):
        return lng, lat
    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


# POI 类型过滤：道路/门牌/出入口等不是"可识别地标"，优先挑楼宇/商场/店/公园等
_REJECT_TYPE = ("道路", "门牌", "桥梁", "隧道", "匝道", "收费站", "出入口")
# 大区域地标：其行政/园区 polygon 极大，坐标落在里面会被当成归属地，禁止当主显示名
_BIG_AREA_TYPE = ("机场", "大学", "学院", "高等院校", "景区", "风景名胜", "旅游景点",
                  "工业园", "产业园", "度假村", "科技园")


def _poi_is_landmark(p: dict) -> bool:
    """POI 是否为人可一眼认出的地标（楼宇/商场/店/公园…），排除路名/门牌号。"""
    t = p.get("type") or ""
    return not any(k in t for k in _REJECT_TYPE)


def _poi_is_big_area(p: dict) -> bool:
    """POI 是否大区域地标（机场/大学/园区/景区…），polygon 极大，易污染地址。"""
    t = p.get("type") or ""
    return any(k in t for k in _BIG_AREA_TYPE)


def _str_or_first(v):
    """高德逆地理有时把 name/street/number/township 等返回成列表（空列表或单元素），
    统一成字符串，避免 [] .strip() 抛 AttributeError 导致整条逆地理失败。"""
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _ac_has_detail(ac: dict) -> bool:
    """addressComponent 是否包含乡镇(township)之后的细节（小区/街道/门牌）。
    仅到 province/city/district/township 时返回 False，此时应让最近 POI 当主名。"""
    if not ac:
        return False
    nb = ac.get("neighborhood") or {}
    nb_name = _str_or_first(nb.get("name") if isinstance(nb, dict) else "").strip()
    sn = ac.get("streetNumber") or {}
    street = _str_or_first(sn.get("street") if isinstance(sn, dict) else "").strip()
    number = _str_or_first(sn.get("number") if isinstance(sn, dict) else "").strip()
    return bool(nb_name or (street and number))


def _build_addr_from_component(ac: dict) -> str:
    """用结构化 addressComponent 拼地址，避免 formatted_address 把坐标所在的大
    polygon 地标（机场/园区）写进地址尾——落到哪归哪，不被 km 外地标污染。

    顺序：省/市/区 + 乡镇(街道) + 村/社区/小区(neighborhood) + 路 + 门牌号。
    """
    if not ac:
        return ""
    out = []

    for k in ("province", "city", "district"):
        v = _str_or_first(ac.get(k)).strip()
        if v:
            out.append(v)
    joined = "".join(out)
    town = _str_or_first(ac.get("township")).strip()
    nb = ac.get("neighborhood") or {}
    sn = ac.get("streetNumber") or {}
    nb_name = _str_or_first(nb.get("name") if isinstance(nb, dict) else "").strip()
    street = _str_or_first(sn.get("street") if isinstance(sn, dict) else "").strip()
    number = _str_or_first(sn.get("number") if isinstance(sn, dict) else "").strip()
    for v in (town, nb_name, street, number):
        if v and v not in joined:
            out.append(v)
            joined += v
    return "".join(out)


def amap_regeo(lat: float, lng: float):
    """高德逆地理：返回中文实际地址 + 最近可识别 POI。失败返回 None。

    策略（仿微信"发送位置"）：
      - 地址底名用 addressComponent 结构化拼装（省/市/区/街道/村或小区/路号），
        不再用 formatted_address（它会被坐标所在的大 polygon 地标——机场/园区——
        污染，返回"XX机场"这类离谱结果）；缺字段时回退 formatted_address。
      - POI 取距离 <=80m 内、属"可识别地标"且非"大区域地标"的最近一个（A+C 组合：
        优选楼宇/商场/店/小区，排除路名/门牌/机场/大学/园区）；
      - 该地标距离 <=30m 时，把它作为主显示名、地址退为括号补充（A：最贴近微信）；
      - 地标在 30~80m 或仅有路名时，仍作"地址（近 XX）"补充，避免远处地标污染。
    """
    if not AMAP_WEB_KEY:
        print("[geocode] AMAP_WEB_KEY 未配置，逆地理将回退 Nominatim（国内易失败）", flush=True)
        return None
    try:
        glng, glat = wgs84_to_gcj02(lng, lat)
        url = ("https://restapi.amap.com/v3/geocode/regeo"
               f"?location={glng:.6f},{glat:.6f}&key={AMAP_WEB_KEY}"
               "&radius=1000&extensions=all&roadlevel=0&language=zh_CN")
        data = _http_get_json(url)
        if data and data.get("status") == "1":
            rg = data.get("regeocode") or {}
            # 地址底名：优先 addressComponent 结构化拼装，避免机场/园区污染
            addr = _build_addr_from_component(rg.get("addressComponent") or {})
            if not addr:
                addr = (rg.get("formatted_address") or "").strip()  # 兜底
            if not addr:
                return None
            ac = rg.get("addressComponent") or {}
            has_detail = _ac_has_detail(ac)
            # 收集 300m 内候选 POI，按距离升序，挑最近可识别地标 + 最近任意
            cands = []
            for p in (rg.get("pois") or []):
                try:
                    d = float(p.get("distance", "9999"))
                except Exception:
                    continue
                if d > 300:
                    continue
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                if _poi_is_big_area(p):
                    continue  # 大区域地标（机场/大学/园区）绝不当主名也不当补充
                cands.append((d, name, _poi_is_landmark(p)))
            cands.sort()
            best_any = cands[0][1] if cands else None
            best_landmark = next((c[1] for c in cands if c[2]), None)
            landmark_dist = next((c[0] for c in cands if c[2]), None)
            # 组结果
            if best_landmark and (landmark_dist is not None
                                  and (landmark_dist <= 30 or not has_detail)):
                if best_landmark in addr:
                    return addr  # addr 已含该地标，不必重复主名
                return f"{best_landmark}（{addr}）"   # 地标当主名（仿微信"发送位置"）
            if best_landmark:
                return f"{addr}（近 {best_landmark}）"
            if best_any and best_any not in addr:
                return f"{addr}（近 {best_any}）"
            return addr
    except Exception as e:
        print("[geocode] amap fail:", e, flush=True)
    return None


def nominatim_regeo(lat: float, lng: float):
    """Nominatim(OSM) 逆地理兜底：免费免 key，中国地址质量一般。"""
    try:
        url = ("https://nominatim.openstreetmap.org/reverse"
               f"?format=jsonv2&lat={lat:.6f}&lon={lng:.6f}&accept-language=zh-CN&zoom=18")
        data = _http_get_json(url, headers={"User-Agent": "anti-theft-findmyphone/1.0"}, timeout=8)
        if data:
            adr = data.get("address") or {}
            order = ["country", "province", "state", "city", "county", "town",
                     "suburb", "village", "road", "pedestrian", "neighbourhood", "quarter"]
            seen, out = set(), []
            for k in order:
                v = adr.get(k)
                if v and v not in seen:
                    seen.add(v)
                    out.append(v)
            return " ".join(out) if out else data.get("display_name")
    except Exception as e:
        print("[geocode] nominatim fail:", e, flush=True)
    return None


def get_addr(lat, lng):
    """逆地理入口：高德优先，失败回退 Nominatim；按坐标 LRU 缓存（上限 500 条，防内存泄漏）。"""
    if lat is None or lng is None:
        return None
    key = f"{round(lat, 5)},{round(lng, 5)}"
    if key in _geo_cache:
        _geo_cache.move_to_end(key)  # LRU: 标记为最近使用
        return _geo_cache[key]
    try:
        res = amap_regeo(lat, lng) or nominatim_regeo(lat, lng)
    except Exception as e:
        print("[geocode] get_addr fail:", repr(e), flush=True)
        res = None
    if res is not None:                       # 仅缓存成功结果，避免 None 污染 LRU
        _geo_cache[key] = res
        if len(_geo_cache) > _GEO_CACHE_MAX:
            _geo_cache.popitem(last=False)    # 淘汰最久未用条目
    return res


# ------------------------- WS（激活后实时指令通道）-------------------------
at_ws = {}  # device_id -> ws


@router.websocket("/ws/anti-theft")
async def ws_anti_theft(ws: WebSocket, device_id: str = Query(None), token: str = Query(None)):
    did = _device_by_token(token)
    if did is None or did != device_id:
        await ws.close(code=1008, reason="unauthorized")
        return
    await ws.accept()
    at_ws[did] = ws
    _touch(did)
    print(f"[{time.strftime('%m-%d %H:%M:%S')}][ws_anti_theft] device {did} connected", flush=True)
    try:
        while True:
            data = await ws.receive()
            if data.get("type") == "websocket.disconnect":
                break
            # 手机可回传文本（ack / 上报），此处简单记录不阻塞
    except WebSocketDisconnect:
        # 客户端正常/异常断开（1000 干净关 / 1006 代理半关等）
        print(f"[{time.strftime('%m-%d %H:%M:%S')}][ws_anti_theft] device {did} client disconnected", flush=True)
    except Exception as e:
        # 任何非预期异常（如代理半关连接、协议错误）也兜底关连接，避免协程泄漏；
        # 日志带异常类型，便于排查重连风暴根因（是代理掐断还是客户端主动关）
        print(f"[{time.strftime('%m-%d %H:%M:%S')}][ws_anti_theft] device {did} ws error: {type(e).__name__}: {e}", flush=True)
    finally:
        if at_ws.get(did) is ws:
            at_ws.pop(did, None)
        print(f"[{time.strftime('%m-%d %H:%M:%S')}][ws_anti_theft] device {did} disconnected", flush=True)


def _touch(device_id):
    with AT_DB_LOCK, at_conn() as c:
        c.execute("UPDATE at_devices SET last_seen=? WHERE device_id=?", (int(time.time() * 1000), device_id))


# ------------------------- REST 接口 -------------------------
def _client_ip(req: Request) -> str:
    """取真实客户端 IP：XFF 取最后一个（Nginx $proxy_add_x_forwarded_for 把真实 IP append
    到末尾；第一个 IP 可能被客户端伪造，用于绕过注册限流），无 XFF 用直连地址。"""
    xff_raw = (req.headers.get("X-Forwarded-For") or "").strip()
    if xff_raw:
        parts = [p.strip() for p in xff_raw.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return req.client.host if req.client else "unknown"


def _audit_cmd(device_id, cmd, params, source_ip, channel):
    """命令下发审计：记录谁在什么时候通过什么 IP、走什么通道下发了什么命令。
    出问题（如误触 wipe）时可追溯。"""
    try:
        with AT_DB_LOCK, at_conn() as c:
            c.execute(
                "INSERT INTO at_audit_log(device_id, cmd, params, source_ip, channel, ts) VALUES(?,?,?,?,?,?)",
                (device_id, cmd, json.dumps(params, ensure_ascii=False) if params else "{}",
                 source_ip, channel, int(time.time() * 1000)),
            )
    except Exception as e:
        print(f"[audit] fail: {e}", flush=True)


# ------------------------- 数据清理（手动可选触发）-------------------------
# 控制台手动触发：时间窗（天）可选 30/60/90；数据类型可选 定位/照片/相册/指令。
# 已彻底移除后台每小时自动清理（避免 at_audit_log 缺失导致整事务回滚、清理全失效的问题）。
_CLEANUP_TYPES = ("locate", "photo", "album", "command")  # 可选删除的数据类型
_CLEANUP_DAYS_OPTIONS = (30, 60, 90)                       # 可选时间窗（天）


class CleanupReq(BaseModel):
    device_id: str
    days: int = 90
    types: List[str] = ["locate", "photo", "album", "command"]


def _normalize_cleanup(device_id, days, types):
    """校验入参，返回 (device_id, cutoff_ms, 合法 types 集合)；非法入参抛 HTTPException。"""
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id 不能为空")
    if days not in _CLEANUP_DAYS_OPTIONS:
        days = 90  # 容错：不在预设档位时夹到最大保留 90 天，不报错中断
    try:
        typeset = set(types or [])
    except Exception:
        typeset = set()
    typeset &= set(_CLEANUP_TYPES)
    if not typeset:
        raise HTTPException(status_code=400, detail="types 必须至少包含一个有效类型：" + "/".join(_CLEANUP_TYPES))
    cutoff = int((time.time() - days * 86400) * 1000)  # ms
    return device_id, cutoff, typeset


def _count_cleanup(c, device_id, cutoff, typeset):
    """统计各类型将被删除的条数（preview 用，不删除）。"""
    out = {}
    if "locate" in typeset:
        out["locate"] = c.execute(
            "SELECT COUNT(*) FROM at_locations WHERE device_id=? AND ts < ?", (device_id, cutoff)).fetchone()[0]
    if "photo" in typeset:
        out["photo"] = c.execute(
            "SELECT COUNT(*) FROM at_photos WHERE device_id=? AND ts < ?", (device_id, cutoff)).fetchone()[0]
    if "album" in typeset:
        out["album"] = c.execute(
            "SELECT COUNT(*) FROM at_albums WHERE device_id=? AND ts < ?", (device_id, cutoff)).fetchone()[0]
    if "command" in typeset:
        out["command"] = c.execute(
            "SELECT COUNT(*) FROM at_commands WHERE device_id=? AND status != 'pending' AND created_at < ?",
            (device_id, cutoff)).fetchone()[0]
    return out


def _do_cleanup(device_id, days, types):
    """按设备、按可选时间窗、按可选数据类型手动清理。
    返回各类型删除条数；photo/album 同时删除磁盘文件。不触碰 at_audit_log（避免报错）。"""
    device_id, cutoff, typeset = _normalize_cleanup(device_id, days, types)
    counts = {t: 0 for t in typeset}
    old_photos, old_albums = [], []
    try:
        with AT_DB_LOCK, at_conn() as c:
            if "photo" in typeset:
                old_photos = [r["path"] for r in c.execute(
                    "SELECT path FROM at_photos WHERE device_id=? AND ts < ?", (device_id, cutoff)).fetchall()]
                c.execute("DELETE FROM at_photos WHERE device_id=? AND ts < ?", (device_id, cutoff))
                counts["photo"] = len(old_photos)
            if "album" in typeset:
                old_albums = [(r["path"], r["original_path"]) for r in c.execute(
                    "SELECT path, original_path FROM at_albums WHERE device_id=? AND ts < ?", (device_id, cutoff)).fetchall()]
                c.execute("DELETE FROM at_albums WHERE device_id=? AND ts < ?", (device_id, cutoff))
                counts["album"] = len(old_albums)
            if "locate" in typeset:
                counts["locate"] = c.execute(
                    "DELETE FROM at_locations WHERE device_id=? AND ts < ?", (device_id, cutoff)).rowcount
            if "command" in typeset:
                counts["command"] = c.execute(
                    "DELETE FROM at_commands WHERE device_id=? AND status != 'pending' AND created_at < ?",
                    (device_id, cutoff)).rowcount
        # 磁盘文件删除放在事务外；单文件失败不影响其他记录与 DB 一致性
        deleted_files = 0
        for fname in old_photos:
            fpath = os.path.join(AT_PHOTOS_DIR, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    deleted_files += 1
                except Exception:
                    pass
        originals = set()
        for apath, orel in old_albums:
            fpath = os.path.join(AT_ALBUMS_DIR, apath)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    deleted_files += 1
                except Exception:
                    pass
            if orel:
                originals.add(orel)
        # 联级删原图（缩略图全删完后统一删，避免同一原图重复 unlink）
        for orel in originals:
            ofp = os.path.join(AT_ALBUMS_DIR, orel)
            if os.path.isfile(ofp):
                try:
                    os.remove(ofp)
                except Exception:
                    pass
        print(f"[cleanup] device={device_id} days={days} deleted={counts} files={deleted_files}", flush=True)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[cleanup] error: {e}", flush=True)
        raise HTTPException(status_code=500, detail=f"清理失败：{e}")
    return {"device_id": device_id, "days": days, "deleted": counts, "deleted_files": deleted_files}


@router.get("/anti-theft/cleanup-preview")
async def at_cleanup_preview(
    device_id: str = Query(...),
    days: int = Query(90),
    types: List[str] = Query(None),
    _: bool = Depends(ctrl_auth),
):
    """预估清理影响（不删除）：返回各类型将删除条数，供确认弹窗展示。"""
    try:
        _, cutoff, typeset = _normalize_cleanup(device_id, days, types or list(_CLEANUP_TYPES))
        with AT_DB_LOCK, at_conn() as c:
            preview = _count_cleanup(c, device_id, cutoff, typeset)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预估失败：{e}")
    return {"device_id": device_id, "days": days, "types": sorted(typeset), "preview": preview}


@router.post("/anti-theft/cleanup")
async def at_cleanup(req: CleanupReq, _: bool = Depends(ctrl_auth)):
    """手动触发可选清理：按设备、按时间窗、按数据类型删除（含照片/相册磁盘文件）。"""
    result = _do_cleanup(req.device_id, req.days, req.types)
    try:
        _audit_cmd(req.device_id, "cleanup",
                   {"days": req.days, "types": req.types, "deleted": result["deleted"]}, "-", "console")
    except Exception:
        pass
    return result


# 注册接口应用层限流（兜底：防无限注册刷爆磁盘/污染设备列表；手机正常注册仅一次，不受影响）
_REGISTER_LIMIT = {}
_REGISTER_LIMIT_LOCK = threading.Lock()
_REGISTER_WINDOW = 60  # 滑动窗口（秒）
_REGISTER_MAX = 10     # 窗口内单 IP 最大注册次数

def _register_allowed(client_ip: str) -> bool:
    now = time.time()
    with _REGISTER_LIMIT_LOCK:
        ts = _REGISTER_LIMIT.get(client_ip, [])
        ts = [t for t in ts if now - t < _REGISTER_WINDOW]
        if len(ts) >= _REGISTER_MAX:
            _REGISTER_LIMIT[client_ip] = ts
            return False
        ts.append(now)
        _REGISTER_LIMIT[client_ip] = ts
        return True

@router.post("/anti-theft/register")
async def at_register(req: Request):
    """手机首次注册，返回独立 device_id + token（开放注册，注册后才有凭证）"""
    client_ip = _client_ip(req)
    if not _register_allowed(client_ip):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    try:
        body = await req.json()
    except Exception:
        body = {}
    model = (body.get("model") or "").strip()
    remark = (body.get("remark") or "").strip() or model or "device"
    owner = (body.get("owner") or "").strip() or "me"
    device_id = "dev_" + secrets.token_hex(8)
    token = secrets.token_hex(32)
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            "INSERT INTO at_devices(device_id, token, model, remark, owner, created_at, last_seen) "
            "VALUES(?,?,?,?,?,?,?)",
            (device_id, token, model, remark, owner, int(time.time() * 1000), int(time.time() * 1000)),
        )
    return {"device_id": device_id, "token": token}


@router.post("/anti-theft/report")
async def at_report(req: Request, device_id: str = Depends(device_auth)):
    """手机上报位置/状态（激活后高频调用）"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    lat = body.get("lat")
    lng = body.get("lng")
    acc = body.get("acc")
    bat = body.get("bat")
    provider = body.get("provider")
    fix_age = body.get("fix_age")
    satellites = body.get("satellites")
    low_accuracy = body.get("low_accuracy")
    addr = body.get("addr")
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            "INSERT INTO at_locations(device_id, lat, lng, acc, bat, provider, fix_age, satellites, low_accuracy, addr, ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, lat, lng, acc, bat, provider, fix_age, satellites,
             1 if low_accuracy else 0, addr, int(time.time() * 1000)),
        )
        c.execute("UPDATE at_devices SET last_seen=? WHERE device_id=?", (int(time.time() * 1000), device_id))
    return {"ok": True}


@router.get("/anti-theft/poll")
async def at_poll(device_id: str = Depends(device_auth)):
    """手机轮询拉取所有待执行指令（WS 离线时的兜底通道）。

    一次性取走全部 pending —— 等价于「把多条命令融合成一条」：最坏只等一个 15 分钟周期，
    不再因 N 条命令而等 N×15 分钟。按执行顺序排列：采集/通道在前，stop 最后。
    自动清理超过 5 分钟的陈旧 pending 命令，避免旧命令被意外执行。
    """
    _touch(device_id)
    with AT_DB_LOCK, at_conn() as c:
        # 清理超过 5 分钟的陈旧 pending 命令（created_at 是毫秒时间戳）
        cutoff_ms = int(time.time() * 1000) - 5 * 60 * 1000
        c.execute(
            "DELETE FROM at_commands WHERE device_id=? AND status='pending' AND created_at<?",
            (device_id, cutoff_ms),
        )
        rows = c.execute(
            "SELECT id, cmd, params FROM at_commands WHERE device_id=? AND status='pending' ORDER BY id ASC",
            (device_id,),
        ).fetchall()
        if not rows:
            return {"commands": []}
        # 执行顺序：realtime(开)→采集(locate/photo/album/activate)→lock→wipe→realtime(关)→stop
        order = {"realtime": 0, "locate": 1, "photo": 1, "album": 1, "activate": 1, "usage": 1,
                 "lock": 2, "wipe": 3, "stop": 9}
        items = sorted(rows, key=lambda r: order.get(r["cmd"], 5))
        out = []
        for r in items:
            out.append({"cmd_id": r["id"], "cmd": r["cmd"], "params": json.loads(r["params"] or "{}")})
            c.execute(
                "UPDATE at_commands SET status='dispatched', dispatched_at=? WHERE id=?",
                (int(time.time() * 1000), r["id"]),
            )
        return {"commands": out}


def _coalesce_command(device_id, cmd, params):
    """合并指令写入队列（纯函数，便于测试）。返回最终下发的 (final_cmd, final_params)。

    合并规则（防狂点堆积成多个轮询周期）：
      - activate 之间：合并参数（locate/photo/album 取 OR，album_n 取 MAX）
      - locate/photo/album：若已有 pending activate 则折叠进它，不新增独立行
      - activate 到达时：吞掉已有的 pending locate/photo/album（折进参数）
      - lock/wipe/realtime：始终独立新增（强指令，保持显式分离）
      - stop：始终独立新增（终止态，放最后执行）
    """
    final_cmd = cmd
    final_params = params
    with AT_DB_LOCK, at_conn() as c:
        act = c.execute(
            "SELECT id, params FROM at_commands WHERE device_id=? AND cmd='activate' AND status='pending' ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()

        def _fold(p, src_cmd, src_params):
            if src_cmd == "locate":
                p["locate"] = 1
            elif src_cmd == "photo":
                p["photo"] = 1
            elif src_cmd == "album":
                p["album"] = 1
                p["album_n"] = max(int(p.get("album_n", 0) or 0), int((src_params or {}).get("n", 0) or 0))

        if cmd == "activate":
            if act is not None:
                # 新命令覆盖旧命令：用户明确说 photo=0 就不应该拍照
                # 旧逻辑用 max() 合并，导致旧 photo=1 永远不会被新 photo=0 覆盖
                p = {"locate": 1 if params.get("locate") else 0,
                     "photo": 1 if params.get("photo") else 0,
                     "album": 1 if params.get("album") else 0,
                     "album_n": int(params.get("album_n", 0) or 0),
                     "continuous": 0}
                # 删除旧的独立 locate/photo/album 命令（不折叠进 activate）
                c.execute("DELETE FROM at_commands WHERE device_id=? AND status='pending' AND cmd IN ('locate','photo','album')", (device_id,))
                c.execute("UPDATE at_commands SET params=? WHERE id=?", (json.dumps(p), act["id"]))
                final_params = p
            else:
                p = {"locate": 1 if params.get("locate") else 0,
                     "photo": 1 if params.get("photo") else 0,
                     "album": 1 if params.get("album") else 0,
                     "album_n": int(params.get("album_n", 0) or 0),
                     "continuous": 0}
                for r in c.execute(
                    "SELECT cmd, params FROM at_commands WHERE device_id=? AND status='pending' AND cmd IN ('locate','photo','album')",
                    (device_id,),
                ).fetchall():
                    _fold(p, r["cmd"], json.loads(r["params"] or "{}"))
                c.execute("DELETE FROM at_commands WHERE device_id=? AND status='pending' AND cmd IN ('locate','photo','album')", (device_id,))
                c.execute(
                    "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                    (device_id, "activate", json.dumps(p), "pending", int(time.time() * 1000)),
                )
                final_params = p
        elif cmd in ("locate", "album"):
            if act is not None:
                p = json.loads(act["params"] or "{}")
                _fold(p, cmd, params)
                c.execute("UPDATE at_commands SET params=? WHERE id=?", (json.dumps(p), act["id"]))
                final_cmd = "activate"
                final_params = p
            else:
                ins = params if cmd != "album" else {"n": int(params.get("n", 10) or 10),
                                                          "from_ts": params.get("from_ts"),
                                                          "to_ts": params.get("to_ts"),
                                                          "reset": params.get("reset")}
                c.execute(
                    "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                    (device_id, cmd, json.dumps(ins), "pending", int(time.time() * 1000)),
                )
        elif cmd == "photo":
            # 拍照指令始终独立入队：保留 facing（前/后摄），不折进 activate
            # （折进 activate 会丢失 facing，且安卓端 activate 路径写死前摄）
            c.execute(
                "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                (device_id, "photo", json.dumps(params if params else {}), "pending", int(time.time() * 1000)),
            )
        elif cmd == "usage":
            # 应用使用统计：一次性采集指令，独立 pending，不折进 activate（activate 不处理 usage）
            c.execute(
                "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                (device_id, "usage", json.dumps(params if params else {}), "pending", int(time.time() * 1000)),
            )
        elif cmd == "stop":
            # 停止前台服务（立刻回静默）
            existing = c.execute(
                "SELECT id FROM at_commands WHERE device_id=? AND status='pending' AND cmd='stop' LIMIT 1",
                (device_id,),
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                    (device_id, "stop", json.dumps({}), "pending", int(time.time() * 1000)),
                )
        else:  # lock / wipe / realtime 等强指令：始终独立新增
            c.execute(
                "INSERT INTO at_commands(device_id, cmd, params, status, created_at) VALUES(?,?,?,?,?)",
                (device_id, cmd, json.dumps(params), "pending", int(time.time() * 1000)),
            )
    return final_cmd, final_params


@router.post("/anti-theft/command")
async def at_command(req: Request, _: bool = Depends(ctrl_auth)):
    """控制端下发指令。

    - 设备 WS 实时通道在线：直接 push（不写队列，避免 poll 重复执行）；多命令各自推、手机串行。
    - WS 离线：合并写入队列，等手机下一个 ≤15 分钟轮询周期一次性取走全部（等价于「融合成一条」）。
    """
    body = await req.json()
    device_id = body.get("device_id")
    cmd = body.get("cmd")
    params = body.get("params", {}) or {}
    if not device_id or not cmd:
        raise HTTPException(status_code=400, detail="device_id and cmd required")

    ip = _client_ip(req)
    ws = at_ws.get(device_id)
    if ws is not None:
        try:
            await ws.send_text(json.dumps({"type": "command", "cmd": cmd, "params": params}))
            _audit_cmd(device_id, cmd, params, ip, "ws")
            return {"ok": True, "cmd": cmd, "params": params, "channel": "ws"}
        except Exception:
            # push 失败（多为 WS 半开/已死但服务端尚未检出）：清理 at_ws 残留条目，
            # 否则控制台会一直误显示「已开启」，且后续命令都石沉大海。清理后回退到 poll 队列。
            at_ws.pop(device_id, None)
            try:
                await ws.close()
            except Exception:
                pass
    final_cmd, final_params = _coalesce_command(device_id, cmd, params)
    _audit_cmd(device_id, final_cmd, final_params, ip, "poll")
    return {"ok": True, "cmd": final_cmd, "params": final_params, "channel": "poll"}


@router.get("/anti-theft/commands")
async def at_commands(device_id: str = Query(...), _: bool = Depends(ctrl_auth)):
    """查看某设备操作队列：待执行(pending) + 已下发(dispatched)。"""
    with at_conn() as c:
        rows = c.execute(
            "SELECT id, cmd, params, status, created_at, dispatched_at FROM at_commands WHERE device_id=? ORDER BY id DESC LIMIT 100",
            (device_id,),
        ).fetchall()
    return {"commands": [
        {"id": r["id"], "cmd": r["cmd"], "params": json.loads(r["params"] or "{}"),
         "status": r["status"], "created_at": r["created_at"], "dispatched_at": r["dispatched_at"]}
        for r in rows
    ]}


@router.delete("/anti-theft/command/{cmd_id}")
async def at_command_delete(cmd_id: int, _: bool = Depends(ctrl_auth)):
    """取消一条待执行指令（已下发的不可撤，返回 409）。"""
    with AT_DB_LOCK, at_conn() as c:
        row = c.execute("SELECT id, status FROM at_commands WHERE id=?", (cmd_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="command not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="only pending commands can be cancelled")
        c.execute("DELETE FROM at_commands WHERE id=?", (cmd_id,))
    return {"ok": True}


@router.delete("/anti-theft/commands")
async def at_commands_clear(device_id: str = Query(...), _: bool = Depends(ctrl_auth)):
    """清空某设备所有待执行指令（已下发的保留）。"""
    deleted = 0
    with AT_DB_LOCK, at_conn() as c:
        cur = c.execute("DELETE FROM at_commands WHERE device_id=? AND status='pending'", (device_id,))
        deleted = cur.rowcount
    return {"ok": True, "deleted": deleted}


@router.get("/anti-theft/audit-log")
async def at_audit_query(device_id: str = Query(None), limit: int = Query(200), _: bool = Depends(ctrl_auth)):
    """查看命令下发审计日志：谁在什么时候通过什么 IP、走什么通道下发了什么命令。
    出问题（如误触 wipe）时可追溯。默认最近 200 条，可按 device_id 过滤。"""
    limit = max(1, min(limit, 1000))
    with at_conn() as c:
        if device_id:
            rows = c.execute(
                "SELECT id, device_id, cmd, params, source_ip, channel, ts FROM at_audit_log "
                "WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, device_id, cmd, params, source_ip, channel, ts FROM at_audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"logs": [
        {"id": r["id"], "device_id": r["device_id"], "cmd": r["cmd"],
         "params": json.loads(r["params"] or "{}"), "source_ip": r["source_ip"],
         "channel": r["channel"], "ts": r["ts"]}
        for r in rows
    ]}


@router.get("/anti-theft/devices")
async def at_devices(_: bool = Depends(ctrl_auth)):
    with at_conn() as c:
        rows = c.execute("SELECT device_id, remark, model, last_seen FROM at_devices").fetchall()
    return {"devices": [dict(r) for r in rows]}


@router.delete("/anti-theft/device/{device_id}")
async def at_device_delete(device_id: str, _: bool = Depends(ctrl_auth)):
    """彻底删除某设备：清 DB 记录（devices/locations/commands/photos）+ 磁盘照片文件 + 断开 WS。不可逆。"""
    with AT_DB_LOCK, at_conn() as c:
        exist = c.execute("SELECT 1 FROM at_devices WHERE device_id=?", (device_id,)).fetchone()
        if exist is None:
            raise HTTPException(status_code=404, detail="device not found")
        rows = c.execute("SELECT path FROM at_photos WHERE device_id=?", (device_id,)).fetchall()
        c.execute("DELETE FROM at_devices WHERE device_id=?", (device_id,))
        c.execute("DELETE FROM at_locations WHERE device_id=?", (device_id,))
        c.execute("DELETE FROM at_commands WHERE device_id=?", (device_id,))
        c.execute("DELETE FROM at_photos WHERE device_id=?", (device_id,))
    for r in rows:
        try:
            fpath = os.path.join(AT_PHOTOS_DIR, r["path"])
            if os.path.isfile(fpath):
                os.remove(fpath)
        except Exception:
            pass
    shutil.rmtree(os.path.join(AT_ALBUMS_DIR, device_id), ignore_errors=True)
    ws = at_ws.pop(device_id, None)
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    return {"ok": True}


@router.get("/anti-theft/locations")
async def at_locations(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    limit: int = Query(5),
    offset: int = Query(0),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """最近位置分页拉取：读哪一页加载哪一页（不拉全量），支持时间窗过滤。
    返回 total 供前端计算页数；坐标仍带 gcj_lat/gcj_lng（高德地图标点用）。"""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    clauses = ["device_id=?"]
    args = [device_id]
    if from_ts is not None:
        clauses.append("ts>=?")
        args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?")
        args.append(int(to_ts))
    where = " AND ".join(clauses)
    with at_conn() as c:
        rows = c.execute(
            f"SELECT lat, lng, acc, bat, ts, provider, fix_age, satellites, low_accuracy, addr "
            f"FROM at_locations WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset),
        ).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM at_locations WHERE {where}", tuple(args)).fetchone()[0]
    out = []
    for i, r in enumerate(rows):
        d = dict(r)
        # 优先用手机端腾讯 SDK 返回的地址；仅当手机未上报地址时才走后端逆地理（省 API 额度）
        # 分页后每页最多 100 条(前端实际只取 5 条)，无旧版 i<10 截断必要
        phone_addr = d.get("addr")
        d["addr"] = phone_addr or get_addr(r["lat"], r["lng"])
        # GCJ-02 坐标（高德/腾讯地图需要 GCJ-02；手机上报的是 WGS-84）
        glng, glat = wgs84_to_gcj02(d["lng"], d["lat"])
        d["gcj_lat"] = glat
        d["gcj_lng"] = glng
        out.append(d)
    return {
        "locations": out,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@router.get("/anti-theft/photos")
async def at_photos(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    limit: int = Query(20),
    offset: int = Query(0),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """照片分页拉取：读哪一页加载哪一页（不拉全量），支持时间窗过滤。"""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    clauses = ["device_id=?"]
    args = [device_id]
    if from_ts is not None:
        clauses.append("ts>=?")
        args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?")
        args.append(int(to_ts))
    where = " AND ".join(clauses)
    with at_conn() as c:
        rows = c.execute(
            f"SELECT id, path, note, ts, facing FROM at_photos WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset),
        ).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM at_photos WHERE {where}", tuple(args)).fetchone()[0]
    return {
        "photos": [
            {"id": r["id"], "path": r["path"], "facing": r["facing"] or "front",
             "url": f"/anti-theft/photo/{r['path']}", "note": r["note"], "ts": r["ts"]}
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@router.get("/anti-theft/photo/{fname}")
async def at_photo(fname: str, _: bool = Depends(ctrl_auth)):
    """控制台显示照片：Bearer 鉴权后返回图片字节（img 标签无法带 header，前端用 fetch+blob）"""
    if not fname or "/" in fname or "\\" in fname or ".." in fname:
        raise HTTPException(status_code=400, detail="bad name")
    fpath = os.path.join(AT_PHOTOS_DIR, fname)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(fpath, media_type="image/jpeg")


@router.delete("/anti-theft/photo/{fname}")
async def at_photo_delete(fname: str, _: bool = Depends(ctrl_auth)):
    """删除单张照片：物理删除磁盘文件 + 清理 at_photos 记录。不可逆。"""
    if not fname or "/" in fname or "\\" in fname or ".." in fname:
        raise HTTPException(status_code=400, detail="bad name")
    with AT_DB_LOCK, at_conn() as c:
        exist = c.execute("SELECT 1 FROM at_photos WHERE path=?", (fname,)).fetchone()
        if exist is None:
            raise HTTPException(status_code=404, detail="photo not found")
        c.execute("DELETE FROM at_photos WHERE path=?", (fname,))
    fpath = os.path.join(AT_PHOTOS_DIR, fname)
    if os.path.isfile(fpath):
        try:
            os.remove(fpath)
        except Exception:
            pass
    return {"ok": True}


@router.post("/anti-theft/upload")
async def at_upload(device_id: str = Depends(device_auth), file: UploadFile = File(...), note: str = "", facing: str = Form("front")):
    """手机上传照片（默认前摄嫌疑人脸），存磁盘不进相册，DB 只存路径。
    facing=front 逆时针旋转 90°、back 顺时针旋转 90°（PIL 重存，保证落盘即正确方向，
    网页/导出均无需再处理）。facing 仅 front/back 两种，其他值按 front 处理。"""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="file too large (max 20MB)")
    # 按 facing 旋转（PIL rotate 角度为逆时针：+90=逆时针，-90=顺时针）
    rotated = False
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        angle = -90 if facing == "back" else 90
        img = img.rotate(angle, expand=True)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        data = buf.getvalue()
        rotated = True
        print(f"[upload] facing={facing} angle={angle} rotated ok, size={len(data)}B", flush=True)
    except Exception as e:
        # PIL 失败时退回原图直存，不影响上传主流程
        print(f"[upload] facing={facing} PIL rotate failed, save raw: {e}", flush=True)
    fname = f"{device_id}_{int(time.time() * 1000)}.jpg"
    fpath = os.path.join(AT_PHOTOS_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(data)
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            "INSERT INTO at_photos(device_id, path, note, ts, facing) VALUES(?,?,?,?,?)",
            (device_id, fname, note, int(time.time() * 1000), facing),
        )
    return {"ok": True, "path": fname}


@router.post("/anti-theft/album")
async def at_album_upload(device_id: str = Depends(device_auth), file: UploadFile = File(...), idx: int = Form(0), media_id: int = Form(None)):
    """手机上传相册缩略图（增量同步：上一份按时间窗仅传未上传过的子集）。
    media_id = 手机 MediaStore._id，UNIQUE(device_id, media_id) 兜底去重；旧手机端不传时为 NULL。"""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="file too large (max 20MB)")
    ddir = os.path.join(AT_ALBUMS_DIR, device_id)
    os.makedirs(ddir, exist_ok=True)
    fname = f"{int(time.time() * 1000)}_{idx}.jpg"
    fpath = os.path.join(ddir, fname)
    with open(fpath, "wb") as f:
        f.write(data)
    try:
        with AT_DB_LOCK, at_conn() as c:
            c.execute(
                "INSERT INTO at_albums(device_id, path, idx, ts, media_id) VALUES(?,?,?,?,?)",
                (device_id, f"{device_id}/{fname}", idx, int(time.time() * 1000), media_id),
            )
    except sqlite3.IntegrityError:
        # UNIQUE(device_id, media_id) 命中：该媒体已存在（真去重），删掉刚写的磁盘文件，返回 ok 不报错
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except Exception:
            pass
        return {"ok": True, "path": None, "dup": True}
    except Exception as e:
        # 其它异常（缺列 / 磁盘满 / 并发锁）：保留现场、打印异常、返回 500 让手机重试，绝不再静默吞照片
        import traceback as _tb
        _tb.print_exc()
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"album insert failed: {e}")
    return {"ok": True, "path": f"{device_id}/{fname}"}


@router.post("/anti-theft/album/original")
async def at_album_original_upload(
    device_id: str = Depends(device_auth),
    file: UploadFile = File(...),
    idx: int = Form(...),
    media_id: int = Form(None),
):
    """手机按需上传单张相册原图（高清）。落盘到 <AT_ALBUMS_DIR>/<device_id>/original/，
    并标记 at_albums.has_original=1；控制台「查看原图」命中此缓存后不再下发指令。
    原图不限制到 20MB 上限（手机照片/截图普遍 3~10MB，但仍设 50MB 兜底防恶意大文件）。"""
    # 自愈：旧 main.py 进程未重启 → UPDATE SET has_original=1/original_path=… 会 500
    _ensure_at_albums_cols()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (max 50MB)")
    odir = os.path.join(AT_ALBUMS_DIR, device_id, "original")
    os.makedirs(odir, exist_ok=True)
    fname = f"{int(time.time() * 1000)}_{idx}.jpg"
    fpath = os.path.join(odir, fname)
    with open(fpath, "wb") as f:
        f.write(data)
    with AT_DB_LOCK, at_conn() as c:
        # 优先用「缩略图行 id」精确定位（不受 media_id 是否一致、占位行干扰）。
        # 匹配规则：device_id 相等 + idx 相等 + (path 非空) — 排除占位空行
        thumb_row = c.execute(
            "SELECT id FROM at_albums WHERE device_id=? AND idx=? AND (path IS NOT NULL AND path != '') "
            "ORDER BY id DESC LIMIT 1",
            (device_id, idx),
        ).fetchone()
        if thumb_row is not None:
            c.execute(
                "UPDATE at_albums SET has_original=1, original_path=?, original_size=?, media_id=COALESCE(media_id, ?) "
                "WHERE id=?",
                (f"{device_id}/original/{fname}", len(data), media_id, thumb_row["id"]),
            )
        else:
            # 没有对应缩略图记录（极端：先拉原图再同步），单独建一条占位
            c.execute(
                "INSERT INTO at_albums(device_id, path, idx, ts, media_id, has_original, original_path, original_size) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (device_id, "", idx, int(time.time() * 1000), media_id, f"{device_id}/original/{fname}", len(data)),
            )
    return {"ok": True, "path": f"{device_id}/original/{fname}"}


@router.get("/anti-theft/album/original")
async def at_album_original_get(device_id: str = Query(...), idx: int = Query(...)):
    """控制台取原图：返回该张的高清原图文件（存在则 200，否则 404 由前端判定为「需申请」）。
    三段式查找：
      1) DB 有 original_path + 磁盘文件存在 → 直接返回（命中缓存）
      2) DB 查不到但磁盘扫到 {ms}_{idx}.jpg → 返回并补写 DB（自愈历史脏数据）
      3) 都没有 → 404
    """
    # 自愈：旧 main.py 进程未重启 → SELECT original_path 会 500
    _ensure_at_albums_cols()
    row = None
    with AT_DB_LOCK, at_conn() as c:
        row = c.execute(
            "SELECT original_path FROM at_albums WHERE device_id=? AND idx=? AND (original_path IS NOT NULL AND original_path != '') ORDER BY id DESC LIMIT 1",
            (device_id, idx),
        ).fetchone()
    if row and row[0]:
        fpath = os.path.join(AT_ALBUMS_DIR, row[0])
        if os.path.isfile(fpath):
            return FileResponse(fpath, media_type="image/jpeg")
    # 兜底：磁盘扫描 original/{device_id}/ 目录，匹配文件名后缀 _{idx}.jpg
    odir = os.path.join(AT_ALBUMS_DIR, device_id, "original")
    if os.path.isdir(odir):
        suffix = f"_{idx}.jpg"
        try:
            for fname in os.listdir(odir):
                if fname.endswith(suffix):
                    fpath = os.path.join(odir, fname)
                    rel = f"{device_id}/original/{fname}"
                    # 补写回 DB（用缩略图行 id，避免覆盖缩略图 path）
                    with AT_DB_LOCK, at_conn() as c:
                        thumb = c.execute(
                            "SELECT id FROM at_albums WHERE device_id=? AND idx=? AND (path IS NOT NULL AND path != '') ORDER BY id DESC LIMIT 1",
                            (device_id, idx),
                        ).fetchone()
                        fsize = os.path.getsize(fpath)
                        if thumb is not None:
                            c.execute(
                                "UPDATE at_albums SET has_original=1, original_path=?, original_size=? WHERE id=?",
                                (rel, fsize, thumb["id"]),
                            )
                        else:
                            c.execute(
                                "INSERT INTO at_albums(device_id, path, idx, ts, has_original, original_path, original_size) VALUES(?,?,?,?,1,?,?)",
                                (device_id, "", idx, int(time.time() * 1000), rel, fsize),
                            )
                    return FileResponse(fpath, media_type="image/jpeg")
        except Exception as _e:
            print(f"[at_album_original_get] disk scan fail: {_e}", flush=True)
    raise HTTPException(status_code=404, detail="original not available")


@router.get("/anti-theft/albums")
async def at_albums_list(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    limit: int = Query(20),
    offset: int = Query(0),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """相册缩略图分页拉取：读哪一页加载哪一页（不拉全量），支持时间窗过滤。"""
    # 自愈：旧 main.py 进程未重启场景下，at_albums 表可能缺 has_original/original_path/original_size 三列
    # → SELECT/UPDATE 会 500 (no such column: has_original)。PRAGMA+O(1) 跳过已存在列，正常情况下零开销。
    # 真正修复还是宝塔「进程守护管理器」重启 main.py 让 lifespan 跑一次 init_anti_theft_db。
    _ensure_at_albums_cols()
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    clauses = ["device_id=?"]
    args = [device_id]
    if from_ts is not None:
        clauses.append("ts>=?")
        args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?")
        args.append(int(to_ts))
    where = " AND ".join(clauses)
    with at_conn() as c:
        rows = c.execute(
            f"SELECT id, path, idx, ts, has_original, media_id FROM at_albums WHERE {where} ORDER BY id ASC LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset),
        ).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM at_albums WHERE {where}", tuple(args)).fetchone()[0]
    return {
        "albums": [
            {"id": r["id"], "path": r["path"], "idx": r["idx"],
             "url": f"/anti-theft/album/{r['path']}", "ts": r["ts"],
             "has_original": bool(r["has_original"] or 0),
             "media_id": r["media_id"]}
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@router.get("/anti-theft/album/{path:path}")
async def at_album_file(path: str, _: bool = Depends(ctrl_auth)):
    """控制台显示相册缩略图：Bearer 鉴权后返回图片字节。"""
    if not path or ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="bad path")
    fpath = os.path.join(AT_ALBUMS_DIR, path)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(fpath, media_type="image/jpeg")


@router.delete("/anti-theft/album/{path:path}")
async def at_album_delete(path: str, _: bool = Depends(ctrl_auth)):
    """删除单张相册缩略图：物理删除磁盘文件 + 清理 at_albums 记录。
    若该张已缓存原图（original_path 非空），同步删除原图文件，避免磁盘孤儿。不可逆。"""
    if not path or ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="bad path")
    original_rel = None
    with AT_DB_LOCK, at_conn() as c:
        row = c.execute("SELECT original_path FROM at_albums WHERE path=?", (path,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="album not found")
        original_rel = row["original_path"]
        c.execute("DELETE FROM at_albums WHERE path=?", (path,))
    fpath = os.path.join(AT_ALBUMS_DIR, path)
    if os.path.isfile(fpath):
        try:
            os.remove(fpath)
        except Exception:
            pass
    # 联级删原图（仅删磁盘文件，DB 行已随上面 DELETE 一起删）
    if original_rel:
        ofp = os.path.join(AT_ALBUMS_DIR, original_rel)
        if os.path.isfile(ofp):
            try:
                os.remove(ofp)
            except Exception:
                pass
    return {"ok": True}


@router.get("/anti-theft/albums/count")
async def at_albums_count(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """相册缩略图轻量计数：给清空确认弹窗预估数量用（只 COUNT，不返回任何行）。"""
    clauses = ["device_id=?"]
    args = [device_id]
    if from_ts is not None:
        clauses.append("ts>=?"); args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?"); args.append(int(to_ts))
    where = " AND ".join(clauses)
    with at_conn() as c:
        n = c.execute(f"SELECT COUNT(*) FROM at_albums WHERE {where}", tuple(args)).fetchone()[0]
    return {"count": n}


@router.delete("/anti-theft/albums")
async def at_albums_clear(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """清空相册：删除该设备全部（或指定时间窗内）相册缩略图（DB 记录 + 磁盘文件）。
    联级删除已缓存的原图（DB 行的 original_path 指向的原图文件）。
    与手机端基线无关；若需手机重新上传被删的图，请单独下发 album reset 指令。不可逆。"""
    clauses = ["device_id=?"]
    args = [device_id]
    if from_ts is not None:
        clauses.append("ts>=?"); args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?"); args.append(int(to_ts))
    where = " AND ".join(clauses)
    with AT_DB_LOCK, at_conn() as c:
        rows = c.execute(
            f"SELECT path, original_path FROM at_albums WHERE {where}", tuple(args)
        ).fetchall()
        c.execute(f"DELETE FROM at_albums WHERE {where}", tuple(args))
    deleted = 0
    originals = set()
    for r in rows:
        fp = os.path.join(AT_ALBUMS_DIR, r["path"])
        if os.path.isfile(fp):
            try:
                os.remove(fp); deleted += 1
            except Exception:
                pass
        if r["original_path"]:
            originals.add(r["original_path"])
    # 联级删原图
    for orel in originals:
        ofp = os.path.join(AT_ALBUMS_DIR, orel)
        if os.path.isfile(ofp):
            try:
                os.remove(ofp)
            except Exception:
                pass
    return {"ok": True, "deleted": deleted}


@router.post("/anti-theft/usage-report")
async def at_usage_upload(req: Request, device_id: str = Depends(device_auth)):
    """手机上传应用使用统计（手动触发，一次性全量：按天分桶使用记录 + 前台 App + 已装列表）。
    存最新一份（删旧）。"""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    usage_n = len(body.get("usage") or [])
    inst_n = len(body.get("installed") or [])
    fg = body.get("foreground")
    print(f"[at_usage_upload] device={device_id} usage={usage_n} installed={inst_n} foreground={fg is not None}", flush=True)
    payload = json.dumps(body, ensure_ascii=False)
    if len(payload) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="payload too large")
    with AT_DB_LOCK, at_conn() as c:
        c.execute("DELETE FROM at_usage WHERE device_id=?", (device_id,))
        c.execute(
            "INSERT INTO at_usage(device_id, ts, payload) VALUES(?,?,?)",
            (device_id, int(time.time() * 1000), payload),
        )
    return {"ok": True}


@router.get("/anti-theft/usage-data")
async def at_usage_data(device_id: str = Query(...), _: bool = Depends(ctrl_auth)):
    with at_conn() as c:
        row = c.execute(
            "SELECT ts, payload FROM at_usage WHERE device_id=? ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    if row is None:
        return {"ts": None, "usage": [], "foreground": None, "installed": [], "collected_at": None}
    try:
        p = json.loads(row["payload"])
    except Exception:
        return {"ts": row["ts"], "usage": [], "foreground": None, "installed": [], "collected_at": None}
    usage = [u for u in p.get("usage", []) if not _is_system_pkg(u.get("pkg", ""))]
    sessions = [s for s in p.get("sessions", []) if not _is_system_pkg(s.get("pkg", ""))]
    fg = p.get("foreground")
    if fg and _is_system_pkg(fg.get("pkg", "")):
        fg = None
    return {
        "ts": row["ts"],
        "device_id": device_id,
        "usage": usage,
        "sessions": sessions,
        "foreground": fg,
        "installed": p.get("installed", []),
        "collected_at": p.get("collected_at"),
    }


@router.post("/anti-theft/notify-report")
async def at_notify_upload(req: Request, device_id: str = Depends(device_auth)):
    """手机通知监听上报（单条即时上传）。"""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    pkg = (body.get("pkg") or "")[:128]
    if pkg not in _ALLOWED_NOTIFY_PKGS:
        # 白名单之外的通知直接丢弃，不落库（只接收微信/QQ/短信）
        return {"ok": True, "skipped": True, "reason": "pkg_not_in_whitelist"}
    title = _normalize_sender_title((body.get("title") or "")[:256])
    text = (body.get("text") or "")[:1024]
    try:
        ts = int(body.get("ts") or time.time() * 1000)
    except Exception:
        ts = int(time.time() * 1000)
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            "INSERT INTO at_notify(device_id, pkg, title, text, ts) VALUES(?,?,?,?,?)",
            (device_id, pkg, title, text, ts),
        )
    return {"ok": True}


@router.get("/anti-theft/notify-data")
async def at_notify_data(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    limit: int = Query(30),
    offset: int = Query(0),
    pkg: str = Query(None),
    sender: str = Query(None),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """通知消息分页拉取：读哪一页加载哪一页（不拉全量）。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    clauses = ["device_id=?"]
    args = [device_id]
    if pkg:
        clauses.append("pkg=?")
        args.append(pkg)
    if sender:
        clauses.append("title=?")
        args.append(sender)
    if from_ts is not None:
        clauses.append("ts>=?")
        args.append(int(from_ts))
    if to_ts is not None:
        clauses.append("ts<=?")
        args.append(int(to_ts))
    where = " AND ".join(clauses)
    with at_conn() as c:
        rows = c.execute(
            f"SELECT id, pkg, title, text, ts FROM at_notify WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset),
        ).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM at_notify WHERE {where}", tuple(args)).fetchone()[0]
    return {
        "device_id": device_id,
        "items": [
            {"id": r["id"], "pkg": r["pkg"], "title": r["title"], "text": r["text"], "ts": r["ts"]}
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@router.get("/anti-theft/notify-summary")
async def at_notify_summary(
    device_id: str = Query(...),
    _: bool = Depends(ctrl_auth),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
):
    """按包名分组聚合：一级分类（微信/QQ/短信）+ 二级 sender 气泡（名称/总数/未读/最新时间）。"""
    time_clauses = []
    time_args = []
    if from_ts is not None:
        time_clauses.append("ts>=?")
        time_args.append(int(from_ts))
    if to_ts is not None:
        time_clauses.append("ts<=?")
        time_args.append(int(to_ts))
    time_where = (" AND " + " AND ".join(time_clauses)) if time_clauses else ""
    pkgs = ["com.tencent.mm", "com.tencent.mobileqq", "com.tencent.qqlite",
            "com.android.mms", "com.google.android.apps.messaging", "com.samsung.android.messaging"]
    app_name = {"com.tencent.mm": "微信", "com.tencent.mobileqq": "QQ", "com.tencent.qqlite": "QQ轻聊",
                "com.android.mms": "短信", "com.google.android.apps.messaging": "短信",
                "com.samsung.android.messaging": "短信"}
    out = []
    with at_conn() as c:
        for pkg in pkgs:
            cnt = c.execute(
                f"SELECT COUNT(*) FROM at_notify WHERE device_id=? AND pkg=?{time_where}",
                (device_id, pkg, *time_args),
            ).fetchone()[0]
            if not cnt:
                continue
            rows = c.execute(
                f"""SELECT a.title,
                           COUNT(*) AS total,
                           SUM(CASE WHEN a.ts > COALESCE(r.last_seen_ts,0) THEN 1 ELSE 0 END) AS unread,
                           MAX(a.ts) AS last_ts
                    FROM at_notify a
                    LEFT JOIN at_notify_read r ON a.device_id=r.device_id AND a.pkg=r.pkg AND a.title=r.sender
                    WHERE a.device_id=? AND a.pkg=?{time_where}
                    GROUP BY a.title
                    ORDER BY last_ts DESC""",
                (device_id, pkg, *time_args),
            ).fetchall()
            senders = [
                {"title": r["title"], "total": r["total"], "unread": r["unread"] or 0, "last_ts": r["last_ts"]}
                for r in rows
            ]
            out.append({"pkg": pkg, "app_name": app_name.get(pkg, pkg), "count": cnt, "senders": senders})
    # 固定顺序：微信 → QQ → 短信（与前端 NOTIFY_TAB_ORDER 严格对齐）。顺序锁死，不要改；改完通知前端同步。
    order = {"com.tencent.mm": 0, "com.tencent.mobileqq": 1, "com.tencent.qqlite": 1,
             "com.android.mms": 2, "com.google.android.apps.messaging": 2, "com.samsung.android.messaging": 2}
    out.sort(key=lambda x: order.get(x["pkg"], 9))
    return {"device_id": device_id, "apps": out}


@router.post("/anti-theft/notify-read")
async def at_notify_mark_read(req: Request, _device_id: str = Depends(ctrl_auth)):
    """标某 sender 为已读（点进气泡时调用）。UPSERT at_notify_read.last_seen_ts=now。"""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    device_id = body.get("device_id")
    pkg = body.get("pkg")
    sender = body.get("sender")
    if not device_id or not pkg or sender is None:
        raise HTTPException(status_code=400, detail="device_id, pkg, sender required")
    now = int(time.time() * 1000)
    with AT_DB_LOCK, at_conn() as c:
        c.execute(
            """INSERT INTO at_notify_read(device_id, pkg, sender, last_seen_ts)
               VALUES(?,?,?,?)
               ON CONFLICT(device_id, pkg, sender) DO UPDATE SET last_seen_ts=excluded.last_seen_ts""",
            (device_id, pkg, sender, now),
        )
    return {"ok": True}


@router.post("/anti-theft/notify-delete")
async def at_notify_delete(req: Request, _device_id: str = Depends(ctrl_auth)):
    """批量删除通知线索：{ids:[..]} 或 {device_id, all:true} 清空某设备，或 {device_id, pkg} 清空某软件，或 {device_id, pkg, sender} 删某好友全部。"""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    ids = body.get("ids") or []
    all_for_device = body.get("all") is True
    target_device = body.get("device_id")
    by_pkg_sender = body.get("pkg") and body.get("sender") is not None
    by_pkg = bool(body.get("pkg")) and target_device
    with AT_DB_LOCK, at_conn() as c:
        if all_for_device and target_device:
            cur = c.execute("DELETE FROM at_notify WHERE device_id=?", (target_device,))
            deleted = cur.rowcount
        elif by_pkg_sender and target_device:
            cur = c.execute(
                "DELETE FROM at_notify WHERE device_id=? AND pkg=? AND title=?",
                (target_device, body.get("pkg"), body.get("sender")),
            )
            deleted = cur.rowcount
        elif by_pkg and target_device:
            cur = c.execute(
                "DELETE FROM at_notify WHERE device_id=? AND pkg=?",
                (target_device, body.get("pkg")),
            )
            deleted = cur.rowcount
        elif isinstance(ids, list) and ids:
            # 仅保留整数 id，防注入
            safe_ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
            if not safe_ids:
                raise HTTPException(status_code=400, detail="no valid ids")
            placeholders = ",".join(["?"] * len(safe_ids))
            cur = c.execute(f"DELETE FROM at_notify WHERE id IN ({placeholders})", safe_ids)
            deleted = cur.rowcount
        else:
            raise HTTPException(status_code=400, detail="ids or all+device_id or pkg[+sender] required")
    return {"ok": True, "deleted": deleted}


@router.get("/anti-theft/status")
async def at_status(device_id: str = Query(...), _: bool = Depends(ctrl_auth)):
    with at_conn() as c:
        row = c.execute("SELECT remark, model, last_seen FROM at_devices WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    last = row["last_seen"] or 0
    online = (time.time() * 1000 - last) < 20 * 60 * 1000  # 20 分钟内上报算在线
    with at_conn() as c:
        loc = c.execute(
            "SELECT lat, lng, addr FROM at_locations WHERE device_id=? ORDER BY id DESC LIMIT 1", (device_id,)
        ).fetchone()
    # 优先用手机端腾讯 SDK 返回的地址；手机未上报时才走后端逆地理
    addr = None
    if loc:
        addr = loc["addr"] or get_addr(loc["lat"], loc["lng"])
    return {"device_id": device_id, "remark": row["remark"], "model": row["model"],
            "online": online, "last_seen": last, "addr": addr,
            "realtime": device_id in at_ws}


def _console_html() -> str:
    """网页控制台：单文件内嵌 JS，token 存 localStorage，不硬编码 CTRL_TOKEN。

    地图 JS 的高德 Key 从环境变量 AMAP_JS_KEY 注入（与逆地理用的 AMAP_WEB_KEY 是两个 Key：
    前者是 Web 端 JS API Key，后者是 Web 服务 Key）。未配置时注入占位符，
    控制台仍可正常使用，只是不显示地图底图。
    """
    html = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>查找手机控制台</title>
<style>
  :root{--primary:#3b6cff;--danger:#d6453b;--bg:#f2f4f8;--card:#fff;--text:#1f2937;--muted:#6b7280;--line:#e5e7eb;--radius:12px}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--text);line-height:1.5}
  header{position:sticky;top:0;z-index:5;padding:12px 20px;background:linear-gradient(90deg,#1f2a44,#2d3d5e);color:#fff;display:flex;gap:10px;align-items:center;box-shadow:0 2px 8px rgba(0,0,0,.12);transition:max-height .25s ease,padding .25s ease,opacity .25s ease}
  header h1{font-size:18px;margin:0;flex:1;letter-spacing:.5px}
  input,button{font-size:14px;padding:9px 14px;border-radius:8px;border:1px solid var(--line)}
  input{flex:1;min-width:180px;max-width:420px;background:#fff}
  button{background:var(--primary);color:#fff;border:none;cursor:pointer;transition:transform .05s,opacity .15s}
  button:active{transform:scale(.97)}
  button.danger{background:var(--danger)}
  button.ghost{background:#f3f4f6;color:var(--text)}
  button.ghost.active{background:var(--primary);color:#fff;border-color:var(--primary)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .wrap{display:flex;gap:16px;padding:16px;max-width:1400px;margin:0 auto;align-items:flex-start}
  #devPanel{flex:none;width:280px;background:var(--card);border-radius:var(--radius);padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.06);transition:width .2s ease,transform .2s ease,padding .2s ease;position:relative;overflow:hidden}
  #devPanel.collapsed{width:0;padding:0;border-radius:0;overflow:visible;background:transparent;box-shadow:none}
  #devPanel.collapsed .dev-panel-inner{opacity:0;pointer-events:none;visibility:hidden}
  #detailPanel{flex:1;min-width:0;background:var(--card);border-radius:var(--radius);padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  .dev-toggle-btn{width:32px;height:32px;border-radius:6px;background:rgba(255,255,255,.92);color:#1f2937;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:15px;padding:0;box-shadow:0 1px 3px rgba(0,0,0,.2);flex:none}
  #devOverlay{display:none}
  #headerToggle{display:none;align-items:center;gap:4px;font-size:12px;padding:6px 10px;min-height:32px}
  #fabRefresh{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:26px;box-shadow:0 4px 14px rgba(59,108,255,.45);z-index:30;cursor:pointer;user-select:none;touch-action:none;transition:transform .1s,box-shadow .1s}
  #fabRefresh:active{transform:scale(.92);box-shadow:0 2px 8px rgba(59,108,255,.35)}
  .section-title{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin:0 0 10px}
  .dev{display:flex;align-items:center;gap:12px;padding:12px;border:1px solid var(--line);border-radius:10px;margin-bottom:10px;cursor:pointer;transition:border-color .15s,background .15s}
  .dev:hover{border-color:#c7d2fe;background:#f8f9ff}
  .dev.sel{border-color:var(--primary);background:#eef3ff}
  .dev .dot{flex-shrink:0;width:10px;height:10px;border-radius:50%;background:#bbb}
  .dev .dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.15)}
  .dev .info{flex:1;min-width:0}
  .dev .name{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dev .sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
  .dev .arrow{color:#9ca3af;font-size:12px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .on{background:#22c55e}.off{background:#9ca3af}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
  th{font-weight:600;color:var(--muted);font-size:12px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-top:10px}
  .grid img{width:100%;border-radius:8px;border:1px solid var(--line);cursor:pointer;transition:transform .15s}
  .grid img:hover{transform:scale(1.02)}
  #pgrid{max-height:340px;overflow-y:auto;padding-right:6px}
  .photo-card{position:relative;display:inline-flex;flex-direction:column;border-radius:8px;overflow:hidden;border:1px solid var(--line);cursor:pointer;background:var(--card)}
  .photo-card img{display:block;width:100%;height:180px;object-fit:cover;object-position:center}
  .photo-check{position:absolute;top:4px;left:4px;width:20px;height:20px;display:none;z-index:4;cursor:pointer;accent-color:#2d8cf0}
  #pgrid.batch .photo-check,#agrid.batch .photo-check{display:block}
  #pgrid.batch .photo-card,#agrid.batch .photo-card{cursor:default}
  .photo-card.sel{outline:2px solid #2d8cf0;outline-offset:-2px}
  .hd-badge{position:absolute;top:6px;right:6px;background:#2d8cf0;color:#fff;font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;z-index:2;pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,.3)}
  .selall{display:inline-flex;align-items:center;gap:4px;font-size:13px;color:var(--muted);white-space:nowrap}
  .batchBar{display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap}
  .batchCount{font-size:13px;color:var(--muted);white-space:nowrap;min-width:48px;text-align:center}
  .photo-ts{font-size:11px;color:var(--muted);padding:4px 6px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:#f8f9fa;border-top:1px solid var(--line)}
  #agrid .photo-card{display:block;width:100%}
  #agrid .photo-card img{width:100%;height:180px;object-fit:cover;object-position:center;display:block}
  #lightbox{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;align-items:center;justify-content:center;z-index:100;flex-direction:column}
  #lightbox.open{display:flex}
  #lightbox img{max-width:92vw;max-height:82vh;object-fit:contain;border-radius:4px;box-shadow:0 20px 60px rgba(0,0,0,.5);transform-origin:50% 50%;will-change:transform;cursor:zoom-in;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-user-drag:none}
  /* 控件层独立接收事件，且不被拖动时穿透 */
  #lightbox button,#lightbox #lbInfo,#lightbox #lbHint,#lightbox #lbZoom{pointer-events:auto}
  /* 拖动期间关闭 img 上的 transition，避免 translate 动画拖泥带水 */
  #lightbox img.dragging{transition:none!important}
  #lightbox img.zoomed{cursor:grab}
  #lightbox img.grabbing{cursor:grabbing}
  #lbClose{position:absolute;top:16px;right:16px;width:40px;height:40px;border-radius:50%;border:none;background:rgba(255,255,255,.15);color:#fff;font-size:24px;cursor:pointer;display:flex;align-items:center;justify-content:center}
  #lbClose:hover{background:rgba(255,255,255,.3)}
  .lbNav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:64px;border:none;background:rgba(255,255,255,.12);color:#fff;font-size:22px;cursor:pointer;display:flex;align-items:center;justify-content:center;border-radius:8px}
  .lbNav:hover{background:rgba(255,255,255,.28)}
  #lbReset{position:absolute;bottom:18px;left:50%;transform:translateX(-50%);padding:8px 16px;border-radius:20px;border:none;background:rgba(255,255,255,.18);color:#fff;font-size:13px;cursor:pointer;display:none;align-items:center;gap:6px;backdrop-filter:blur(4px)}
  #lbReset:hover{background:rgba(255,255,255,.32)}
  #lbOrig{position:absolute;bottom:62px;left:50%;transform:translateX(-50%);padding:8px 16px;border-radius:20px;border:none;background:rgba(45,140,240,.85);color:#fff;font-size:13px;cursor:pointer;display:none;align-items:center;gap:6px;backdrop-filter:blur(4px);white-space:nowrap}
  #lbOrig:hover{background:rgba(45,140,240,1)}
  #lbOrig:disabled{opacity:.7;cursor:default}
  #lbZoom{position:absolute;bottom:18px;right:16px;padding:6px 12px;border-radius:16px;background:rgba(0,0,0,.55);color:#fff;font-size:13px;pointer-events:none;font-variant-numeric:tabular-nums}
  #lbInfo{bottom:18px;left:16px;max-width:60vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #lbHint{position:absolute;top:16px;left:50%;transform:translateX(-50%);padding:6px 14px;border-radius:16px;background:rgba(0,0,0,.5);color:#fff;font-size:12px;pointer-events:none;opacity:0;transition:opacity .3s}
  #lbHint.show{opacity:1}
  #lbPrev{left:12px}
  #lbNext{right:12px}
  #lbInfo{position:absolute;bottom:16px;left:50%;transform:translateX(-50%);color:#fff;font-size:13px;background:rgba(0,0,0,.5);padding:6px 14px;border-radius:20px;white-space:nowrap}
  @media (max-width:768px){
    .lbNav{width:36px;height:52px;font-size:18px}
    #lbInfo{font-size:12px;padding:5px 10px}
  }
  .ops{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
  .hint{font-size:12px;color:var(--muted);margin:8px 0}
  .hint a{color:var(--primary)}
  .card{border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:12px;background:#fafbfc}
  .card-title{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:10px}
  .danger-zone{margin-top:12px;border:1px solid #fecaca;border-radius:10px;padding:12px;background:#fef2f2}
  .danger-zone summary{cursor:pointer;color:var(--danger);font-weight:600;user-select:none;list-style:none}
  .danger-zone summary::before{content:"▶";display:inline-block;margin-right:6px;font-size:10px;transition:transform .2s}
  .danger-zone[open] summary::before{transform:rotate(90deg)}
  .danger-zone .ack{display:block;margin:10px 0;font-size:13px}
  .danger-zone input[type=text],.danger-zone input:not([type]){width:100%;margin:6px 0}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:20;backdrop-filter:blur(2px)}
  .modal .box{background:var(--card);padding:22px;border-radius:var(--radius);max-width:360px;width:90%;box-shadow:0 20px 40px rgba(0,0,0,.2)}
  .modal .ops{justify-content:flex-end;margin-top:16px}
  #notifyList{border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden;margin:6px 0 4px}
  .nitem{position:relative;padding:9px 14px;border-bottom:1px solid var(--line);background:transparent;min-height:0;display:flex;align-items:flex-start;gap:10px;transition:background-color .12s}
  .nitem:last-child{border-bottom:none}
  #notifyList.batch .nitem{cursor:pointer}
  #notifyList.batch .nitem:hover{background-color:#f5f7fb}
  .nitem.sel{background-color:#fff1f0}
  .nitem .nbody{flex:1;min-width:0;display:flex;align-items:baseline;flex-wrap:wrap;gap:6px;font-size:14px;line-height:1.45;color:var(--text)}
  .nitem .nname{font-weight:600;color:#111827;white-space:nowrap}
  .nitem .nsep{color:var(--muted);font-weight:400}
  .nitem .ntext{word-break:break-word;white-space:pre-wrap}
  .nitem .ntime{flex:none;font-size:12px;color:var(--muted);white-space:nowrap;margin-top:2px}
  .batchBar .delNotify{order:3}
  .batchBar .clearNotify{order:4}
  .qzone{margin-top:12px;border:1px solid var(--line);border-radius:10px;padding:12px;background:#fafbfc}
  .qzone summary{cursor:pointer;font-weight:600;user-select:none;list-style:none;font-size:14px}
  .qzone summary::before{content:"▶";display:inline-block;margin-right:6px;font-size:10px;transition:transform .2s}
  .qzone[open] summary::before{transform:rotate(90deg)}
  .qrow{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid #f3f4f6;font-size:13px}
  .qrow:last-child{border-bottom:none}
  .qrow .meta{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .qrow .st{font-size:11px;padding:2px 8px;border-radius:12px;white-space:nowrap;font-weight:500}
  .st.pending{background:#dbeafe;color:#1e40af}
  .st.dispatched{background:#f3f4f6;color:#6b7280}
  .empty{text-align:center;padding:30px 20px;color:var(--muted);font-size:13px}
  /* 通知气泡层 */
  .nbub-wrap{position:relative;display:flex;align-items:center;gap:8px;margin:6px 0 14px}
  .nbub-scroll{display:flex;gap:10px;overflow-x:auto;padding:6px 2px;scroll-snap-type:x proximity;flex:1;min-width:0}
  .nbub-scroll::-webkit-scrollbar{height:6px}
  .nbub-scroll::-webkit-scrollbar-thumb{background:#c7cdd6;border-radius:3px}
  .nbub-scroll::-webkit-scrollbar-thumb:hover{background:#9aa3af}
  .nbub-arrow{background:var(--card);border:1px solid var(--line);border-radius:50%;width:30px;height:30px;flex:none;cursor:pointer;font-size:16px;color:var(--muted);display:flex;align-items:center;justify-content:center;user-select:none}
  .nbub-arrow:hover{border-color:var(--primary);color:var(--primary)}
  .nbub{position:relative;flex:none;width:fit-content;min-width:96px;max-width:100%;scroll-snap-align:start;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;cursor:pointer;transition:border-color .15s,box-shadow .15s;text-align:center}
  .nbub:hover{border-color:var(--primary);box-shadow:0 2px 8px rgba(59,108,255,.12)}
  .nbub.unread{border-color:#ff4d4f}
  .nbub .nb-name{font-weight:600;font-size:14px;color:#111827;white-space:nowrap;display:inline-block}
  .nbub .nb-count{font-size:12px;color:var(--muted);margin-top:6px;white-space:nowrap;display:inline-block}
  .nbub .nb-badge{position:absolute;top:-7px;right:-7px;min-width:18px;height:18px;padding:0 4px;background:#ff4d4f;color:#fff;border-radius:10px;font-size:11px;line-height:18px;text-align:center;font-weight:700;box-shadow:0 0 0 2px var(--card)}
  .nbub-clear{margin-left:6px}
  /* 消息列表头部：永久保留 sender 气泡，气泡层视觉一致；右侧按需放动作按钮 */
  .nlist-head{display:flex;align-items:center;gap:10px;margin:8px 0 12px;flex-wrap:wrap}
  .nlist-head .nbub{cursor:default;min-width:120px;max-width:100%}
  .nlist-head .nbub.active{border-color:var(--primary);box-shadow:0 0 0 2px rgba(59,108,255,.18)}
  /* 消息列表分页器 */
  .npager{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
  .npager button{padding:5px 10px;border:1px solid var(--line);background:var(--card);border-radius:7px;cursor:pointer;font-size:13px;color:var(--text)}
  .npager button:hover{border-color:var(--primary)}
  .npager button.cur{border-color:var(--primary);background:var(--primary);color:#fff;font-weight:600}
  .npager button:disabled{opacity:.4;cursor:not-allowed}
  .npager .np-info{font-size:12px;color:var(--muted);margin-right:auto}
  .npager .np-go{display:inline-flex;align-items:center;gap:4px}
  .npager .np-go input{width:56px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;font-size:13px}
  /* Go 弹窗 */
  .gomodal{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;align-items:center;justify-content:center;z-index:50}
  .gomodal.show{display:flex}
  .gomodal .gbox{background:var(--card);padding:20px 22px;border-radius:12px;width:300px;box-shadow:0 20px 40px rgba(0,0,0,.25)}
  .gomodal .grow{display:flex;align-items:center;gap:8px;margin:14px 0}
  .gomodal .grow input{flex:1;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:15px}
  .gomodal .gbtns{display:flex;gap:8px;justify-content:flex-end}
  .gomodal .ghint{font-size:12px;color:var(--muted);margin-top:6px}
  .nlist-head{display:flex;align-items:center;gap:10px;margin:4px 0 8px;flex-wrap:wrap}
  .nlist-head .back{background:transparent;border:1px solid var(--line);border-radius:7px;padding:4px 10px;cursor:pointer;font-size:13px}
  /* 当前选中好友/群聊的纯文本标题：加粗黑色 + 灰色"共 N 条"，一行展示 */
  .nb-selected{display:inline-flex;align-items:baseline;gap:8px;padding:4px 2px}
  .nb-selected .nb-selected-name{font-weight:700;font-size:18px;color:#111827;white-space:nowrap}
  .nb-selected .nb-selected-meta{font-size:13px;color:var(--muted);white-space:nowrap}
  .detail-header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px}
  .detail-title h2{margin:0;font-size:20px;font-weight:700}
  .ctl-group{display:flex;align-items:center;gap:10px;margin:8px 0;flex-wrap:wrap}
  .ctl-label{flex:none;min-width:48px;font-size:12px;font-weight:600;color:var(--muted)}
  .ctl-group .ops{margin:0;flex:1;min-width:0}
  .dev-toggle-m{display:none}
  .dev-panel-hd{display:none}
  .loc-list{display:none}
  .loc-item{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;background:#fff;font-size:13px}
  .loc-item .row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f3f4f6;gap:8px}
  .loc-item .row:last-child{border-bottom:none}
  .loc-item .k{color:var(--muted);font-size:12px;flex:none}
  .loc-item .v{word-break:break-all;text-align:right;flex:1;min-width:0}
  @media (max-width:768px){
    header{flex-wrap:wrap;padding:0 12px;max-height:0;overflow:hidden;opacity:0}
    header.open{max-height:220px;padding:10px 12px;opacity:1}
    header h1{width:100%;flex:none;font-size:16px;margin-bottom:8px}
    header input{flex:1 1 100%;min-width:0;max-width:none;margin:0}
    header button{flex:1;padding:10px;min-height:44px}
    #headerToggle{display:inline-flex}
    .wrap{padding:10px}
    .col{padding:12px}
    .detail-header{flex-direction:column;gap:10px}
    .detail-header>div:last-child{display:flex;width:100%;gap:8px}
    .detail-header button{flex:1;min-height:44px}
    .detail-title h2{font-size:18px}
    .ctl-group{flex-direction:column;align-items:stretch;gap:8px;margin:10px 0}
    .ctl-label{min-width:auto;font-size:12px}
    .ops button,.ctl-group button{flex:1 1 auto;min-height:40px;padding:8px 10px;font-size:12px}
    .batchBar{width:100%;gap:8px}
    .batchBar button{flex:1;min-height:40px;padding:8px 10px;font-size:13px}
    .batchCount{flex:1;text-align:center}
    .loc-table{display:none}
    .loc-list{display:block}
    .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
    .photo-card img{width:100%;height:120px;object-fit:cover;object-position:center}
    .wrap{padding:0;gap:0}
    #devPanel{position:fixed;left:0;top:0;bottom:0;width:280px;z-index:25;border-radius:0;padding:12px;transform:translateX(-100%);transition:transform .25s ease}
    #devPanel.open{transform:translateX(0)}
    #devPanel.collapsed{width:280px}
    #devPanel.collapsed .dev-panel-inner{opacity:1;pointer-events:auto;visibility:visible}
    .dev-toggle-btn{width:28px;height:28px;font-size:14px;background:rgba(255,255,255,.92)}
    #detailPanel{border-radius:0;padding:12px;min-height:100vh}
    #devOverlay{display:block;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:20;opacity:0;pointer-events:none;transition:opacity .25s ease}
    #devOverlay.open{opacity:1;pointer-events:auto}
    .dev-toggle-m{display:flex;gap:10px;align-items:center;padding:10px 12px;background:var(--card);border-bottom:1px solid var(--line)}
    .dev-toggle-m button{flex:none;min-height:40px;padding:8px 14px}
    .dev-toggle-m span{font-size:13px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .dev-panel-hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
    .dev-panel-hd strong{font-size:16px}
    .dev-panel-hd button{min-height:36px}
    .modal .box{padding:18px;width:92%}
    .nitem{padding:7px 11px;gap:8px}
    .nitem .ntime{font-size:11px}
    .nitem .nname,.nitem .ntext{font-size:13px}
    .batchBar .delNotify{order:4}
    .batchBar .clearNotify{order:3}
  }
.uv-wrap{max-width:1100px;margin:0 auto;padding:16px}
.uv-head{position:sticky;top:0;z-index:5;padding:12px 16px;background:linear-gradient(90deg,#1f2a44,#2d3d5e);color:#fff;display:flex;gap:10px;align-items:center}
.uv-head span{flex:1;font-size:13px;opacity:.85}
.uv-tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:12px;flex-wrap:wrap}
.uv-tab{background:transparent;border:none;padding:9px 14px;cursor:pointer;font-size:14px;color:var(--muted);border-radius:8px 8px 0 0;border-bottom:2px solid transparent}
.uv-tab.active{color:var(--primary);font-weight:600;border-bottom-color:var(--primary)}
.uv-tab-content{display:none}
.uv-tab-content.active{display:block}
.uv-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;max-height:60vh;overflow:auto}
.uv-item{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.uv-item .l{font-weight:600}
.uv-item .p{color:var(--muted);font-size:12px;word-break:break-all}
.uv-fg{font-size:15px}
.uv-row{cursor:pointer}
.uv-row:hover td{background:#f7f9fc}
.uv-row.open td{background:#eef3ff}
.uv-sessions td{padding:0;border-bottom:none;background:#fafbfc}
.uv-sessions .inner{padding:10px 14px}
.uv-sessions table{width:100%;border-collapse:collapse;font-size:12px}
.uv-sessions th,.uv-sessions td{padding:6px 8px;border-bottom:1px solid var(--line)}
.uv-sessions th{color:var(--muted);font-weight:600;background:#f4f6fa;text-align:left}
.uv-sessions td.num{text-align:right}
.uv-sessions .hint{color:var(--muted);font-size:12px;margin-top:6px}
@media (max-width:768px){.uv-wrap{padding:12px}.uv-sessions .inner{padding:8px}.uv-sessions th,.uv-sessions td{padding:5px 6px;font-size:11px}}
</style>
<script>
  // JS API 2.0 安全密钥配置（2021-12-02 之后创建的 Key 必须配合使用）。
  // securityJsCode 从环境变量 AMAP_JS_CODE 注入；未配置时该对象为空，
  // 地图仍能加载，但高德可能拒绝部分需要后端的能力。
  window._AMapSecurityConfig = { securityJsCode: "{AMAP_JS_CODE}" };
</script>
<script src="https://webapi.amap.com/maps?v=2.0&key={AMAP_JS_KEY}"></script>
</head>
<body>
<header>
  <button class="dev-toggle-btn" onclick="toggleDevPanel()" title="展开/折叠设备栏">☰</button>
  <h1>查找手机控制台</h1>
  <input id="tok" placeholder="控制端 Token (Bearer)" type="password">
  <button onclick="saveTok()">保存</button>
  <button class="ghost" onclick="loadDevices()">刷新设备</button>
</header>
<div class="dev-toggle-m"><button onclick="toggleDevPanel(true)">☰ 选择设备</button><span id="devToggleLabel">未选择设备</span><button class="ghost" id="headerToggle" onclick="toggleHeader()">Token ▼</button></div>
<div id="devOverlay" onclick="toggleDevPanel(false)"></div>
<div id="mainView"><div class="wrap">
  <div id="devPanel">
    <div class="dev-panel-inner">
      <div class="dev-panel-hd"><strong>选择设备</strong></div>
      <div class="hint">点击设备查看操作面板</div>
      <div id="devs"></div>
    </div>
  </div>
  <div id="detailPanel">
    <div id="detail"><div class="hint">未选择设备</div></div>
  </div>
</div>
</div>
<div id="usageView" style="display:none">
  <div class="uv-head"><button class="ghost" onclick="hideUsage()">← 返回控制台</button><span id="uvDev"></span></div>
    <div class="uv-wrap">
    <div class="card">
      <div class="ops">
        <button onclick="requestUsage()">获取/刷新数据</button>
        <button class="ghost" onclick="u_loadData()">重新载入</button>
        <span id="uvStatus" class="hint"></span>
      </div>
      <div class="hint">数据由手机端「应用使用情况」指令采集后上传（手动触发，不自动上报）。点击「获取/刷新数据」下发指令，手机（在线/WS 秒级，离线最多 15 分钟）响应后，再点「重新载入」。</div>
      <div id="uvWarning" style="display:none;margin-top:10px;padding:10px 12px;border-radius:8px;background:#fff3e0;color:#bf360c;font-size:13px;line-height:1.5"></div>
    </div>
    <div class="card">
      <div class="uv-tabs">
        <button class="uv-tab active" id="tab-ua" onclick="u_switchTab('ua')">使用时长 / 前台 App</button>
        <button class="uv-tab" id="tab-inst" onclick="u_switchTab('inst')">已装 App</button>
      </div>
      <div class="uv-tab-content active" id="content-ua">
        <div class="ops">
          <span class="hint" style="margin:0">时间范围：</span>
          <select id="rangeSel" onchange="renderUsage()">
            <option value="today">今天</option>
            <option value="yesterday">昨天</option>
            <option value="3d">近三天</option>
            <option value="week">本周</option>
            <option value="lastweek">上一周</option>
            <option value="month">近一个月</option>
            <option value="lastmonth">上个月</option>
          </select>
          <span class="hint" id="usageSummary" style="margin:0"></span>
        </div>
        <div id="usageTable"></div>
        <div id="fgBox" style="margin-top:16px"></div>
      </div>
      <div class="uv-tab-content" id="content-inst">
        <div class="ops"><input type="text" id="instFilter" placeholder="筛选应用名/包名" oninput="renderInstalled()" style="flex:1;min-width:160px"></div>
        <div id="instBox"><div class="empty">暂无数据</div></div>
      </div>
    </div>
  </div>
</div>
<div class="modal" id="modal"><div class="box">
  <div id="modalText"></div>
  <div class="ops"><button class="ghost" onclick="closeModal()">取消</button><button id="modalOk" class="danger" onclick="modalOk()">确认</button></div>
</div></div>
<div class="modal" id="wipeModal"><div class="box">
  <div style="font-weight:700;color:#d6453b">⚠ 最终警告：擦除不可撤销</div>
  <div class="hint">此操作将<b>恢复出厂设置</b>，所有数据将被清除且无法恢复。</div>
  <label class="ack">请输入「擦除」二字以解锁确认按钮：</label>
  <input id="wipePhrase" oninput="wipeCheck()" placeholder="擦除">
  <label class="ack"><input type="checkbox" id="wipeFinalAck" onchange="wipeCheck()"> 我确认要擦除此设备</label>
  <div class="ops"><button class="ghost" onclick="closeWipeModal()">取消</button><button id="wipeFinal" class="danger" disabled onclick="doWipe()">5 秒后可确认</button></div>
</div></div>
<div class="modal" id="delDevModal"><div class="box">
  <div style="font-weight:700;color:#d6453b">⚠ 删除设备：<span id="delDevName"></span></div>
  <div class="hint">将从服务器<b>永久删除</b>此设备的所有位置、照片（含磁盘文件）、指令记录，<b>不可恢复</b>。删除后该手机将无法再上报。</div>
  <label class="ack">请输入「删除」二字以解锁确认按钮：</label>
  <input id="delDevPhrase" oninput="delDevCheck()" placeholder="删除">
  <label class="ack"><input type="checkbox" id="delDevAck" onchange="delDevCheck()"> 我确认要删除此设备</label>
  <div class="ops"><button class="ghost" onclick="closeDelDevModal()">取消</button><button id="delDevFinal" class="danger" disabled onclick="doDelDev()">5 秒后可确认</button></div>
</div></div>
<div class="modal" id="cleanupModal"><div class="box">
  <div style="font-weight:700;color:#d6453b">⚠ 数据清理确认：<span id="cleanupDevName"></span></div>
  <div class="hint" id="cleanupPreview">正在预估影响…</div>
  <label class="ack">请输入「清理」二字以解锁确认按钮：</label>
  <input id="cleanupPhrase" oninput="cleanupCheck()" placeholder="清理">
  <label class="ack"><input type="checkbox" id="cleanupFinalAck" onchange="cleanupCheck()"> 我确认要删除上述旧数据（不可恢复）</label>
  <div class="ops"><button class="ghost" onclick="closeCleanupModal()">取消</button><button id="cleanupFinal" class="danger" disabled onclick="doCleanup()">5 秒后可确认</button></div>
</div></div>
<div class="modal" id="albumClearModal"><div class="box">
  <div style="font-weight:700;color:#d6453b">⚠ 相册清空确认：<span id="albumClearDevName"></span></div>
  <div class="hint" id="albumClearPreview">正在预估影响…</div>
  <label class="ack">请输入「清空」二字以解锁确认按钮：</label>
  <input id="albumClearPhrase" oninput="albumClearCheck()" placeholder="清空">
  <label class="ack"><input type="checkbox" id="albumClearAck" onchange="albumClearCheck()"> 我确认要永久删除上述相册图（不可恢复）</label>
  <label class="ack"><input type="checkbox" id="albumClearResetBaseline"> 同时重置手机基线（删除后让手机遗忘这些照片，下次同步会重新上传）</label>
  <div class="ops"><button class="ghost" onclick="closeAlbumClearModal()">取消</button><button id="albumClearFinal" class="danger" disabled onclick="doAlbumClear()">5 秒后可确认</button></div>
</div></div>
<div class="modal" id="syncAlbumModal"><div class="box">
  <div style="font-weight:700"><span id="syncAlbumTitle">📅 同步相册时间窗</span>：<span id="syncAlbumDevName"></span></div>
  <div class="hint" id="syncAlbumHint">请选择本次同步的时间段（必须指定才能同步，避免误传整个媒体库）。<b>“全部”即同步所有照片</b>，请确认清楚。</div>
  <div class="ops" style="flex-wrap:wrap" id="syncAlbumPresets">
    <button class="ghost" data-r="all" onclick="syncAlbumPick('all')">全部</button>
    <button class="ghost" data-r="today" onclick="syncAlbumPick('today')">今天</button>
    <button class="ghost" data-r="yesterday" onclick="syncAlbumPick('yesterday')">昨天</button>
    <button class="ghost" data-r="3d" onclick="syncAlbumPick('3d')">近三天</button>
    <button class="ghost" data-r="week" onclick="syncAlbumPick('week')">近7天</button>
    <button class="ghost" data-r="month" onclick="syncAlbumPick('month')">近30天</button>
  </div>
  <div class="hint" style="margin-top:8px">自定义时间段：</div>
  <div class="ops" style="flex-wrap:wrap">
    <span class="hint" style="margin:0 4px">从</span>
    <input type="datetime-local" id="saFrom" onchange="syncAlbumCustom()">
    <span class="hint" style="margin:0 4px">至</span>
    <input type="datetime-local" id="saTo" onchange="syncAlbumCustom()">
    <button class="ghost" onclick="syncAlbumClearCustom()">清除</button>
  </div>
  <div style="margin-top:10px;padding:10px 12px;background:#0f172a;border-radius:6px;color:#7ee787;font-weight:700;font-size:14px" id="syncAlbumDesc">尚未选择时间段</div>
  <div class="ops"><button class="ghost" onclick="closeSyncAlbumModal()">取消</button><button id="syncAlbumGo" class="danger" disabled onclick="submitAlbumModal()">请先选择时间段</button></div>
</div></div>
<div id="notifyView" style="display:none">
  <div class="uv-head"><button class="ghost" onclick="hideNotify()">← 返回控制台</button><span id="ntDev"></span></div>
  <div class="uv-wrap">
    <div class="card">
      <div class="ops">
        <button onclick="loadNotify()">刷新</button>
        <button id="batchBtnNotify" onclick="toggleNotifyBatch()">批量操作</button>
        <span id="batchBarNotify" class="batchBar" style="display:none">
          <label class="selall"><input type="checkbox" id="selAllNotify" onchange="selAllNotifyPage(this.checked)"> 全选本页</label>
          <button class="ghost" onclick="selAllNotifySender()">全选整个好友</button>
          <span id="batchCountNotify" class="batchCount">已选 0</span>
          <button class="danger clearNotify" onclick="clearNotify()">清空所有软件消息</button>
          <button class="danger delNotify" onclick="delNotifySel()">删除</button>
          <button class="ghost" onclick="toggleNotifyBatch()">取消</button>
        </span>
        <button class="ghost" onclick="toggleNotifyFilter()" style="margin-left:auto">📅 时间筛选 ▾</button>
      </div>
      <div id="notifyFilter" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
        <div class="ops" style="flex-wrap:wrap">
          <button class="ghost" onclick="setNotifyRange('today')">今天</button>
          <button class="ghost" onclick="setNotifyRange('yesterday')">昨天</button>
          <button class="ghost" onclick="setNotifyRange('3d')">近三天</button>
          <button class="ghost" onclick="setNotifyRange('week')">近7天</button>
          <button class="ghost" onclick="setNotifyRange('month')">近30天</button>
          <button class="ghost" onclick="setNotifyRange('all')">全部</button>
          <span class="hint" style="margin:0 4px">自定义：</span>
          <input type="datetime-local" id="nfFrom" onchange="applyNotifyFilter()">
          <span class="hint" style="margin:0 4px">至</span>
          <input type="datetime-local" id="nfTo" onchange="applyNotifyFilter()">
          <button class="ghost" onclick="clearNotifyFilter()">清除筛选</button>
        </div>
      </div>
      <div class="hint">通知监听捕获的系统通知（含微信/QQ 等聊天预览）。按软件（微信/QQ/短信）→ 好友/群聊（气泡）两级分类，点气泡进消息列表。只能拿到通知栏展示内容（发件人+预览文本）。</div>
    </div>
    <div class="card">
      <div class="uv-tabs" id="notifyTabs"></div>
      <div id="notifyTabContent"><div class="hint">加载中…</div></div>
    </div>
  </div>
  <div class="gomodal" id="goModal">
    <div class="gbox">
      <div style="font-weight:600;font-size:15px">跳转到指定页码</div>
      <div class="grow"><span>到达</span><input type="number" id="goPageInput2" min="1" placeholder="页码"><span>页</span></div>
      <div class="ghint" id="goHint"></div>
      <div class="gbtns"><button class="ghost" onclick="closeGoModal()">取消</button><button onclick="goJump()">确认跳转(可回车)</button></div>
    </div>
  </div>
</div>
<div id="fabRefresh" title="刷新">⟳</div>
<div id="lightbox" onclick="closeLightbox(event)" ondragstart="return false">
  <button id="lbClose" onclick="closeLightbox(event)" title="关闭">×</button>
  <button id="lbPrev" class="lbNav" onclick="changeLightbox(-1,event)" title="上一张">‹</button>
  <img id="lbImg" src="" alt="大图" draggable="false">
  <button id="lbNext" class="lbNav" onclick="changeLightbox(1,event)" title="下一张">›</button>
  <div id="lbInfo"></div>
  <button id="lbReset" onclick="resetZoom(event)" title="还原大小">↻ 还原</button>
  <button id="lbOrig" onclick="requestOriginal()" title="查看高清原图">📥 查看原图</button>
  <div id="lbZoom">100%</div>
  <div id="lbHint">滚轮 / 双指缩放 · 拖动平移 · 双击放大 · ← → 翻页</div>
</div>
<script>
const LS='at_ctrl_token';
let TOKEN=localStorage.getItem(LS)||'';
let SEL=null;
const loadedPhotos=new Set();
const selPhotos=new Set();
const selAlbums=new Set();
let BATCH={photo:false,album:false};
let PHOTO_LIST=[];
let ALBUM_LIST=[];
let LB_STATE={type:'photo',idx:0,open:false};
if(TOKEN)document.getElementById('tok').value=TOKEN;
function hdr(){return{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'}}
async function api(p,o={}){o.headers=Object.assign(hdr(),o.headers||{});const r=await fetch(p,o);if(!r.ok)throw new Error(r.status+' '+await r.text());return r}
async function apiPost(p,body){const r=await fetch(p,{method:'POST',headers:hdr(),body:JSON.stringify(body||{})});if(!r.ok)throw new Error(r.status+' '+await r.text());return r.json();}
function fmt(ts){if(!ts)return'';return new Date(ts).toLocaleString()}
function esc(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function regEsc(s){return String(s==null?'':s).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
function stripNotifyText(raw,senderName){let t=String(raw==null?'':raw);t=t.replace(/^[\s\u3000]*\[\s*\d+\s*条\s*\][\s\u3000]*/,'');if(senderName){const safe=regEsc(senderName);t=t.replace(new RegExp('^[\\s\u3000]*'+safe+'[\\s\u3000]*[:：][\\s\u3000]*'),'');}return t.trim()}
function isMobile(){return window.innerWidth<=768}
function toggleDevPanel(show){const p=document.getElementById('devPanel');const o=document.getElementById('devOverlay');if(!p)return;if(show===undefined||show===null){show=isMobile()?!p.classList.contains('open'):p.classList.contains('collapsed');}if(isMobile()){if(show){p.classList.add('open');if(o)o.classList.add('open');}else{p.classList.remove('open');if(o)o.classList.remove('open');}}else{if(show){p.classList.remove('collapsed');}else{p.classList.add('collapsed');}}}
function toggleHeader(show){const h=document.querySelector('header');const b=document.getElementById('headerToggle');if(!h)return;if(show===undefined||show===null){show=!h.classList.contains('open');}if(show){h.classList.add('open');if(b)b.textContent='Token ▲';}else{h.classList.remove('open');if(b)b.textContent='Token ▼';}}
async function loadDevices(){try{const j=await(await api('/anti-theft/devices')).json();const box=document.getElementById('devs');box.innerHTML='';const lab=document.getElementById('devToggleLabel');if(lab)lab.textContent=SEL?(window.SEL_REMARK||SEL):'未选择设备';if(!j.devices.length){box.innerHTML='<div class="empty">暂无设备<br><span style="font-size:12px">请在手机上完成授权后等待首次轮询</span></div>';autoOpenDevPanel();return;}j.devices.forEach(d=>{const on=(Date.now()-(d.last_seen||0))<20*60*1000;const el=document.createElement('div');el.className='dev'+(SEL===d.device_id?' sel':'');el.innerHTML='<span class="dot '+(on?'on':'off')+'"></span><div class="info"><div class="name">'+esc(d.remark||d.device_id)+'</div><div class="sub">'+esc(d.model||'')+' · '+(on?'在线':'离线')+' · '+fmt(d.last_seen)+'</div></div><span class="arrow">›</span>';el.onclick=()=>{SEL=d.device_id;loadDevices();loadDetail();toggleDevPanel(false);};box.appendChild(el);});autoOpenDevPanel();}catch(e){alert('加载设备失败：'+e.message)}}
function autoOpenDevPanel(){if(!SEL&&isMobile())toggleDevPanel(true);}
async function loadDetail(){if(!SEL)return;const d=document.getElementById('detail');try{
  // 关键: 重建 detail 前主动销毁地图实例(它引用的 mapBox DOM 会被 d.innerHTML=html 销毁)
  // 这样 initAtMap 走"创建"分支(refreshDetail 不调 loadDetail → DOM 不变 → 复用实例,省 Key)
  if(window._atMap){try{window._atMap.destroy();}catch(e){}window._atMap=null;if(window._atOv){window._atOv=[];}}
  const st=await(await api('/anti-theft/status?device_id='+SEL)).json();
  window.SEL_REMARK = st.remark || SEL;
  let html='<div class="detail-header">';
  html+='<div class="detail-title"><h2>'+esc(st.remark||SEL)+'</h2>';
  let addrLine = st.addr ? ' · 📍 '+esc(st.addr) : '';
  html+='<div class="hint">'+(st.online?'<span class="dot on"></span>在线':'<span class="dot off"></span>离线')+' · '+esc(st.model||'')+' · 最近上报 '+fmt(st.last_seen)+addrLine+'</div></div>';
  html+='<div style="display:flex;gap:10px;flex-wrap:wrap"><button class="ghost" onclick="refreshDetail()">刷新本设备</button><button onclick="showUsage()">应用使用情况</button><button onclick="showNotify()">通知线索</button><button class="danger" onclick="confirmDeleteDevice()">删除设备</button></div>';
  html+='</div>';
  html+='<div class="card"><div class="card-title">远程控制</div>';
  html+='<div class="ctl-group"><div class="ctl-label">实时通道</div><div class="ops" style="margin:0">';
  html+='<span id="rtState" class="hint" style="margin:0 8px 0 0">状态：检测中…</span>';
  html+='<button onclick="openRealtime()">开启实时通道</button><button class="ghost" onclick="toggleRealtime(false)">关闭实时通道</button>';
  html+='</div></div>';
  html+='<div class="ctl-group"><div class="ctl-label">快捷</div><div class="ops" style="margin:0">';
  html+='<button onclick="cmd(\'locate\')">定位(单次)</button><button onclick="cmdPhoto(\'front\')">拍照·前摄</button><button onclick="cmdPhoto(\'back\')">拍照·后摄</button><button class="danger" onclick="confirmCmd(\'lock\',\'锁屏\')">锁屏</button>';
  html+='</div></div>';
  html+='<div class="ctl-group"><div class="ctl-label">相册（增量同步）</div><div class="ops" style="margin:0;flex-wrap:wrap;align-items:center">';
  html+='<button onclick="openSyncAlbumModal()">📥 同步相册（自动增量）</button>';
  html+='<button class="ghost" onclick="openResetAlbumModal()">♻️ 重置基线</button>';
  html+='</div>';
  html+='<div class="hint">激活/实时通道为唤醒类指令；快捷与相册为单次执行。开启实时通道会先激活设备以确保离线也能收到。</div></div>';
  html+='<details class="danger-zone"><summary>危险操作</summary>';
  html+='<div class="hint">擦除将恢复出厂设置，所有数据不可恢复，且无法撤销。</div>';
  html+='<label class="ack"><input type="checkbox" id="wipeAck"> 我已了解「擦除」将恢复出厂且不可逆</label>';
  html+='<button id="btnWipe" class="danger" disabled onclick="requestWipe()">擦除全部数据</button>';
  html+='</details>';
  html+='<details class="danger-zone"><summary>数据清理（手动可选）</summary>';
  html+='<div class="hint">手动清理旧数据：选时间窗 + 选数据类型，点「执行清理」会先预估影响、再二次确认。仅删除所选设备所选范围的数据，磁盘照片/相册文件一并删除。</div>';
  html+='<div class="ops" style="flex-wrap:wrap;align-items:center;margin:8px 0"><span style="margin-right:6px">时间窗</span>';
  html+='<select id="cleanDays"><option value="30">30 天前</option><option value="60">60 天前</option><option value="90" selected>90 天前</option></select>';
  html+='<span style="margin:0 12px 0 16px">数据类型</span>';
  html+='<label class="ack"><input type="checkbox" class="cleanType" value="locate" checked> 定位</label>';
  html+='<label class="ack"><input type="checkbox" class="cleanType" value="photo" checked> 照片</label>';
  html+='<label class="ack"><input type="checkbox" class="cleanType" value="album" checked> 相册</label>';
  html+='<label class="ack"><input type="checkbox" class="cleanType" value="command" checked> 指令</label>';
  html+='</div>';
  html+='<button class="danger" onclick="openCleanupModal()">执行清理…</button>';
  html+='</details>';
  html+='<details class="qzone" open><summary>操作队列（手动刷新）</summary>';
  html+='<div class="ops"><button class="ghost" onclick="loadQueue()">刷新队列</button><button class="ghost" onclick="clearQueue()">清空待执行</button></div>';
  html+='<div id="queue"><div class="hint">加载中…</div></div>';
  html+='<div class="hint">待执行可删除；已下发不可撤销，需等手机下一个轮询周期执行。</div></details>';
  html+='<details class="qzone" open><summary>最近位置（手动刷新）</summary>';
  html+='<div class="ops" style="flex-wrap:wrap"><button class="ghost" onclick="loadLocations()">刷新位置</button><button class="ghost" onclick="toggleLocFilter()" style="margin-left:auto">📅 时间筛选 ▾</button></div>';
  html+='<div id="locFilter" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">';
  html+='<div class="ops" style="flex-wrap:wrap">';
  html+='<button class="ghost" onclick="setLocRange(\'today\')">今天</button>';
  html+='<button class="ghost" onclick="setLocRange(\'yesterday\')">昨天</button>';
  html+='<button class="ghost" onclick="setLocRange(\'3d\')">近三天</button>';
  html+='<button class="ghost" onclick="setLocRange(\'week\')">近7天</button>';
  html+='<button class="ghost" onclick="setLocRange(\'month\')">近30天</button>';
  html+='<button class="ghost" onclick="setLocRange(\'all\')">全部</button>';
  html+='<span class="hint" style="margin:0 4px">自定义：</span>';
  html+='<input type="datetime-local" id="lfFrom" onchange="applyLocFilter()">';
  html+='<span class="hint" style="margin:0 4px">至</span>';
  html+='<input type="datetime-local" id="lfTo" onchange="applyLocFilter()">';
  html+='<button class="ghost" onclick="clearLocFilter()">清除筛选</button>';
  html+='</div></div>';
  html+='<div id="mapBox" style="height:360px;border-radius:10px;overflow:hidden;margin-bottom:12px;border:1px solid var(--line)"></div><div id="locTable"></div><div class="hint">坐标为 WGS-84（GPS原生）；「实际位置」由高德逆地理解析（街道/最近 POI，精度取决于 GPS 信号）。「来源」标 ⚠低精度=网络定位（精度几十米~几百米，仅供参考）；GPS=可信（5–20m）。「时间」=本次刷新时距离该定位过了多久（越红说明定位越旧）。位置手动刷新（点悬浮 ⟳ 或「刷新本设备」）。每页 5 条，地图标点随当前页变化；翻页/筛选只拉取当前页数据。</div></details>';
  html+='<details class="qzone" open><summary>照片（手动刷新 · 每页 20 张）</summary>';
  html+='<div class="ops" style="flex-wrap:wrap"><button class="ghost" onclick="loadPhotos(0)">刷新照片</button><button class="ghost" id="batchBtnPhoto" onclick="toggleBatch(\'photo\')">批量操作</button><span id="batchBarPhoto" class="batchBar" style="display:none"><label class="selall"><input type="checkbox" id="selAllPhoto" onchange="selAll(\'photo\',this.checked)"> 全选</label><span id="batchCountPhoto" class="batchCount">已选 0</span><button class="danger" onclick="batchDelete(\'photo\')">删除</button><button onclick="batchExport(\'photo\')">导出</button><button class="ghost" onclick="toggleBatch(\'photo\')">取消</button></span><button class="ghost" onclick="togglePhotoFilter()" style="margin-left:auto">📅 时间筛选 ▾</button></div>';
  html+='<div id="photoFilter" style="display:none;margin:8px 0;padding-top:8px;border-top:1px solid var(--line)"><div class="ops" style="flex-wrap:wrap">';
  html+='<button class="ghost" onclick="setPhotoRange(\'today\')">今天</button>';
  html+='<button class="ghost" onclick="setPhotoRange(\'yesterday\')">昨天</button>';
  html+='<button class="ghost" onclick="setPhotoRange(\'3d\')">近三天</button>';
  html+='<button class="ghost" onclick="setPhotoRange(\'week\')">近7天</button>';
  html+='<button class="ghost" onclick="setPhotoRange(\'month\')">近30天</button>';
  html+='<button class="ghost" onclick="setPhotoRange(\'all\')">全部</button>';
  html+='<span class="hint" style="margin:0 4px">自定义：</span>';
  html+='<input type="datetime-local" id="pfFrom" onchange="applyPhotoFilter()">';
  html+='<span class="hint" style="margin:0 4px">至</span>';
  html+='<input type="datetime-local" id="pfTo" onchange="applyPhotoFilter()">';
  html+='<button class="ghost" onclick="clearPhotoFilter()">清除筛选</button>';
  html+='</div></div>';
  html+='<div class="grid" id="pgrid"></div><div id="pgridPager"></div></details>';
  html+='<details class="qzone" open><summary>相册缩略图（手动刷新 · 每页 20 张）</summary><div class="ops" style="flex-wrap:wrap"><button class="ghost" onclick="loadAlbums(0)">刷新相册</button><button class="ghost" id="batchBtnAlbum" onclick="toggleBatch(\'album\')">批量操作</button><span id="batchBarAlbum" class="batchBar" style="display:none"><label class="selall"><input type="checkbox" id="selAllAlbum" onchange="selAll(\'album\',this.checked)"> 全选</label><span id="batchCountAlbum" class="batchCount">已选 0</span><button class="danger" onclick="batchDelete(\'album\')">删除</button><button onclick="batchExport(\'album\')">导出</button><button class="ghost" onclick="toggleBatch(\'album\')">取消</button></span><span style="width:1px;height:24px;background:var(--line);margin:0 6px"></span><button id="btnClearAlbumAll" class="danger" onclick="openAlbumClearModal(\'all\')">🗑️ 清空全部</button><button id="btnClearAlbumFiltered" class="danger" onclick="openAlbumClearModal(\'filtered\')" disabled title="请先在相册区域设置时间筛选">🗑️ 清空筛选范围</button><span style="flex:1"></span><button class="ghost" onclick="toggleAlbumFilter()">📅 时间筛选 ▾</button></div>';
  html+='<div id="albumFilter" style="display:none;margin:8px 0;padding-top:8px;border-top:1px solid var(--line)"><div class="ops" style="flex-wrap:wrap">';
  html+='<button class="ghost" onclick="setAlbumRange(\'today\')">今天</button>';
  html+='<button class="ghost" onclick="setAlbumRange(\'yesterday\')">昨天</button>';
  html+='<button class="ghost" onclick="setAlbumRange(\'3d\')">近三天</button>';
  html+='<button class="ghost" onclick="setAlbumRange(\'week\')">近7天</button>';
  html+='<button class="ghost" onclick="setAlbumRange(\'month\')">近30天</button>';
  html+='<button class="ghost" onclick="setAlbumRange(\'all\')">全部</button>';
  html+='<span class="hint" style="margin:0 4px">自定义：</span>';
  html+='<input type="datetime-local" id="afFrom" onchange="applyAlbumFilter()">';
  html+='<span class="hint" style="margin:0 4px">至</span>';
  html+='<input type="datetime-local" id="afTo" onchange="applyAlbumFilter()">';
  html+='<button class="ghost" onclick="clearAlbumFilter()">清除筛选</button>';
  html+='</div></div>';
  html+='<div class="grid" id="agrid"></div><div id="agridPager"></div></details>';
  d.innerHTML=html;
  document.getElementById('wipeAck').onchange=function(){document.getElementById('btnWipe').disabled=!this.checked;};
        loadedPhotos.clear();
        // 切换设备：位置翻页回到第 1 页；时间筛选条件保留并回填到新渲染的输入框
        LOC_STATE.page=0;
        setDtLocal('lfFrom',LOC_STATE.filter.from_ts);
        setDtLocal('lfTo',LOC_STATE.filter.to_ts);
        // 切换设备：照片/相册翻页与时间筛选全部重置为第 1 页、无筛选（面板 HTML 为新渲染，输入框默认空）
        PHOTO_STATE.page=0;PHOTO_STATE.filter={from_ts:null,to_ts:null};
        ALBUM_STATE.page=0;ALBUM_STATE.filter={from_ts:null,to_ts:null};
        loadQueue();
        loadLocations();
        loadPhotos(0);
        loadAlbums(0);
        loadRealtimeStatus();
    }catch(e){d.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>'}}
function fixSrc(p){return p=='gps'?'GPS':p=='network'?'网络':p=='passive'?'被动':(p?'其他':'-')}
function locAddr(l){
  if(!l.addr) return '<span style="color:#bbb">—</span>';
  let s = esc(l.addr);
  if(l.acc!=null){
    const tag = fixSrc(l.provider);
    const cls = l.low_accuracy ? ' style="color:#e67e22"' : ' style="color:#888"';
    s += ' <span'+cls+'>(±'+l.acc+'m '+tag+')</span>';
  }
  return s;
}
function timeAgo(ts){if(!ts)return '-';const sec=Math.max(0,Math.floor((Date.now()-ts)/1000));if(sec<60)return sec+'秒前';if(sec<3600)return Math.floor(sec/60)+'分钟前';if(sec<86400)return Math.floor(sec/3600)+'小时前';return '<span style="color:#e74c3c">'+Math.floor(sec/86400)+'天前</span>'}
// ★ 最近位置：分页状态 + 时间筛选（与消息通知同一套范式），每页 5 条、只拉当前页
let LOC_STATE={page:0,total:0,filter:{from_ts:null,to_ts:null}};
const LOC_LIMIT=5;
// ★ 照片/相册：分页状态 + 时间筛选（与位置/通知同一套范式），每页 20 张、只拉当前页
let PHOTO_STATE={page:0,total:0,filter:{from_ts:null,to_ts:null}};
const PHOTO_LIMIT=36;
let ALBUM_STATE={page:0,total:0,filter:{from_ts:null,to_ts:null}};
const ALBUM_LIMIT=36;
function locFilterQs(){const f=LOC_STATE.filter;let s='';if(f.from_ts!=null)s+='&from_ts='+f.from_ts;if(f.to_ts!=null)s+='&to_ts='+f.to_ts;return s;}
function toggleLocFilter(){const f=document.getElementById('locFilter');if(f)f.style.display=(f.style.display==='none'||!f.style.display)?'block':'none';}
function setLocRange(r){const now=Date.now();const day=86400000;let from=null,to=null;if(r==='today'){from=startOfDayTs(now);}else if(r==='yesterday'){from=startOfDayTs(now-day);to=startOfDayTs(now)-1;}else if(r==='3d'){from=now-3*day;}else if(r==='week'){from=now-7*day;}else if(r==='month'){from=now-30*day;}else if(r==='all'){from=null;to=null;}LOC_STATE.filter={from_ts:from,to_ts:to};setDtLocal('lfFrom',from);setDtLocal('lfTo',to);loadLocations(0);}
function applyLocFilter(){const from=dtLocalToTs('lfFrom');const to=dtLocalToTs('lfTo');LOC_STATE.filter={from_ts:from,to_ts:to};loadLocations(0);}
function clearLocFilter(){LOC_STATE.filter={from_ts:null,to_ts:null};const f=document.getElementById('lfFrom');if(f)f.value='';const t=document.getElementById('lfTo');if(t)t.value='';loadLocations(0);}
function clearAtMapOv(){if(window._atMap&&window._atOv){window._atOv.forEach(function(o){window._atMap.remove(o);});}window._atOv=[];}
async function loadLocations(page){if(!SEL)return;if(page==null)page=LOC_STATE.page||0;try{const loc=await(await api('/anti-theft/locations?device_id='+encodeURIComponent(SEL)+'&limit='+LOC_LIMIT+'&offset='+(page*LOC_LIMIT)+locFilterQs())).json();const box=document.getElementById('locTable');if(!box)return;const rows=loc.locations||[];LOC_STATE.page=page;LOC_STATE.total=loc.total||0;
if(!rows.length){clearAtMapOv();box.innerHTML='<div class="hint">当前筛选条件下暂无位置数据</div>';return;}
let h='<div class="loc-table"><table><tr><th>时间</th><th>纬度</th><th>经度</th><th>精度</th><th>来源</th><th>时间</th><th>星数</th><th>电量</th><th>实际位置</th></tr>';
rows.forEach(l=>{const low=l.low_accuracy?true:false;h+='<tr><td>'+fmt(l.ts)+'</td><td>'+l.lat+'</td><td>'+l.lng+'</td><td>'+(l.acc?l.acc+'m':'')+'</td><td>'+(low?'<span style="color:#e67e22">':'')+fixSrc(l.provider)+(low?' ⚠低精度</span>':'')+'</td><td>'+timeAgo(l.ts)+'</td><td>'+(l.satellites!=null?l.satellites:'-')+'</td><td>'+(l.bat!=null?l.bat+'%':'')+'</td><td>'+locAddr(l)+'</td></tr>';});
h+='</table></div>';
h+='<div class="loc-list">';
rows.forEach(l=>{const low=l.low_accuracy?true:false;const src=(low?'<span style="color:#e67e22">':'')+fixSrc(l.provider)+(low?' ⚠低精度</span>':'')+'';const addr=locAddr(l);
h+='<div class="loc-item">';
h+='<div class="row"><span class="k">时间</span><span class="v">'+fmt(l.ts)+'</span></div>';
h+='<div class="row"><span class="k">坐标</span><span class="v">'+l.lat+', '+l.lng+'</span></div>';
h+='<div class="row"><span class="k">精度</span><span class="v">'+(l.acc?l.acc+'m':'-')+'</span></div>';
h+='<div class="row"><span class="k">来源</span><span class="v">'+src+'</span></div>';
h+='<div class="row"><span class="k">时间</span><span class="v">'+timeAgo(l.ts)+'</span></div>';
h+='<div class="row"><span class="k">星数</span><span class="v">'+(l.satellites!=null?l.satellites:'-')+'</span></div>';
h+='<div class="row"><span class="k">电量</span><span class="v">'+(l.bat!=null?l.bat+'%':'-')+'</span></div>';
h+='<div class="row"><span class="k">实际位置</span><span class="v">'+addr+'</span></div>';
h+='</div>';});
h+='</div>';
h+=renderLocPager(LOC_STATE.total,page);
box.innerHTML=h;initAtMap(rows);}catch(e){}}
// 最近位置翻页条：复用通用压缩算法(最多 5 个数字按钮 + 省略号)，样式与消息通知一致
function renderLocPager(total,page){const pages=Math.max(1,Math.ceil(total/LOC_LIMIT));const cur=page+1;const pid='lpGo_'+cur+'_'+Math.floor(Math.random()*1e9).toString(36);let h='<div class="npager"><span class="np-info">共 '+total+' 条 · 第 '+cur+' / '+pages+' 页</span>';h+='<button '+(page<=0?'disabled':'')+' onclick="gotoLocPage('+(page-1)+')">‹</button>';h+=pagerNumsHTML(page,pages,'gotoLocPage(');h+='<button '+(page>=pages-1?'disabled':'')+' onclick="gotoLocPage('+(page+1)+')">›</button>';h+='<span class="np-go">到第 <input type="number" id="'+pid+'" min="1" max="'+pages+'" placeholder="页" onkeydown="if(event.keyCode===13){goLocJumpInline(&#39;'+pid+'&#39;);event.preventDefault();}"> 页 <button onclick="goLocJumpInline(&#39;'+pid+'&#39;)">Go</button></span>';h+='</div>';return h;}
async function gotoLocPage(page){const pages=Math.max(1,Math.ceil(LOC_STATE.total/LOC_LIMIT));if(page<0||page>=pages)return;await loadLocations(page);}
function goLocJumpInline(inputId){const pages=Math.max(1,Math.ceil(LOC_STATE.total/LOC_LIMIT));const inp=document.getElementById(inputId);if(!inp)return;const v=parseInt(inp.value,10);if(!v||v<1||v>pages){alert('请输入 1~'+pages+' 之间的页码');inp.focus();inp.select();return;}inp.value='';gotoLocPage(v-1);}
function initAtMap(rows){
var mb=document.getElementById('mapBox');
if(!mb||typeof AMap==='undefined'||!rows.length)return;
var latest=rows[0];
if(!latest.gcj_lat||!latest.gcj_lng)return;
// 关键: loadDetail 已在重建 DOM 前主动销毁旧实例,这里只需判断是否存在:
// - loadDetail 触发(点 ⟳ / 切换设备): window._atMap 已被销毁 → 走创建分支(1 次 Key)
// - refreshDetail 触发(点"刷新本设备"): DOM 没变 → window._atMap 仍在 → 复用(0 次 Key,省额度)
if(!window._atMap){
window._atMap=new AMap.Map('mapBox',{zoom:17,center:[latest.gcj_lng,latest.gcj_lat]});
}else{
window._atMap.setCenterAndZoom([latest.gcj_lng,latest.gcj_lat],17);
}
if(window._atOv){window._atOv.forEach(function(o){window._atMap.remove(o);});}
window._atOv=[];
rows.forEach(function(l,idx){
if(!l.gcj_lat||!l.gcj_lng)return;
var isLatest=idx===0;
var m=new AMap.Marker({position:[l.gcj_lng,l.gcj_lat],title:fmt(l.ts)});
m.on('click',function(){
var c='<div style="padding:8px 12px;min-width:220px;font-size:13px;line-height:1.6">';
c+='<b style="color:'+(isLatest?'#3b6cff':'#888')+'">'+(isLatest?'最新位置':'#'+(idx+1))+'</b><br>';
c+=fmt(l.ts)+' ('+timeAgo(l.ts)+')<br>';
c+='精度: ±'+(l.acc||'?')+'m ('+fixSrc(l.provider)+')<br>';
c+='地址: '+esc(l.addr||'-');
c+='</div>';
var iw=new AMap.InfoWindow({offset:new AMap.Pixel(0,-32),content:c});
iw.open(window._atMap,[l.gcj_lng,l.gcj_lat]);
window._atMap.setCenter([l.gcj_lng,l.gcj_lat]);
});
window._atMap.add(m);
window._atOv.push(m);
if(l.acc&&l.acc>0){
var ci=new AMap.Circle({center:[l.gcj_lng,l.gcj_lat],radius:l.acc,fillColor:isLatest?'#3b6cff':'#9ca3af',fillOpacity:0.12,strokeColor:isLatest?'#3b6cff':'#bbb',strokeWeight:1,strokeOpacity:0.5});
window._atMap.add(ci);
window._atOv.push(ci);
}
});
}
async function loadPhotos(page){if(!SEL)return;if(page==null)page=PHOTO_STATE.page||0;try{
  const ph=await(await api('/anti-theft/photos?device_id='+encodeURIComponent(SEL)+'&limit='+PHOTO_LIMIT+'&offset='+(page*PHOTO_LIMIT)+photoFilterQs())).json();
  const grid=document.getElementById('pgrid');if(!grid)return;const rows=ph.photos||[];
  PHOTO_STATE.page=page;PHOTO_STATE.total=ph.total||0;
  if(!rows.length){grid.innerHTML=(PHOTO_STATE.page===0)?'<div class="hint">暂无照片（下发「拍照」后等待手机响应）</div>':'<div class="hint">该时间范围内没有更多照片</div>';PHOTO_LIST=[];loadedPhotos.clear();selPhotos.clear();renderPhotoPager();return;}
  grid.innerHTML='';PHOTO_LIST=[];loadedPhotos.clear();selPhotos.clear();
  for(const p of rows){loadedPhotos.add(p.id);PHOTO_LIST.push(p);const card=document.createElement('div');card.className='photo-card';card.dataset.id=p.id;card.dataset.path=p.path;const img=document.createElement('img');const r=await api(p.url);const blob=await r.blob();img.src=URL.createObjectURL(blob);img.title=fmt(p.ts);card.appendChild(img);const chk=document.createElement('input');chk.type='checkbox';chk.className='photo-check';chk.dataset.id=p.id;chk.onclick=(e)=>e.stopPropagation();chk.onchange=()=>{toggleSelCard('photo',p.id,card);};card.appendChild(chk);const ts=document.createElement('div');ts.className='photo-ts';ts.textContent=fmt(p.ts);card.appendChild(ts);card.onclick=()=>{if(BATCH.photo){toggleSelCard('photo',p.id,card);}else{openLightbox('photo',p.id);}};grid.appendChild(card);}
  renderPhotoPager();
}catch(e){alert('加载照片失败：'+e.message);}}
function photoFilterQs(){const f=PHOTO_STATE.filter;let s='';if(f.from_ts!=null)s+='&from_ts='+f.from_ts;if(f.to_ts!=null)s+='&to_ts='+f.to_ts;return s;}
function togglePhotoFilter(){const f=document.getElementById('photoFilter');if(f)f.style.display=(f.style.display==='none'||!f.style.display)?'block':'none';}
function setPhotoRange(r){const now=Date.now();const day=86400000;let from=null,to=null;if(r==='today'){from=startOfDayTs(now);}else if(r==='yesterday'){from=startOfDayTs(now-day);to=startOfDayTs(now)-1;}else if(r==='3d'){from=now-3*day;}else if(r==='week'){from=now-7*day;}else if(r==='month'){from=now-30*day;}else if(r==='all'){from=null;to=null;}PHOTO_STATE.filter={from_ts:from,to_ts:to};setDtLocal('pfFrom',from);setDtLocal('pfTo',to);loadPhotos(0);}
function applyPhotoFilter(){const from=dtLocalToTs('pfFrom');const to=dtLocalToTs('pfTo');PHOTO_STATE.filter={from_ts:from,to_ts:to};loadPhotos(0);}
function clearPhotoFilter(){PHOTO_STATE.filter={from_ts:null,to_ts:null};const f=document.getElementById('pfFrom');if(f)f.value='';const t=document.getElementById('pfTo');if(t)t.value='';loadPhotos(0);}
function renderPhotoPager(){const box=document.getElementById('pgridPager');if(!box)return;const total=PHOTO_STATE.total;const page=PHOTO_STATE.page;const pages=Math.max(1,Math.ceil(total/PHOTO_LIMIT));box.innerHTML=renderPagerGeneric(total,page,pages,'gotoPhotoPage');}
async function gotoPhotoPage(page){const pages=Math.max(1,Math.ceil(PHOTO_STATE.total/PHOTO_LIMIT));if(page<0||page>=pages)return;await loadPhotos(page);}
function gotoPhotoPageJump(inputId){const pages=Math.max(1,Math.ceil(PHOTO_STATE.total/PHOTO_LIMIT));const inp=document.getElementById(inputId);if(!inp)return;const v=parseInt(inp.value,10);if(!v||v<1||v>pages){alert('请输入 1~'+pages+' 之间的页码');inp.focus();inp.select();return;}inp.value='';gotoPhotoPage(v-1);}
// 通用压缩翻页条（最多 5 个数字按钮 + 省略号），照片/相册/位置/通知共用同一套视觉
function renderPagerGeneric(total,page,pages,call){const cur=page+1;const pid='pj_'+call+'_'+cur+'_'+Math.floor(Math.random()*1e9).toString(36);let h='<div class="npager"><span class="np-info">共 '+total+' 张 · 第 '+cur+' / '+pages+' 页</span>';h+='<button '+(page<=0?'disabled':'')+' onclick="'+call+'('+(page-1)+')">‹</button>';h+=pagerNumsHTML(page,pages,call+'(');h+='<button '+(page>=pages-1?'disabled':'')+' onclick="'+call+'('+(page+1)+')">›</button>';h+='<span class="np-go">到第 <input type="number" id="'+pid+'" min="1" max="'+pages+'" placeholder="页" onkeydown="if(event.keyCode===13){'+call+'Jump(&#39;'+pid+'&#39;);event.preventDefault();}"> 页 <button onclick="'+call+'Jump(&#39;'+pid+'&#39;)">Go</button></span>';h+='</div>';return h;}
function toggleSelCard(type,id,card){const set=type==='album'?selAlbums:selPhotos;const chk=card.querySelector('.photo-check');if(set.has(id)){set.delete(id);card.classList.remove('sel');if(chk)chk.checked=false;}else{set.add(id);card.classList.add('sel');if(chk)chk.checked=true;}updateBatchBar(type);}
function updateBatchBar(type){const set=type==='album'?selAlbums:selPhotos;document.getElementById('batchCount'+(type==='album'?'Album':'Photo')).textContent='已选 '+set.size;}
function toggleBatch(type){BATCH[type]=!BATCH[type];const grid=document.getElementById(type==='album'?'agrid':'pgrid');grid.classList.toggle('batch',BATCH[type]);const btn=document.getElementById('batchBtn'+(type==='album'?'Album':'Photo'));const bar=document.getElementById('batchBar'+(type==='album'?'Album':'Photo'));if(BATCH[type]){btn.style.display='none';bar.style.display='inline-flex';}else{btn.style.display='inline-block';bar.style.display='none';const set=type==='album'?selAlbums:selPhotos;set.clear();grid.querySelectorAll('.photo-card').forEach(c=>c.classList.remove('sel'));const sa=document.getElementById('selAll'+(type==='album'?'Album':'Photo'));if(sa)sa.checked=false;updateBatchBar(type);}}
function selAll(type,checked){const set=type==='album'?selAlbums:selPhotos;const grid=document.getElementById(type==='album'?'agrid':'pgrid');grid.querySelectorAll('.photo-card').forEach(card=>{const id=card.dataset.id;const chk=card.querySelector('.photo-check');if(checked){set.add(id);card.classList.add('sel');if(chk)chk.checked=true;}else{set.delete(id);card.classList.remove('sel');if(chk)chk.checked=false;}});updateBatchBar(type);}
async function batchDelete(type){const set=type==='album'?selAlbums:selPhotos;if(!set.size)return;if(!confirm('确认批量删除选中的 '+set.size+' 张'+(type==='album'?'相册图':'照片')+'？\n物理文件将永久删除，不可恢复。'))return;const ids=Array.from(set);for(const id of ids){const el=document.querySelector((type==='album'?'#agrid':'#pgrid')+' .photo-card[data-id="'+id+'"]');const path=el&&el.dataset.path;if(!path)continue;try{await api('/anti-theft/'+(type==='album'?'album':'photo')+'/'+encodeURIComponent(path),{method:'DELETE'});}catch(e){}}if(type==='album'){await loadAlbums(ALBUM_STATE.page);selAlbums.clear();const sa=document.getElementById('selAllAlbum');if(sa)sa.checked=false;updateBatchBar('album');}else{await loadPhotos(PHOTO_STATE.page);selPhotos.clear();const sa=document.getElementById('selAllPhoto');if(sa)sa.checked=false;updateBatchBar('photo');}}
function batchExport(type){const set=type==='album'?selAlbums:selPhotos;if(!set.size)return;const grid=document.getElementById(type==='album'?'agrid':'pgrid');let n=0;const cN={c:0,t:0};grid.querySelectorAll('.photo-card').forEach(card=>{if(!set.has(card.dataset.id))return;const img=card.querySelector('img');if(!img||!img.src)return;let url=img.src;let label='export_';// 相册图：若该张已缓存原图（has_original=1），则导出原图；否则导出缩略图。
if(type==='album'){const id=parseInt(card.dataset.id);const p=ALBUM_LIST.find(x=>x.id===id);if(p&&p.has_original){url='/anti-theft/album/original?device_id='+encodeURIComponent(SEL)+'&idx='+encodeURIComponent(p.idx);cN.c++;label='original_';}else{cN.t++;label='thumb_';}}const a=document.createElement('a');a.href=url;a.download=label+(++n)+'.jpg';a.target='_self';document.body.appendChild(a);a.click();a.remove();});if(!n)alert('没有可导出的图片');else if(type==='album')alert('已导出 '+n+' 张（原图 '+cN.c+' 张，缩略图 '+cN.t+' 张）。\n浏览器可能弹出多次下载，请允许。');}
async function loadAlbums(page){if(!SEL)return;if(page==null)page=ALBUM_STATE.page||0;try{
  const ph=await(await api('/anti-theft/albums?device_id='+encodeURIComponent(SEL)+'&limit='+ALBUM_LIMIT+'&offset='+(page*ALBUM_LIMIT)+albumFilterQs())).json();
  const grid=document.getElementById('agrid');if(!grid)return;const rows=ph.albums||[];
  ALBUM_STATE.page=page;ALBUM_STATE.total=ph.total||0;
  if(!rows.length){grid.innerHTML=(ALBUM_STATE.page===0)?'<div class="hint">暂无相册（点「同步相册」后等待手机响应）</div>':'<div class="hint">该时间范围内没有更多相册图</div>';ALBUM_LIST=[];selAlbums.clear();renderAlbumPager();return;}
  grid.innerHTML='';ALBUM_LIST=[];
  for(const p of rows){ALBUM_LIST.push(p);const r=await api(p.url);const blob=await r.blob();const url=URL.createObjectURL(blob);const card=document.createElement('div');card.className='photo-card';card.dataset.id=p.id;card.dataset.path=p.path;const img=document.createElement('img');img.src=url;img.title=fmt(p.ts)+' #'+p.idx;card.appendChild(img);if(p.has_original){const hd=document.createElement('div');hd.className='hd-badge';hd.textContent='HD';card.appendChild(hd);}const chk=document.createElement('input');chk.type='checkbox';chk.className='photo-check';chk.dataset.id=p.id;chk.onclick=(e)=>e.stopPropagation();chk.onchange=()=>{toggleSelCard('album',p.id,card);};card.appendChild(chk);const ts=document.createElement('div');ts.className='photo-ts';ts.textContent=fmt(p.ts);card.appendChild(ts);card.onclick=()=>{if(BATCH.album){toggleSelCard('album',p.id,card);}else{openLightbox('album',p.id);}};grid.appendChild(card);}
  renderAlbumPager();
}catch(e){alert('加载相册失败：'+e.message);}}
function albumFilterQs(){const f=ALBUM_STATE.filter;let s='';if(f.from_ts!=null)s+='&from_ts='+f.from_ts;if(f.to_ts!=null)s+='&to_ts='+f.to_ts;return s;}
function toggleAlbumFilter(){const f=document.getElementById('albumFilter');if(f)f.style.display=(f.style.display==='none'||!f.style.display)?'block':'none';}
function setAlbumRange(r){const now=Date.now();const day=86400000;let from=null,to=null;if(r==='today'){from=startOfDayTs(now);}else if(r==='yesterday'){from=startOfDayTs(now-day);to=startOfDayTs(now)-1;}else if(r==='3d'){from=now-3*day;}else if(r==='week'){from=now-7*day;}else if(r==='month'){from=now-30*day;}else if(r==='all'){from=null;to=null;}ALBUM_STATE.filter={from_ts:from,to_ts:to};setDtLocal('afFrom',from);setDtLocal('afTo',to);updateClearAlbumFilteredBtn();loadAlbums(0);}
function applyAlbumFilter(){const from=dtLocalToTs('afFrom');const to=dtLocalToTs('afTo');ALBUM_STATE.filter={from_ts:from,to_ts:to};updateClearAlbumFilteredBtn();loadAlbums(0);}
function clearAlbumFilter(){ALBUM_STATE.filter={from_ts:null,to_ts:null};const f=document.getElementById('afFrom');if(f)f.value='';const t=document.getElementById('afTo');if(t)t.value='';updateClearAlbumFilteredBtn();loadAlbums(0);}
function updateClearAlbumFilteredBtn(){const btn=document.getElementById('btnClearAlbumFiltered');if(!btn)return;const f=ALBUM_STATE.filter||{from_ts:null,to_ts:null};const hasFilter=(f.from_ts!=null||f.to_ts!=null);btn.disabled=!hasFilter;btn.title=hasFilter?'':'请先在相册区域设置时间筛选';}
let albumClearTimer=null;let _albumClearScope=null;let _albumClearCount=-1;
function openAlbumClearModal(scope){if(!SEL)return;_albumClearScope=scope;_albumClearCount=-1;const m=document.getElementById('albumClearModal');document.getElementById('albumClearDevName').textContent=window.SEL_REMARK||SEL;const pv=document.getElementById('albumClearPreview');pv.textContent='正在预估影响…';m.style.display='flex';(async()=>{try{const f=ALBUM_STATE.filter||{from_ts:null,to_ts:null};let qs='?device_id='+encodeURIComponent(SEL);if(scope==='filtered'){if(f.from_ts!=null)qs+='&from_ts='+f.from_ts;if(f.to_ts!=null)qs+='&to_ts='+f.to_ts;}const j=await(await api('/anti-theft/albums/count'+qs)).json();_albumClearCount=j.count||0;const scopeText=scope==='all'?'<b>全部</b>':'<b>当前筛选范围</b>';pv.innerHTML='将删除 '+scopeText+' 的相册图，共 <b>'+_albumClearCount+'</b> 张。';}catch(e){pv.textContent='预估失败：'+e.message;}})();document.getElementById('albumClearPhrase').value='';document.getElementById('albumClearAck').checked=false;document.getElementById('albumClearResetBaseline').checked=false;window._albumClearAck=false;const btn=document.getElementById('albumClearFinal');btn.disabled=true;btn.textContent='5 秒后可确认';clearInterval(albumClearTimer);let n=5;albumClearTimer=setInterval(()=>{n--;if(n<=0){clearInterval(albumClearTimer);if(_albumClearCount===0){btn.textContent='范围内无相册图';btn.disabled=true;}else{btn.textContent='确认清空';btn.disabled=false;}}else{btn.textContent=n+' 秒后可确认';}},1000);}
function albumClearCheck(){const phraseOk=document.getElementById('albumClearPhrase').value.trim()==='清空';const ackOk=document.getElementById('albumClearAck').checked;window._albumClearAck=phraseOk&&ackOk;}
async function doAlbumClear(){if(!window._albumClearAck)return;if(document.getElementById('albumClearFinal').disabled){alert('请等待冷却结束再确认');return;}const scope=_albumClearScope;const resetBaseline=document.getElementById('albumClearResetBaseline').checked;closeAlbumClearModal();try{const f=ALBUM_STATE.filter||{from_ts:null,to_ts:null};let qs='?device_id='+encodeURIComponent(SEL);if(scope==='filtered'){if(f.from_ts!=null)qs+='&from_ts='+f.from_ts;if(f.to_ts!=null)qs+='&to_ts='+f.to_ts;}const r=await api('/anti-theft/albums'+qs,{method:'DELETE'});const j=await r.json();if(resetBaseline){const cmdParams={reset:true};if(scope==='filtered'){if(f.from_ts!=null)cmdParams.from_ts=f.from_ts;if(f.to_ts!=null)cmdParams.to_ts=f.to_ts;}try{await api('/anti-theft/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,cmd:'album',params:cmdParams})});}catch(e){alert('清空成功（删除 '+(j.deleted||0)+' 张），但重置基线下发失败：'+e.message);await loadAlbums(0);selAlbums.clear();const sa=document.getElementById('selAllAlbum');if(sa)sa.checked=false;updateBatchBar('album');return;}}let msg='已清空相册，共删除 '+(j.deleted||0)+' 张。';if(resetBaseline)msg+='\n已同时重置手机基线，下次同步会重新上传这些照片。';else msg+='\n（手机基线未变，旧图不会自动重传）';alert(msg);await loadAlbums(0);selAlbums.clear();const sa=document.getElementById('selAllAlbum');if(sa)sa.checked=false;updateBatchBar('album');}catch(e){alert('清空失败：'+e.message);}}
function closeAlbumClearModal(){document.getElementById('albumClearModal').style.display='none';clearInterval(albumClearTimer);}
function renderAlbumPager(){const box=document.getElementById('agridPager');if(!box)return;const total=ALBUM_STATE.total;const page=ALBUM_STATE.page;const pages=Math.max(1,Math.ceil(total/ALBUM_LIMIT));box.innerHTML=renderPagerGeneric(total,page,pages,'gotoAlbumPage');}
async function gotoAlbumPage(page){const pages=Math.max(1,Math.ceil(ALBUM_STATE.total/ALBUM_LIMIT));if(page<0||page>=pages)return;await loadAlbums(page);}
function gotoAlbumPageJump(inputId){const pages=Math.max(1,Math.ceil(ALBUM_STATE.total/ALBUM_LIMIT));const inp=document.getElementById(inputId);if(!inp)return;const v=parseInt(inp.value,10);if(!v||v<1||v>pages){alert('请输入 1~'+pages+' 之间的页码');inp.focus();inp.select();return;}inp.value='';gotoAlbumPage(v-1);}
function openLightbox(type,id){const list=type==='album'?ALBUM_LIST:PHOTO_LIST;const idx=list.findIndex(x=>x.id===id);if(idx<0)return;LB_STATE={type:type,idx:idx,open:true};renderLightbox();applyZoom();document.getElementById('lightbox').classList.add('open');flashLbHint();}
// 每图独立保存缩放状态：key = type+':'+id，value={scale,tx,ty}
const LB_ZOOM={};
function lbKey(){const list=LB_STATE.type==='album'?ALBUM_LIST:PHOTO_LIST;const p=list[LB_STATE.idx];return LB_STATE.type+':'+(p?p.id:'?');}
function lbGetZoom(){const k=lbKey();return LB_ZOOM[k]||{scale:1,tx:0,ty:0};}
function lbSetZoom(z){LB_ZOOM[lbKey()]=z;applyZoom();}
function applyZoom(){const img=document.getElementById('lbImg');const z=lbGetZoom();img.style.transform='translate3d('+z.tx+'px,'+z.ty+'px,0) scale('+z.scale+')';img.classList.toggle('zoomed',z.scale>1.001);const resetBtn=document.getElementById('lbReset');resetBtn.style.display=z.scale>1.001?'flex':'none';document.getElementById('lbZoom').textContent=Math.round(z.scale*100)+'%';}
// 仅限制 scale，不限制 tx/ty（图片可以完全拖出视口任意方向，靠"↻ 还原"按钮一键回到 100% 居中）
function lbClamp(z){const s=Math.max(0.5,Math.min(6,z.scale));return {scale:s,tx:z.tx,ty:z.ty};}
function renderLightbox(){const list=LB_STATE.type==='album'?ALBUM_LIST:PHOTO_LIST;const p=list[LB_STATE.idx];if(!p)return;const img=document.getElementById('lbImg');const info=document.getElementById('lbInfo');const origBtn=document.getElementById('lbOrig');img.style.transform='';img.classList.remove('zoomed','grabbing');// 如果该照片已加载过原图（缓存在 _originalUrl），继续显示原图；否则显示缩略图
  if (p._originalUrl) {
    img.src = p._originalUrl;
    p._showingOriginal = true;
  } else {
    img.src=p._url||(LB_STATE.type==='album'?document.querySelector('#agrid .photo-card img[title*="#'+p.idx+'"]')?.src:document.querySelector('#pgrid .photo-card[data-id="'+p.id+'"] img')?.src)||'';
    p._showingOriginal = false;
  }info.textContent=(LB_STATE.idx+1)+' / '+list.length+' · '+fmt(p.ts)+(LB_STATE.type==='album'?' · 相册#'+p.idx:'')+' · 滚轮/双指缩放 · 拖动平移 · ← → 翻页 · ESC 关闭';// 相册图且有原图：显示「查看原图」按钮；否则隐藏。缩略图点击直接看缩略图（原图按需）
  // 相册图一律显示「查看原图」按钮（不论 has_original；命中已缓存就走 GET 切换，下发指令走轮询）。
  // 设计原意"按需拉"是「点了才拉」，不是「有缓存才让点」。否则刚重传完 has_original 全 0 → 按钮永远不可见 → 死锁。
  const _showOrigBtn = LB_STATE.type === 'album';
  origBtn.style.display = _showOrigBtn ? 'flex' : 'none';
  origBtn.dataset.idx = p.idx;
  origBtn.dataset.mediaId = (p.media_id != null ? p.media_id : '');
  // 文案按当前图状态切换：已显示原图 → "🔙 切回缩略图"；已缓存但当前在缩略图 → "🔍 显示原图"；未缓存 → "📥 查看原图"
  if (_showOrigBtn) {
    if (p._showingOriginal) {
      origBtn.textContent = '🔙 切回缩略图';
    } else {
      // 不管 has_original 是 0 还是 1，统一动作：去拉原图。
      // 上一版分"📥 查看原图 / 🔍 显示原图"两档，用户会被"显示原图"这四个字误导成"当前就是原图状态"，实际上图还是缩略图。
      origBtn.textContent = '📥 查看原图';
    }
  }
  origBtn.disabled = false;
  applyZoom();}
// 查看原图：先尝试从服务器取缓存（已申请过则直接切换），否则下发指令让手机上传，轮询直到可用。
// 三态按钮：(a) 当前在原图 → 切回缩略图；(b) 已缓存（has_original=1）但当前在缩略图 → 拉取并切换；(c) 未缓存 → 下发指令后轮询。
let lbOrigTimer = null;
async function requestOriginal() {
  const idx = parseInt(document.getElementById('lbOrig').dataset.idx);
  if (isNaN(idx)) return;
  console.log('[lbOrig] enter idx=' + idx + ' url=/anti-theft/album/original?device_id=' + encodeURIComponent(SEL) + '&idx=' + idx);
  const btn = document.getElementById('lbOrig');
  const p = (LB_STATE.type === 'album' ? ALBUM_LIST : PHOTO_LIST).find(x => x.idx === idx);
  if (!p) return;
  // (a) 当前已是原图 → 切回缩略图
  if (p._showingOriginal) {
    if (p._thumbUrl) {
      document.getElementById('lbImg').src = p._thumbUrl;
    }
    p._showingOriginal = false;
    btn.textContent = '📥 查看原图';
    applyZoom();
    return;
  }
  btn.disabled = true;
  btn.textContent = '⏳ 请求原图中…';
  try {
    // (b) / (c) 都先尝试 GET 拿缓存（hit 直接显示；miss 下发指令再轮询）
    let r = await fetch('/anti-theft/album/original?device_id=' + encodeURIComponent(SEL) + '&idx=' + idx);
    console.log('[lbOrig] first GET status=' + r.status + ' ct=' + r.headers.get('content-type') + ' cl=' + r.headers.get('content-length'));
    if (!r.ok) {
      console.log('[lbOrig] GET miss → 下发手机上传指令');
      await apiPost('/anti-theft/command', { device_id: SEL, cmd: 'album_original', params: { idx: idx, media_id: (p.media_id != null ? p.media_id : null) } });
      btn.textContent = '⏳ 手机上传中…';
      // 轮询最多 ~30s，每 2s 试一次
      let waited = 0;
      const got = await new Promise((resolve) => {
        lbOrigTimer = setInterval(async () => {
          try {
            const rr = await fetch('/anti-theft/album/original?device_id=' + encodeURIComponent(SEL) + '&idx=' + idx);
            if (rr.ok) { clearInterval(lbOrigTimer); resolve(rr); return; }
          } catch (_) { /* keep polling */ }
          waited += 2000;
          if (waited >= 30000) { clearInterval(lbOrigTimer); resolve(null); }
        }, 2000);
      });
      r = got;
    }
    if (r && r.ok) {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      console.log('[lbOrig] success blobSize=' + blob.size + ' type=' + blob.type);
      // 记住原图 URL 与原缩略图 URL，下次再点可来回切
      if (!p._thumbUrl) p._thumbUrl = document.getElementById('lbImg').src;
      p._originalUrl = url;
      p._showingOriginal = true;
      p.has_original = true;
      const oldSrc = document.getElementById('lbImg').src;
      document.getElementById('lbImg').src = url;
      // 等待图片真的解码出来（避免浏览器对同 src 替换有缓存）
      document.getElementById('lbImg').onload = function () {
        const nW = document.getElementById('lbImg').naturalWidth;
        const nH = document.getElementById('lbImg').naturalHeight;
        console.log('[lbOrig] img loaded natural=' + nW + 'x' + nH + ' oldSrc=' + (oldSrc||'').slice(0,40) + ' newSrc=' + url.slice(0,40));
        // 切到原图后根据原图自然尺寸自动调整 zoom：
        // - 自然尺寸 > 容器：保持当前 zoom（用户可手动看大图）
        // - 自然尺寸 ≈ 容器：当前 1x OK
        // - 自然尺寸 << 容器（1080x2420 fit 到大窗口）：自动放大到 1.5x，让"原图更大"这件事视觉上更明显
        const z = lbGetZoom();
        if (z.scale <= 1.001 && nW > 0) {
          const r = document.getElementById('lbImg').getBoundingClientRect();
          const containerW = r.width / z.scale;
          if (nW / containerW > 0.8) {
            // 原图比容器大（少见），自动 zoom 到 1.5x 看细节
            lbSetZoom({ scale: 1.5, tx: 0, ty: 0 });
          }
        }
      };
      btn.textContent = '🔙 切回缩略图';
      btn.disabled = false;
      // 不调 applyZoom()——保留当前 zoom。如果之前用户已经在 zoom 状态（比如放大看缩略图细节），切到原图后同一 zoom 下细节更清晰，更直观。
    } else {
      btn.disabled = false;
      btn.textContent = '⚠️ 获取超时，重试';
    }
  } catch (_e) {
    console.log('[lbOrig] error: ' + (_e && _e.message));
    btn.disabled = false;
    btn.textContent = '⚠️ 获取失败，重试';
  }
}
function currentAlbumMediaId(idx){const p=ALBUM_LIST.find(x=>x.idx===idx);return p&&p.media_id!=null?p.media_id:null;}
// 关闭：只有点到"图片之外的空白区"或 × 才关；点到图片 / 拖动结束的 click 一律忽略
function closeLightbox(e){
  if(!e)return;
  const lbImg=document.getElementById('lbImg');
  // 点到图片本身：不关（拖动/缩放都在图片上，松手补发的 click target 也是它）
  if(e.target===lbImg)return;
  // 点到其它控件（× / 翻页 / 还原 / 底部信息条）由其自身 onclick 处理，这里不拦截也不关
  if(e.target.closest('#lbClose,#lbPrev,#lbNext,#lbReset,#lbInfo,#lbHint,#lbZoom'))return;
  e.stopPropagation();
  LB_STATE.open=false;
  document.getElementById('lightbox').classList.remove('open');
}
function changeLightbox(dir,e){if(e)e.stopPropagation();const list=LB_STATE.type==='album'?ALBUM_LIST:PHOTO_LIST;if(!list.length)return;LB_STATE.idx=(LB_STATE.idx+dir+list.length)%list.length;renderLightbox();}
function resetZoom(e){if(e)e.stopPropagation();lbSetZoom({scale:1,tx:0,ty:0});}
function flashLbHint(){const h=document.getElementById('lbHint');h.classList.add('show');clearTimeout(h._t);h._t=setTimeout(()=>h.classList.remove('show'),2600);}
// —— 缩放/拖动状态机 ——
(function(){
  const img=document.getElementById('lbImg');
  let dragging=false,moved=false,sx=0,sy=0,oz=null;
  let pinch=null; // {dist, scale0, z0}
  // 滚轮缩放：以光标位置为锚点（光标下的点不动），scale 0.5~6
  // 滚轮缩放：以光标位置为锚点（光标下的点不动）。CSS transform-origin:50% 50%，锚点坐标统一用"光标相对 box 中心"
  img.addEventListener('wheel',function(e){if(!LB_STATE.open)return;e.preventDefault();const r=img.getBoundingClientRect();const bw=img.clientWidth,bh=img.clientHeight;const z=lbGetZoom();const cx=r.left+bw/2,cy=r.top+bh/2;const px=e.clientX-cx,py=e.clientY-cy;const factor=e.deltaY<0?1.15:1/1.15;const ns=Math.max(0.5,Math.min(6,z.scale*factor));const ratio=ns/z.scale;const tx=px*(1-ratio)+z.tx*ratio,ty=py*(1-ratio)+z.ty*ratio;lbSetZoom({scale:ns,tx,ty});},{passive:false});
  // 拖动监听挂在 #lightbox 容器上（fixed inset:0 全屏），不受 transform/img 边界影响；水平拖动光标跑出图片时仍能持续 dispatch
  const lbRoot=document.getElementById('lightbox');
  let rafPending=false,lastZ=null;
  function flushZoom(){rafPending=false;if(lastZ)lbSetZoom(lastZ);}
  img.addEventListener('pointerdown',function(e){if(!LB_STATE.open)return;if(e.button!=null&&e.button!==0)return;e.preventDefault();dragging=true;moved=false;sx=e.clientX;sy=e.clientY;oz=lbGetZoom();img.classList.add('grabbing','dragging');try{lbRoot.setPointerCapture(e.pointerId);}catch(_){}});
  lbRoot.addEventListener('pointermove',function(e){if(!dragging||!LB_STATE.open)return;const dx=e.clientX-sx,dy=e.clientY-sy;if(Math.abs(dx)>4||Math.abs(dy)>4)moved=true;lastZ={scale:oz.scale,tx:oz.tx+dx,ty:oz.ty+dy};if(!rafPending){rafPending=true;requestAnimationFrame(flushZoom);}});
  function endDrag(e){if(!dragging)return;dragging=false;img.classList.remove('grabbing','dragging');try{lbRoot.releasePointerCapture(e.pointerId);}catch(_){}}
  lbRoot.addEventListener('pointerup',endDrag);lbRoot.addEventListener('pointercancel',endDrag);
  // 容器层兜底：拖动产生的 click（target=lbImg 或容器本身）一律吞掉，不冒泡到 closeLightbox
  lbRoot.addEventListener('click',function(e){if(moved){e.stopPropagation();e.preventDefault();moved=false;}},true);
  // 双击：以双击点为锚点，1x↔2.5x（用 box 中心为锚点基准）
  img.addEventListener('dblclick',function(e){if(!LB_STATE.open)return;e.preventDefault();const r=img.getBoundingClientRect();const bw=img.clientWidth,bh=img.clientHeight;const z=lbGetZoom();if(z.scale>1.001){lbSetZoom({scale:1,tx:0,ty:0});}else{const cx=r.left+bw/2,cy=r.top+bh/2;const px=e.clientX-cx,py=e.clientY-cy;const ns=2.5;const ratio=ns;const tx=px*(1-ratio)+z.tx*ratio,ty=py*(1-ratio)+z.ty*ratio;lbSetZoom({scale:ns,tx,ty});}});
  // 双指捏合缩放
  img.addEventListener('touchstart',function(e){if(e.touches.length===2){const r=img.getBoundingClientRect();const bw=img.clientWidth,bh=img.clientHeight;const a=e.touches[0],b=e.touches[1];pinch={dist:touchDist(e),z0:lbGetZoom(),mid:{x:(a.clientX+b.clientX)/2-(r.left+bw/2),y:(a.clientY+b.clientY)/2-(r.top+bh/2)}};}},{passive:false});
  img.addEventListener('touchmove',function(e){if(e.touches.length===2&&pinch){e.preventDefault();const d=touchDist(e);const ratio=d/(pinch.dist||1);const ns=Math.max(0.5,Math.min(6,pinch.z0.scale*ratio));const r=img.getBoundingClientRect();const bw=img.clientWidth,bh=img.clientHeight;const a=e.touches[0],b=e.touches[1];const cmx=(a.clientX+b.clientX)/2-(r.left+bw/2),cmy=(a.clientY+b.clientY)/2-(r.top+bh/2);const dratio=ns/pinch.z0.scale;const tx=cmx*(1-dratio)+pinch.z0.tx*dratio,ty=cmy*(1-dratio)+pinch.z0.ty*dratio;lbSetZoom({scale:ns,tx,ty});}},{passive:false});
  img.addEventListener('touchend',function(e){if(e.touches.length<2)pinch=null;});
  function touchDist(e){const a=e.touches[0],b=e.touches[1];return Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);}
})();
document.addEventListener('keydown',function(e){if(!LB_STATE.open)return;if(e.key==='Escape')closeLightbox();if(e.key==='ArrowLeft')changeLightbox(-1);if(e.key==='ArrowRight')changeLightbox(1);});
// —— 同步相册/重置基线 通用弹窗：单一 DOM + mode 状态机，避免“不知道同步/重置了哪段”的困惑 ——
// mode='sync' → 下发增量同步；mode='reset' → 下发重置基线（params 多带 reset:true）。两份共用同一时间窗 UI。
let SYNC_ALBUM_MOD={mode:'sync'};
let SYNC_ALBUM_WIN={preset:null,from_ts:null,to_ts:null};
function albumWindowDesc(f){f=f||{};const labels={'all':'全部','today':'今天','yesterday':'昨天','3d':'近三天','week':'近7天','month':'近30天'};const p=f.preset;if(p&&p!=='custom')return labels[p]||p;const hasFrom=f.from_ts!=null&&typeof f.from_ts!=='undefined';const hasTo=f.to_ts!=null&&typeof f.to_ts!=='undefined';if(!hasFrom&&!hasTo)return '所有照片';const fmtT=t=>{const d=new Date(t);const s=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+s(d.getMonth()+1)+'-'+s(d.getDate())+' '+s(d.getHours())+':'+s(d.getMinutes());};if(hasFrom&&hasTo)return fmtT(f.from_ts)+' 至 '+fmtT(f.to_ts);if(hasFrom)return '从 '+fmtT(f.from_ts);return '至 '+fmtT(f.to_ts);}
function syncAlbumRange(r){const now=Date.now();const day=86400000;let from=null,to=null;if(r==='all'){from=null;to=null;}else if(r==='today'){from=startOfDayTs(now);}else if(r==='yesterday'){from=startOfDayTs(now-day);to=startOfDayTs(now)-1;}else if(r==='3d'){from=now-3*day;}else if(r==='week'){from=now-7*day;}else if(r==='month'){from=now-30*day;}return{from,to};}
function syncAlbumPick(r){const {from,to}=syncAlbumRange(r);SYNC_ALBUM_WIN={preset:r,from_ts:from,to_ts:to};setDtLocal('saFrom',null);setDtLocal('saTo',null);document.querySelectorAll('#syncAlbumPresets button').forEach(b=>b.classList.toggle('active',b.getAttribute('data-r')===r));syncAlbumRefresh();}
function syncAlbumCustom(){const from=dtLocalToTs('saFrom');const to=dtLocalToTs('saTo');if(from==null&&to==null)return;SYNC_ALBUM_WIN={preset:'custom',from_ts:from,to_ts:to};document.querySelectorAll('#syncAlbumPresets button').forEach(b=>b.classList.remove('active'));syncAlbumRefresh();}
function syncAlbumClearCustom(){setDtLocal('saFrom',null);setDtLocal('saTo',null);if(SYNC_ALBUM_WIN.preset==='custom'){SYNC_ALBUM_WIN={preset:null,from_ts:null,to_ts:null};syncAlbumRefresh();}}
function syncAlbumRefresh(){const f=SYNC_ALBUM_WIN,m=SYNC_ALBUM_MOD;const btn=document.getElementById('syncAlbumGo');const desc=document.getElementById('syncAlbumDesc');const hasWin=f.preset!=null;const winLabel=f.preset==='all'?'所有照片（不限时间）':albumWindowDesc(f);const isReset=m.mode==='reset';const icon=isReset?'♻️':'📌';const verb=isReset?'将重置':'将同步';const goText=isReset?'♻️ 重置基线':'🔄 同步相册';desc.textContent=`${icon} ${verb}：`+(hasWin?winLabel:'尚未选择时间段');if(hasWin){btn.disabled=false;btn.textContent=goText;}else{btn.disabled=true;btn.textContent='请先选择时间段';}}
function openSyncAlbumModal(mode){if(!SEL)return;SYNC_ALBUM_MOD={mode:mode||'sync'};SYNC_ALBUM_WIN={preset:null,from_ts:null,to_ts:null};setDtLocal('saFrom',null);setDtLocal('saTo',null);document.querySelectorAll('#syncAlbumPresets button').forEach(b=>b.classList.remove('active'));document.getElementById('syncAlbumDevName').textContent=window.SEL_REMARK||SEL;const title=document.getElementById('syncAlbumTitle');const hint=document.getElementById('syncAlbumHint');if(SYNC_ALBUM_MOD.mode==='reset'){title.textContent='♻️ 重置基线时间窗';hint.innerHTML='请选择要<b>重置基线</b>的时间段。手机会遗忘该范围内已上传的记录，下次同步会重新上传。全部 = 重置全部基线。';}else{title.textContent='📅 同步相册时间窗';hint.innerHTML='请选择本次同步的时间段（必须指定才能同步，避免误传整个媒体库）。<b>“全部”即同步所有照片</b>，请确认清楚。';}syncAlbumRefresh();document.getElementById('syncAlbumModal').style.display='flex';}
function openResetAlbumModal(){openSyncAlbumModal('reset');}
function closeSyncAlbumModal(){document.getElementById('syncAlbumModal').style.display='none';}
async function submitAlbumModal(){const f=SYNC_ALBUM_WIN,m=SYNC_ALBUM_MOD;if(!f.preset){alert('请先选择时间段');return;}const isReset=m.mode==='reset';const params={};if(isReset)params.reset=true;if(f.from_ts!=null)params.from_ts=f.from_ts;if(f.to_ts!=null)params.to_ts=f.to_ts;const winLabel=f.preset==='all'?'所有照片（不限时间）':albumWindowDesc(f);closeSyncAlbumModal();try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'album',params:params})});const verb=isReset?'重置基线':'同步相册';const follow=isReset?'手机将在下次轮询时清除对应记录并重新上传':'手机将只上传该时间窗内尚未上传过的图片（增量同步，最多15分钟内响应）';alert(`已下发：${verb}（${winLabel}）\n${follow}`);}catch(e){alert('下发失败：'+e.message)}}
// 注意：确认按钮直接绑 submitAlbumModal()，mode 由打开弹窗的入口（openSyncAlbumModal('sync'/'reset')）决定；
// 不要在这里再包一层强制设置 mode 的函数——那会让「重置基线」入口点确认时被改回同步，重置永远不生效（2026-08-23 踩坑）。
async function refreshDetail(){loadQueue();loadLocations();loadPhotos();loadRealtimeStatus();}
function openUsagePage(){if(!SEL){alert('请先选择设备');return;}window.open('/anti-theft/usage?device_id='+encodeURIComponent(SEL),'_blank');}
const NOTIFY_PKG={'com.tencent.mm':'微信','com.tencent.mobileqq':'QQ','com.tencent.qqlite':'QQ轻聊','com.android.mms':'短信','com.google.android.apps.messaging':'短信','com.samsung.android.messaging':'短信'};
// ★ 固定 Tab 出场顺序：微信 → QQ（QQ/QQ轻聊合并位次） → 短信。顺序锁死、不要改、不要让后端或前端排序漂移。新增 pkg 默认排到末尾。
const NOTIFY_TAB_ORDER=['com.tencent.mm','com.tencent.mobileqq','com.tencent.qqlite','com.android.mms','com.google.android.apps.messaging','com.samsung.android.messaging'];
const NOTIFY_ORDER={'com.tencent.mm':0,'com.tencent.mobileqq':1,'com.tencent.qqlite':1,'com.android.mms':2,'com.google.android.apps.messaging':2,'com.samsung.android.messaging':2};
let NOTIFY_STATE={apps:[],active_pkg:null,sender:null,page:0,total:0,items:[],filter:{from_ts:null,to_ts:null},selAllSender:null};
let BATCH_NOTIFY=false;
const selNotifyIds=new Set();
const NOTIFY_LIMIT=30;
async function loadNotify(){if(!SEL)return;const tc=document.getElementById('notifyTabContent');if(tc)tc.innerHTML='<div class="hint">加载中…</div>';try{const j=await(await api('/anti-theft/notify-summary?device_id='+encodeURIComponent(SEL)+notifyFilterQs())).json();NOTIFY_STATE.apps=j.apps||[];const first=(NOTIFY_STATE.apps.find(a=>a.pkg==='com.tencent.mm')||NOTIFY_STATE.apps[0]||null);NOTIFY_STATE.active_pkg=first?first.pkg:null;renderNotifyTabs();if(NOTIFY_STATE.active_pkg){const firstSender=isSms(NOTIFY_STATE.active_pkg)?null:(first.senders.length?first.senders[0].title:null);renderNotifyAll(NOTIFY_STATE.active_pkg,firstSender,0);}else if(tc){tc.innerHTML='<div class="empty">暂无通知线索（确认手机已开启「通知监听」且产生过通知）</div>';}}catch(e){if(tc)tc.innerHTML='<div class="empty">通知线索加载失败：'+esc(e.message)+'</div>';}}
function notifyFilterQs(){const f=NOTIFY_STATE.filter;let s='';if(f.from_ts!=null)s+='&from_ts='+f.from_ts;if(f.to_ts!=null)s+='&to_ts='+f.to_ts;return s;}
function renderNotifyTabs(){const tabs=document.getElementById('notifyTabs');if(!tabs)return;// 固定顺序输出：按 NOTIFY_TAB_ORDER 锁定出场位置（微信→QQ→短信），不再依赖 sort，任凭后端顺序如何都不会乱
const ordered=NOTIFY_TAB_ORDER.map(pkg=>NOTIFY_STATE.apps.find(a=>a.pkg===pkg)).filter(Boolean);const extras=NOTIFY_STATE.apps.filter(a=>!NOTIFY_TAB_ORDER.includes(a.pkg));const apps=ordered.concat(extras);if(!apps.length){tabs.innerHTML='';return;}tabs.innerHTML=apps.map(a=>'<button class="uv-tab'+(a.pkg===NOTIFY_STATE.active_pkg?' active':'')+'" onclick="switchNotifyTab(\''+a.pkg+'\')">'+esc(a.app_name)+' ('+a.count+')</button>').join('');}
function switchNotifyTab(pkg){NOTIFY_STATE.active_pkg=pkg;renderNotifyTabs();const app=NOTIFY_STATE.apps.find(a=>a.pkg===pkg);const firstSender=isSms(pkg)?null:(app&&app.senders.length?app.senders[0].title:null);renderNotifyAll(pkg,firstSender,0);}
function isSms(pkg){return pkg.indexOf('mms')>=0||pkg.indexOf('messaging')>=0;}
// 单页面渲染：气泡层 + 聊天记录 + 分页器，一次拼完（无 view 切换）
// 短信 Tab：sender=null，跳过气泡层只渲染聊天记录段
async function renderNotifyAll(pkg,sender,page){
  // 取数据
  let qs='/anti-theft/notify-data?device_id='+encodeURIComponent(SEL)+'&pkg='+encodeURIComponent(pkg)+'&limit='+NOTIFY_LIMIT+'&offset='+(page*NOTIFY_LIMIT);
  if(sender!=null)qs+='&sender='+encodeURIComponent(sender);
  qs+=notifyFilterQs();
  let j;
  try{j=await(await api(qs)).json();}catch(e){const tc=document.getElementById('notifyTabContent');if(tc)tc.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';return;}
  NOTIFY_STATE.total=j.total;NOTIFY_STATE.items=j.items;NOTIFY_STATE.sender=sender;NOTIFY_STATE.page=page;
  const tc=document.getElementById('notifyTabContent');if(!tc)return;
  const app=NOTIFY_STATE.apps.find(a=>a.pkg===pkg);
  const appName=app?app.app_name:(NOTIFY_PKG[pkg]||pkg);
  let h='';
  // 第一段：气泡层（仅微信/QQ，短信 Tab 跳过）
  if(!isSms(pkg)){
    h+='<div class="ops" style="margin-bottom:6px"><button class="ghost nbub-clear" onclick="clearNotifyApp(&#39;'+pkg+'&#39;)">清空本软件所有消息</button></div>';
    h+='<div class="nbub-wrap"><div class="nbub-arrow" onclick="nbubScroll(-1)">‹</div><div class="nbub-scroll" id="nbubScroll">';
    if(!app||!app.senders.length){h+='<div class="hint">该软件暂无会话</div>';}
    else{app.senders.forEach(s=>{const isActive=s.title===sender;const unread=isActive?0:s.unread;const ureadClass=unread>0?' unread':'';const badge=unread>0?'<span class="nb-badge">'+unread+'</span>':'';const st=esc(s.title).replace(/'/g,"&#39;");h+='<div class="nbub'+(isActive?' active':'')+ureadClass+'" onclick="openSender(&#39;'+pkg+'&#39;,&#39;'+st+'&#39;)">'+badge+'<div class="nb-name">'+esc(s.title)+'</div><div class="nb-count">'+s.total+' 条</div></div>';});}
    h+='</div><div class="nbub-arrow" onclick="nbubScroll(1)">›</div></div>';
  }
  // 第二段：聊天记录头部 + 消息列表 + 分页器（永久显示，短信 Tab 时无气泡层但仍拼接）
  h+='<div class="nlist-head"><div class="nb-selected"><span class="nb-selected-name">'+esc(sender!=null?sender:appName)+'</span><span class="nb-selected-meta"> · 共 '+j.total+' 条</span></div></div>';
  if(!j.items.length){h+='<div class="empty">暂无消息</div>';}else{h+='<div id="notifyList">';j.items.forEach(it=>{const sel=BATCH_NOTIFY&&selNotifyIds.has(it.id)?' sel':'';const clk=BATCH_NOTIFY?' onclick="toggleNotifySel('+it.id+')"':'';const showName=sender!=null?sender:(it.title||'');const cleanText=stripNotifyText(it.text,showName);h+='<div class="nitem'+sel+'" data-id="'+it.id+'"'+clk+'><div class="nbody">';if(showName)h+='<span class="nname">'+esc(showName)+'</span>';if(showName&&cleanText)h+='<span class="nsep">：</span>';if(cleanText)h+='<span class="ntext">'+esc(cleanText)+'</span>';h+='</div><div class="ntime">'+fmt(it.ts)+'<br><span style="opacity:.75">'+timeAgo(it.ts)+'</span></div></div>';});h+='</div>';}
  h+=renderPager(j.total,page,pkg,sender);
  tc.innerHTML=h;tc.classList.toggle('batch',BATCH_NOTIFY);
  if(!isSms(pkg)){setTimeout(()=>{const sc=document.getElementById('nbubScroll');if(sc)sc.scrollLeft=0;},0);}
}
function nbubScroll(dir){const sc=document.getElementById('nbubScroll');if(sc)sc.scrollBy({left:dir*300,behavior:'smooth'});}
async function openSender(pkg,sender){try{await api('/anti-theft/notify-read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,pkg,sender})});}catch(e){}const app=NOTIFY_STATE.apps.find(a=>a.pkg===pkg);if(app){const s=app.senders.find(x=>x.title===sender);if(s)s.unread=0;}await renderNotifyAll(pkg,sender,0);}
function jsStr(v){if(v==null)return 'null';if(typeof v==='number'||typeof v==='boolean')return ''+v;return "'"+String(v).replace(/'/g,"\\'").replace(/"/g,'\\"')+"'";}
// ★ 通用压缩翻页算法：数字按钮最多 5 个（含首页/末页），中间用省略号隐藏
// 例：8 页时 → 第1页 [1,2,3,4,…,8] / 第5页 [1,…,4,5,6,…,8] / 第8页 [1,…,5,6,7,8]
function buildPagerItems(page,pages){const cur=page+1;const items=[];if(pages<=5){for(let i=1;i<=pages;i++){items.push(i);}return items;}if(cur<=3){items.push(1,2,3,4,'…');}else if(cur>=pages-2){items.push(1,'…',pages-3,pages-2,pages-1);}else{items.push(1,'…',cur-1,cur,cur+1,'…');}items.push(pages);return items;}
// call = 点击页码要执行的调用前缀（字符串，不带右括号）：
//   带参数前缀传 'gotoPage(\'pkg\',\'sender\''（结尾是参数），无参前缀传 'gotoLocPage('（结尾就是左括号）
// 自动拼上页码(0起)和右括号；结尾为 '(' 时直接拼页码不加逗号
function pagerNumsHTML(page,pages,call){const sep=(call.charAt(call.length-1)==='(')?'':',';let h='';buildPagerItems(page,pages).forEach(function(it){if(it==='…'){h+='<span class="np-info">…</span>';}else{h+='<button class="'+(it===page+1?'cur':'')+'" onclick="'+call+sep+(it-1)+')">'+it+'</button>';}});return h;}
function renderPager(total,page,pkg,sender){const pages=Math.max(1,Math.ceil(total/NOTIFY_LIMIT));const cur=page+1;const pid='npGo_'+cur+'_'+Math.floor(Math.random()*1e9).toString(36);let h='<div class="npager"><span class="np-info">共 '+total+' 条 · 第 '+cur+' / '+pages+' 页</span>';h+='<button '+(page<=0?'disabled':'')+' onclick="gotoPage('+jsStr(pkg)+','+jsStr(sender)+','+(page-1)+')">‹</button>';h+=pagerNumsHTML(page,pages,'gotoPage('+jsStr(pkg)+','+jsStr(sender));h+='<button '+(page>=pages-1?'disabled':'')+' onclick="gotoPage('+jsStr(pkg)+','+jsStr(sender)+','+(page+1)+')">›</button>';h+='<span class="np-go">到第 <input type="number" id="'+pid+'" min="1" max="'+pages+'" placeholder="页" onkeydown="if(event.keyCode===13){goJumpInline('+jsStr(pkg)+','+jsStr(sender)+',&#39;'+pid+'&#39;);event.preventDefault();}"> 页 <button onclick="goJumpInline('+jsStr(pkg)+','+jsStr(sender)+',&#39;'+pid+'&#39;)">Go</button></span>';h+='</div>';return h;}
function goJumpInline(pkg,sender,inputId){const pages=Math.max(1,Math.ceil(NOTIFY_STATE.total/NOTIFY_LIMIT));const inp=document.getElementById(inputId);if(!inp)return;const v=parseInt(inp.value,10);if(!v||v<1||v>pages){alert('请输入 1~'+pages+' 之间的页码');inp.focus();inp.select();return;}inp.value='';gotoPage(pkg,sender,v-1);}
async function gotoPage(pkg,sender,page){const pages=Math.max(1,Math.ceil(NOTIFY_STATE.total/NOTIFY_LIMIT));if(page<0||page>=pages)return;await renderNotifyAll(pkg,sender,page);const sc=document.getElementById('notifyTabContent');if(sc)sc.scrollIntoView({behavior:'smooth',block:'start'});}
function showGoModal(pkg,sender){window._go={pkg:pkg,sender:sender};const pages=Math.max(1,Math.ceil(NOTIFY_STATE.total/NOTIFY_LIMIT));const m=document.getElementById('goModal');document.getElementById('goPageInput2').value='';document.getElementById('goHint').textContent='共 '+pages+' 页，请输入 1~'+pages;m.classList.add('show');setTimeout(()=>{const i=document.getElementById('goPageInput2');if(i)i.focus();},50);}
function closeGoModal(){const m=document.getElementById('goModal');if(m)m.classList.remove('show');}
function goJump(){const pages=Math.max(1,Math.ceil(NOTIFY_STATE.total/NOTIFY_LIMIT));const v=parseInt(document.getElementById('goPageInput2').value,10);if(!v||v<1||v>pages){alert('请输入 1~'+pages+' 之间的页码');return;}const g=window._go;closeGoModal();gotoPage(g.pkg,g.sender,v-1);}
function getNotifySel(){return Array.from(selNotifyIds);}
function onNotifyChk(){updateNotifyBatchBar();const n=selNotifyIds.size;const box=document.getElementById('notifyTabContent');const hint=box?box.querySelector('.np-info'):null;if(hint)hint.textContent='已选 '+n+' 条';}
function updateNotifyBatchBar(){const el=document.getElementById('batchCountNotify');if(!el)return;el.textContent=NOTIFY_STATE.selAllSender?('已选整个好友「'+NOTIFY_STATE.selAllSender.sender+'」'):('已选 '+selNotifyIds.size+' 条');}
function selAllNotifyPage(checked){NOTIFY_STATE.selAllSender=null;const box=document.getElementById('notifyList');if(!box)return;box.querySelectorAll('.nitem').forEach(el=>{const id=parseInt(el.dataset.id,10);if(checked)selNotifyIds.add(id);else selNotifyIds.delete(id);el.classList.toggle('sel',checked);});onNotifyChk();}
function selAllNotifySender(){if(NOTIFY_STATE.sender==null){alert('短信/应用层无「整个好友」概念');return;}NOTIFY_STATE.selAllSender={pkg:NOTIFY_STATE.active_pkg,sender:NOTIFY_STATE.sender};selNotifyIds.clear();updateNotifyBatchBar();const box=document.getElementById('notifyTabContent');const hint=box?box.querySelector('.np-info'):null;if(hint)hint.textContent='已选整个好友「'+NOTIFY_STATE.sender+'」全部消息';}
function toggleNotifySel(id){if(selNotifyIds.has(id))selNotifyIds.delete(id);else selNotifyIds.add(id);const el=document.querySelector('#notifyList .nitem[data-id="'+id+'"]');if(el)el.classList.toggle('sel',selNotifyIds.has(id));onNotifyChk();}
function toggleNotifyBatch(){BATCH_NOTIFY=!BATCH_NOTIFY;const btn=document.getElementById('batchBtnNotify');const bar=document.getElementById('batchBarNotify');if(BATCH_NOTIFY){btn.style.display='none';bar.style.display='inline-flex';}else{btn.style.display='inline-block';bar.style.display='none';const sa=document.getElementById('selAllNotify');if(sa)sa.checked=false;selNotifyIds.clear();NOTIFY_STATE.selAllSender=null;}if(NOTIFY_STATE.active_pkg){renderNotifyAll(NOTIFY_STATE.active_pkg,NOTIFY_STATE.sender,NOTIFY_STATE.page);}updateNotifyBatchBar();}
async function delNotifySel(){if(NOTIFY_STATE.selAllSender){const ss=NOTIFY_STATE.selAllSender;if(!confirm('确定删除【'+(NOTIFY_PKG[ss.pkg]||ss.pkg)+'】的「'+ss.sender+'」全部消息（跨页）？此操作不可恢复。'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,pkg:ss.pkg,sender:ss.sender})});const j=await r.json();NOTIFY_STATE.selAllSender=null;loadNotify();setTimeout(()=>alert('已删除 '+j.deleted+' 条'),0);}catch(e){alert('删除失败：'+e.message);}return;}const ids=getNotifySel();if(!ids.length){alert('请先勾选要删除的通知');return;}if(!confirm('确定删除选中的 '+ids.length+' 条通知？'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});const j=await r.json();selNotifyIds.clear();if(NOTIFY_STATE.active_pkg){renderNotifyAll(NOTIFY_STATE.active_pkg,NOTIFY_STATE.sender,NOTIFY_STATE.page);}setTimeout(()=>alert('已删除 '+j.deleted+' 条'),0);}catch(e){alert('删除失败：'+e.message);}}
async function clearNotifyApp(pkg){const name=NOTIFY_PKG[pkg]||pkg;if(!confirm('确定清空【'+name+'】的所有通知线索？此操作不可恢复。'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,pkg})});const j=await r.json();loadNotify();setTimeout(()=>alert('已清空 '+j.deleted+' 条'),0);}catch(e){alert('清空失败：'+e.message);}}
async function clearNotify(){if(!SEL){alert('缺少 device_id');return;}if(!confirm('确定清空本设备的所有通知线索（微信/QQ/短信全部）？此操作不可恢复。'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,all:true})});const j=await r.json();selNotifyIds.clear();NOTIFY_STATE.selAllSender=null;loadNotify();setTimeout(()=>alert('已清空 '+j.deleted+' 条'),0);}catch(e){alert('清空失败：'+e.message);}}
function toggleNotifyFilter(){const f=document.getElementById('notifyFilter');if(f)f.style.display=(f.style.display==='none'||!f.style.display)?'block':'none';}
function startOfDayTs(ts){const d=new Date(ts);d.setHours(0,0,0,0);return d.getTime();}
function setDtLocal(id,ts){const el=document.getElementById(id);if(!el)return;if(ts==null){el.value='';return;}const d=new Date(ts);const p=n=>String(n).padStart(2,'0');el.value=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes());}
function dtLocalToTs(id){const el=document.getElementById(id);if(!el||!el.value)return null;const t=Date.parse(el.value);return isNaN(t)?null:t;}
function setNotifyRange(r){const now=Date.now();const day=86400000;let from=null,to=null;if(r==='today'){from=startOfDayTs(now);}else if(r==='yesterday'){from=startOfDayTs(now-day);to=startOfDayTs(now)-1;}else if(r==='3d'){from=now-3*day;}else if(r==='week'){from=now-7*day;}else if(r==='month'){from=now-30*day;}else if(r==='all'){from=null;to=null;}NOTIFY_STATE.filter={from_ts:from,to_ts:to};setDtLocal('nfFrom',from);setDtLocal('nfTo',to);loadNotify();}
function applyNotifyFilter(){const from=dtLocalToTs('nfFrom');const to=dtLocalToTs('nfTo');NOTIFY_STATE.filter={from_ts:from,to_ts:to};loadNotify();}
function clearNotifyFilter(){NOTIFY_STATE.filter={from_ts:null,to_ts:null};const f=document.getElementById('nfFrom');if(f)f.value='';const t=document.getElementById('nfTo');if(t)t.value='';loadNotify();}
document.addEventListener('keydown',function(e){if(e.key==='Escape'){const m=document.getElementById('goModal');if(m&&m.classList.contains('show'))closeGoModal();}if(e.key==='Enter'&&document.activeElement&&document.activeElement.id==='goPageInput2'){goJump();}});
async function loadRealtimeStatus(){if(!SEL)return;try{const st=await(await api('/anti-theft/status?device_id='+SEL)).json();const el=document.getElementById('rtState');if(!el)return;window._rt=st.realtime;el.textContent='状态：'+(st.realtime?'已开启（秒级）':'已关闭（轮询兜底）');}catch(e){}}
async function toggleRealtime(on){if(!SEL)return;try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'realtime',params:{on:on}})});if(on)alert('已下发：开启实时通道（手机下次轮询≤15分钟内建立，之后命令秒级到达）');else alert('已下发：关闭实时通道');setTimeout(loadRealtimeStatus,1500);}catch(e){alert('下发失败：'+e.message)}}
async function openRealtime(){if(!SEL)return;try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'activate',params:{locate:0,photo:0}})});await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'realtime',params:{on:true}})});alert('已下发：激活 + 开启实时通道（手机下次轮询≤15分钟内建立 WS，之后命令秒级到达）');setTimeout(loadRealtimeStatus,1500);}catch(e){alert('下发失败：'+e.message)}}

async function cmd(c){try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:c})});alert('已下发：'+c+'（手机最多15分钟内响应）');}catch(e){alert('下发失败：'+e.message)}}
async function cmdPhoto(facing){if(!SEL)return;try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'photo',params:{facing:facing}})});alert('已下发：拍照（'+(facing==='back'?'后摄':'前摄')+'）（手机最多15分钟内响应；解锁/亮屏态立即触发，锁屏态可能点亮屏幕一闪）');}catch(e){alert('下发失败：'+e.message)}}
let pending=null;
function confirmCmd(c,label){pending=c;document.getElementById('modalText').textContent='确认要对该设备执行【'+label+'】？此操作不可撤销。';document.getElementById('modal').style.display='flex'}
function closeModal(){document.getElementById('modal').style.display='none';pending=null}
async function modalOk(){const c=pending;closeModal();if(c)await cmd(c)}
function saveTok(){TOKEN=document.getElementById('tok').value.trim();localStorage.setItem(LS,TOKEN);loadDevices()}
let wipeTimer=null;
function requestWipe(){
  const m=document.getElementById('wipeModal'); m.style.display='flex';
  document.getElementById('wipePhrase').value='';
  document.getElementById('wipeFinalAck').checked=false;
  window._wipeAck=false;
  const btn=document.getElementById('wipeFinal'); btn.disabled=true; btn.textContent='5 秒后可确认';
  clearInterval(wipeTimer); let n=5;
  wipeTimer=setInterval(()=>{n--; if(n<=0){clearInterval(wipeTimer); btn.textContent='确认擦除'; btn.disabled=false;} else {btn.textContent=n+' 秒后可确认';}},1000);
}
function wipeCheck(){
  const phraseOk=document.getElementById('wipePhrase').value.trim()==='擦除';
  const ackOk=document.getElementById('wipeFinalAck').checked;
  window._wipeAck=phraseOk&&ackOk;
}
function doWipe(){
  if(!window._wipeAck) return;
  if(document.getElementById('wipeFinal').disabled) return;
  closeWipeModal(); cmd('wipe');
}
function closeWipeModal(){document.getElementById('wipeModal').style.display='none'; clearInterval(wipeTimer);}
let delDevTimer=null;
function confirmDeleteDevice(){
  const m=document.getElementById('delDevModal');
  document.getElementById('delDevName').textContent=window.SEL_REMARK||SEL;
  document.getElementById('delDevPhrase').value='';
  document.getElementById('delDevAck').checked=false;
  window._delDevAck=false;
  const btn=document.getElementById('delDevFinal'); btn.disabled=true; btn.textContent='5 秒后可确认';
  clearInterval(delDevTimer); let n=5;
  delDevTimer=setInterval(()=>{n--; if(n<=0){clearInterval(delDevTimer); btn.textContent='确认删除'; btn.disabled=false;} else {btn.textContent=n+' 秒后可确认';}},1000);
  m.style.display='flex';
}
function delDevCheck(){
  const phraseOk=document.getElementById('delDevPhrase').value.trim()==='删除';
  const ackOk=document.getElementById('delDevAck').checked;
  window._delDevAck=phraseOk&&ackOk;
}
function doDelDev(){
  if(!window._delDevAck) return;
  if(document.getElementById('delDevFinal').disabled){alert('请等待冷却结束再确认');return;}
  closeDelDevModal();
  delDevExec();
}
async function delDevExec(){
  try{
    await api('/anti-theft/device/'+encodeURIComponent(SEL),{method:'DELETE'});
    const deleted=SEL; SEL=null; loadDevices();
    document.getElementById('detail').innerHTML='<div class="hint">设备已删除</div>';
    alert('已删除设备：'+deleted);
  }catch(e){alert('删除失败：'+e.message)}
}
function closeDelDevModal(){document.getElementById('delDevModal').style.display='none'; clearInterval(delDevTimer);}
let cleanupTimer=null;
let _cleanupPreview=null;
const _CLEANUP_NAME={locate:'定位',photo:'照片',album:'相册',command:'指令'};
async function openCleanupModal(){
  const m=document.getElementById('cleanupModal');
  document.getElementById('cleanupDevName').textContent=window.SEL_REMARK||SEL;
  const days=parseInt(document.getElementById('cleanDays').value||'90',10);
  const types=[...document.querySelectorAll('.cleanType:checked')].map(e=>e.value);
  if(!types.length){alert('请至少选择一种数据类型');return;}
  const pv=document.getElementById('cleanupPreview');
  pv.textContent='正在预估影响…';
  m.style.display='flex';
  try{
    const qs='/anti-theft/cleanup-preview?device_id='+encodeURIComponent(SEL)+'&days='+days+'&types='+types.map(encodeURIComponent).join('&types=');
    const j=await(await api(qs)).json();
    _cleanupPreview=j;
    const p=j.preview||{};
    const lines=Object.keys(p).filter(k=>p[k]>0).map(k=>'· '+(_CLEANUP_NAME[k]||k)+'：'+p[k]+' 条');
    pv.innerHTML='将删除 <b>'+j.days+' 天前</b> 的：<br>'+(lines.length?lines.join('<br>'):'（所选范围内无符合条件的数据）');
  }catch(e){
    pv.textContent='预估失败：'+e.message;
    _cleanupPreview=null;
  }
  document.getElementById('cleanupPhrase').value='';
  document.getElementById('cleanupFinalAck').checked=false;
  window._cleanupAck=false;
  const btn=document.getElementById('cleanupFinal'); btn.disabled=true; btn.textContent='5 秒后可确认';
  clearInterval(cleanupTimer); let n=5;
  cleanupTimer=setInterval(()=>{n--; if(n<=0){clearInterval(cleanupTimer); btn.textContent='确认清理'; btn.disabled=false;} else {btn.textContent=n+' 秒后可确认';}},1000);
}
function cleanupCheck(){
  const phraseOk=document.getElementById('cleanupPhrase').value.trim()==='清理';
  const ackOk=document.getElementById('cleanupFinalAck').checked;
  window._cleanupAck=phraseOk&&ackOk;
}
async function doCleanup(){
  if(!window._cleanupAck) return;
  if(document.getElementById('cleanupFinal').disabled){alert('请等待冷却结束再确认');return;}
  if(!_cleanupPreview){alert('预估信息缺失，请重新打开弹窗');return;}
  const days=_cleanupPreview.days, types=_cleanupPreview.types;
  closeCleanupModal();
  try{
    const r=await api('/anti-theft/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,days:days,types:types})});
    const j=await r.json();
    const d=j.deleted||{};
    const total=Object.values(d).reduce((a,b)=>a+(b||0),0);
    alert('已清理：'+total+' 条记录'+(j.deleted_files?('，删除 '+j.deleted_files+' 个磁盘文件'):''));
    loadDetail();
  }catch(e){alert('清理失败：'+e.message)}
}
function closeCleanupModal(){document.getElementById('cleanupModal').style.display='none'; clearInterval(cleanupTimer);}
function cmdLabel(c,params){params=params||{};if(c==='activate'){const parts=[];if(params.locate)parts.push('定位');if(params.photo)parts.push('拍照');return '激活'+(parts.length?('('+parts.join('+')+')'):'');}if(c==='locate')return '定位(单次)';if(c==='photo')return '拍照(单次)';if(c==='lock')return '锁屏';if(c==='wipe')return '擦除';if(c==='stop')return '停止服务';return c;}
async function loadQueue(){if(!SEL)return;try{const j=await(await api('/anti-theft/commands?device_id='+SEL)).json();const box=document.getElementById('queue');if(!box)return;const list=j.commands.slice(0,10);if(!list.length){box.innerHTML='<div class="hint">队列为空（无待执行指令）</div>';return;}let h='';list.forEach(x=>{const st=x.status==='pending'?'<span class="st pending">待执行</span>':'<span class="st dispatched">已下发</span>';const del=x.status==='pending'?'<button class="ghost" onclick="delCmd('+x.id+')">删除</button>':'';h+='<div class="qrow"><div class="meta">#'+x.id+' '+esc(cmdLabel(x.cmd,x.params))+' · '+fmt(x.created_at)+'</div>'+st+del+'</div>';});box.innerHTML=h;}catch(e){}}
async function delCmd(id){try{await api('/anti-theft/command/'+id,{method:'DELETE'});loadQueue();}catch(e){alert('删除失败：'+e.message)}}
async function clearQueue(){if(!SEL)return;if(!confirm('确认清空所有待执行指令？（已下发的不可撤）'))return;try{const j=await(await api('/anti-theft/commands?device_id='+SEL,{method:'DELETE'})).json();loadQueue();alert('已清空 '+(j.deleted||0)+' 条待执行指令');}catch(e){alert('清空失败：'+e.message)}}
// 自动刷新已全部关闭，改为手动：点悬浮 ⟳ 按钮（initFab→loadDetail）或设备详情「刷新本设备」按钮即可刷新全部数据。
(function initFab(){
  const fab=document.getElementById('fabRefresh');if(!fab||!fab.style)return;
  let dragging=false,startX,startY,startLeft,startTop,startTime,hasMoved=false;
  const onDown=function(e){dragging=false;hasMoved=false;startX=e.clientX||e.touches[0].clientX;startY=e.clientY||e.touches[0].clientY;const rect=fab.getBoundingClientRect();startLeft=rect.left;startTop=rect.top;startTime=Date.now();};
  const onMove=function(e){if(startX===undefined)return;const cx=e.clientX!==undefined?e.clientX:e.touches[0].clientX;const cy=e.clientY!==undefined?e.clientY:e.touches[0].clientY;const dx=cx-startX,dy=cy-startY;if(Math.abs(dx)>3||Math.abs(dy)>3){dragging=true;hasMoved=true;}if(dragging){e.preventDefault();fab.style.left=Math.max(0,Math.min(window.innerWidth-56,startLeft+dx))+'px';fab.style.top=Math.max(0,Math.min(window.innerHeight-56,startTop+dy))+'px';fab.style.right='auto';fab.style.bottom='auto';}};
  const onUp=function(e){if(!dragging&&Date.now()-startTime<400){loadDevices();if(SEL)loadDetail();}startX=undefined;startY=undefined;dragging=false;};
  fab.addEventListener('mousedown',onDown);fab.addEventListener('touchstart',onDown,{passive:false});
  document.addEventListener('mousemove',onMove);document.addEventListener('touchmove',onMove,{passive:false});
  document.addEventListener('mouseup',onUp);document.addEventListener('touchend',onUp);
})();
// ---- 应用使用分析（内嵌视图，与主控制台共用一个 URL，整体切换）----
function escapeHtml(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmtDur(ms){if(!ms)return '0';const s=Math.floor(ms/1000);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;if(h>0)return h+'小时'+m+'分';if(m>0)return m+'分'+sec+'秒';return sec+'秒';}
function startOfDay(d){const x=new Date(d);x.setHours(0,0,0,0);return x.getTime();}
function startOfWeek(d){const x=new Date(startOfDay(d));const dow=(x.getDay()+6)%7;x.setDate(x.getDate()-dow);return x.getTime();}
function startOfMonth(d){const x=new Date(startOfDay(d));x.setDate(1);return x.getTime();}
function rangeBounds(sel){const now=Date.now();const D=86400000;
  if(sel==='today')return[startOfDay(now),now];
  if(sel==='yesterday'){const t=startOfDay(now);return[t-D,t];}
  if(sel==='3d')return[now-3*D,now];
  if(sel==='week')return[startOfWeek(now),now];
  if(sel==='lastweek'){const w=startOfWeek(now);return[w-7*D,w];}
  if(sel==='month')return[now-30*D,now];
  if(sel==='lastmonth'){const m=startOfMonth(now);const lm=new Date(m);lm.setMonth(lm.getMonth()-1);return[lm.getTime(),m];}
  return[0,now];}
function isSystemPkg(pkg){const p=pkg||'';const list=['android','com.android.','com.google.android.','com.qualcomm.','com.qti.','com.miui.','com.mi.','com.xiaomi.','com.bsp.','com.mobiletools.','com.huawei.','com.samsung.','com.lge.','com.oneplus.','com.coloros.','com.oppo.','com.vivo.','com.realme.','com.heytap.','com.meizu.','com.smartisan.','com.lenovo.','com.zui.','com.motorola.','com.sony.','com.asus.','com.mediatek.','com.sec.','com.sonyericsson.','com.amazon.'];return list.some(x=>p===x||p.startsWith(x));}
let UV_DATA=null;
async function requestUsage(){if(!SEL){alert('缺少设备');return;}const el=document.getElementById('uvStatus');if(el)el.textContent='正在下发指令...';try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'usage'})});if(el)el.textContent='已下发：获取应用使用数据（在线/WS 秒级，离线最多 15 分钟）；稍后点「重新载入」。';}catch(e){if(el)el.textContent=''; alert('下发失败：'+e.message);}}
async function u_loadData(){if(!TOKEN){alert('请先在控制台顶部保存 Token');return;}if(!SEL){alert('请先选择设备');return;}const el=document.getElementById('uvStatus');if(el)el.textContent='正在载入...';try{const j=await(await api('/anti-theft/usage-data?device_id='+encodeURIComponent(SEL))).json();UV_DATA=j;const dv=document.getElementById('uvDev');if(dv)dv.textContent=(window.SEL_REMARK||SEL)+' · 设备ID '+SEL+''+(j.ts?' · 采集于 '+timeAgo(j.ts):' · 未采集');renderUsage();renderFg();renderInstalled();const usageCnt=(j.usage||[]).length;const instCnt=(j.installed||[]).length;const hasLabel=usageCnt>0 && j.usage.some(b=>b.label);const warn=document.getElementById('uvWarning');if(warn){if(usageCnt===0&&instCnt===0){warn.style.display='block';warn.innerHTML='<b>⚠ 该设备暂无使用数据</b><br>请确认：①控制台「选择设备」选的是这台手机；②手机端已点「获取/刷新数据」且上传成功（logcat 应有 usage uploaded=true）；③服务端已部署最新版。三者任一不符都会导致这里空白。';}else if(usageCnt>0 && !hasLabel){warn.style.display='block';warn.innerHTML='<b>⚠ 此设备上传的是旧版客户端数据</b><br>应用名无法显示（只能看到包名）。请卸载手机上旧 APK，安装最新版后重新点击「获取/刷新数据」。';}else{warn.style.display='none';warn.textContent='';}}if(el)el.textContent='载入完成：使用记录 '+usageCnt+' 条，已装应用 '+instCnt+' 个'+(j.ts?'，采集于 '+timeAgo(j.ts):'');}catch(e){if(el)el.textContent='载入失败：'+e.message;}}
function renderUsage(){if(!UV_DATA){return;}const sel=document.getElementById('rangeSel').value;window._UV_RANGE=rangeBounds(sel);const[s,e]=window._UV_RANGE;const rows=(UV_DATA.usage||[]).filter(b=>!isSystemPkg(b.pkg));const labels={};(UV_DATA.installed||[]).forEach(a=>labels[a.pkg]=a.label);window._UV_LABELS=labels;const map={};rows.forEach(b=>{const lu=b.last_used;if(lu==null||lu<s||lu>=e)return;const p=b.pkg;if(!map[p])map[p]={total:0,last:0,first:lu,launches:0,label:b.label||labels[p]||p};map[p].total+=(b.total_ms||0);if(lu>map[p].last)map[p].last=lu;if(lu<map[p].first)map[p].first=lu;map[p].launches+=(b.launches||0);});const arr=Object.keys(map).map(p=>Object.assign({pkg:p},map[p]));arr.sort((a,b)=>b.last-a.last);
  const box=document.getElementById('usageTable');document.getElementById('usageSummary').textContent='共 '+arr.length+' 个应用';
  if(!arr.length){box.innerHTML='<div class="empty">该范围内无使用记录（或尚未采集数据）</div>';return;}
  let h='<table id="uvUsageTable"><tr><th>#</th><th>应用</th><th>最近使用</th><th class="num">区间使用时长</th><th class="num">打开次数</th></tr>';
  arr.forEach((a,i)=>{h+='<tr class="uv-row" data-pkg="'+escapeHtml(a.pkg)+'" onclick="toggleSessions(this)"><td>'+(i+1)+'</td><td>'+escapeHtml(a.label)+'<br><span style="color:var(--muted);font-size:12px">'+escapeHtml(a.pkg)+'</span></td><td>'+timeAgo(a.last)+'</td><td class="num">'+fmtDur(a.total)+'</td><td class="num">'+(a.launches||0)+'</td></tr>';});
  h+='</table>';box.innerHTML=h;}
function fmtTime(ts){const d=new Date(ts);return d.getFullYear()+'/'+(d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0');}
function toggleSessions(row){const pkg=row.getAttribute('data-pkg');const next=row.nextElementSibling;if(next&&next.classList.contains('uv-sessions')){next.remove();row.classList.remove('open');return;}row.classList.add('open');const[s,e]=window._UV_RANGE||[0,Date.now()];const labels=window._UV_LABELS||{};const all=(UV_DATA.sessions||[]).filter(x=>x.pkg===pkg&&x.start>=s&&x.start<e).sort((a,b)=>b.start-a.start);const tr=document.createElement('tr');tr.className='uv-sessions';let inner='<div class="inner"><b>'+escapeHtml(labels[pkg]||pkg)+'</b> <span style="color:var(--muted)">'+escapeHtml(pkg)+'</span>';if(!all.length){inner+='<div class="hint">该时间范围内没有详细会话记录（尚未采集或超出 30 天会话保留期）</div></div>';tr.innerHTML='<td colspan="5">'+inner+'</td>';row.parentNode.insertBefore(tr,row.nextSibling);return;}inner+='<table><tr><th>开始时间</th><th>结束时间</th><th class="num">使用时长</th></tr>';all.forEach(se=>{inner+='<tr><td>'+fmtTime(se.start)+'</td><td>'+fmtTime(se.end)+'</td><td class="num">'+fmtDur(se.duration_ms)+'</td></tr>';});inner+='</table><div class="hint">共 '+all.length+' 次使用记录</div></div>';tr.innerHTML='<td colspan="5">'+inner+'</td>';row.parentNode.insertBefore(tr,row.nextSibling);}
function renderFg(){const box=document.getElementById('fgBox');let fg=UV_DATA&&UV_DATA.foreground;if(!fg||isSystemPkg(fg.pkg)){box.innerHTML='<div class="empty">暂无前台 App 数据（采集时未授权或无前台应用）</div>';return;}const labels={};(UV_DATA.installed||[]).forEach(a=>labels[a.pkg]=a.label);const name=fg.label||labels[fg.pkg]||fg.pkg;box.innerHTML='<div class="uv-fg"><b>当前前台 App：</b>'+escapeHtml(name)+' <span style="color:var(--muted)">'+escapeHtml(fg.pkg)+'</span></div><div class="hint">采集时间：'+fmt(fg.ts)+'（'+timeAgo(fg.ts)+'）</div><div class="hint">注：为采集时刻快照，非实时。</div>';}
function renderInstalled(){if(!UV_DATA)return;const f=(document.getElementById('instFilter').value||'').toLowerCase();const list=(UV_DATA.installed||[]).filter(a=>!f||(a.label||'').toLowerCase().includes(f)||(a.pkg||'').toLowerCase().includes(f));const box=document.getElementById('instBox');if(!list.length){box.innerHTML='<div class="empty">无匹配应用</div>';return;}let h='<div class="hint" style="margin:0 0 8px">共 '+list.length+' 个用户应用（已隐藏系统应用）</div><div class="uv-list">';list.forEach(a=>{h+='<div class="uv-item"><div class="l">'+escapeHtml(a.label||a.pkg)+'</div><div class="p">'+escapeHtml(a.pkg)+'</div></div>';});h+='</div>';box.innerHTML=h;}
function u_switchTab(name){document.querySelectorAll('.uv-tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.uv-tab-content').forEach(c=>c.classList.remove('active'));document.getElementById('tab-'+name).classList.add('active');document.getElementById('content-'+name).classList.add('active');}
function showUsage(){if(!SEL){alert('请先选择设备');return;}const mv=document.getElementById('mainView');const uv=document.getElementById('usageView');const dt=document.querySelector('.dev-toggle-m');const fab=document.getElementById('fabRefresh');if(mv)mv.style.display='none';if(uv)uv.style.display='block';if(dt)dt.style.display='none';if(fab)fab.style.display='none';u_loadData();}
function hideUsage(){const mv=document.getElementById('mainView');const uv=document.getElementById('usageView');const dt=document.querySelector('.dev-toggle-m');const fab=document.getElementById('fabRefresh');if(mv)mv.style.display='';if(uv)uv.style.display='none';if(dt)dt.style.display='';if(fab)fab.style.display='';}
function showNotify(){if(!SEL){alert('请先选择设备');return;}const mv=document.getElementById('mainView');const uv=document.getElementById('usageView');const nv=document.getElementById('notifyView');const dt=document.querySelector('.dev-toggle-m');const fab=document.getElementById('fabRefresh');if(mv)mv.style.display='none';if(uv)uv.style.display='none';if(nv)nv.style.display='block';if(dt)dt.style.display='none';if(fab)fab.style.display='none';const dev=document.getElementById('ntDev');if(dev)dev.textContent=SEL_REMARK||SEL;loadNotify();}
function hideNotify(){const mv=document.getElementById('mainView');const nv=document.getElementById('notifyView');const dt=document.querySelector('.dev-toggle-m');const fab=document.getElementById('fabRefresh');if(mv)mv.style.display='';if(nv)nv.style.display='none';if(dt)dt.style.display='';if(fab)fab.style.display='';}
loadDevices();
</script>
</body>
</html>"""
    # 注入地图 JS 的高德 Key（Web 端 JS API Key，与逆地理用的 AMAP_WEB_KEY 是两个不同的 Key）
    # AMAP_JS_CODE 是同一 JS Key 配套的「安全密钥」，2021-12-02 之后创建的 Key 必须配置
    js_key = os.environ.get("AMAP_JS_KEY", "")
    js_code = os.environ.get("AMAP_JS_CODE", "")
    return html.replace("{AMAP_JS_KEY}", js_key).replace("{AMAP_JS_CODE}", js_code)


@router.get("/anti-theft/console")
async def at_console():
    return Response(content=_console_html(), media_type="text/html; charset=utf-8")


@router.get("/anti-theft/usage")
async def at_usage_page(device_id: str = Query(None)):
    return Response(content=_usage_html(device_id), media_type="text/html; charset=utf-8")


def _usage_html(device_id):
    """应用使用分析独立页面：三标签（使用时长/打开记录、当前前台 App、已装 App）+ 时间范围过滤。"""
    did = json.dumps(device_id or "")
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>应用使用分析</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8ef;--text:#1f2733;--muted:#6b7686;--primary:#3b6cff;--primary-d:#2d54d8;--warn:#e67e22;--danger:#d6453b}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px}
header{position:sticky;top:0;z-index:5;padding:12px 16px;background:linear-gradient(90deg,#1f2a44,#2d3d5e);color:#fff;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;flex:1}
header a{color:#fff;text-decoration:none;font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border-radius:12px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:14px}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:12px;flex-wrap:wrap}
.tab{background:transparent;border:none;padding:9px 14px;cursor:pointer;font-size:14px;color:var(--muted);border-radius:8px 8px 0 0;border-bottom:2px solid transparent}
.tab.active{color:var(--primary);font-weight:600;border-bottom-color:var(--primary)}
.tab-content{display:none}
.tab-content.active{display:block}
.ops{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
select,input[type=text]{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;background:#fff;color:var(--text)}
button{cursor:pointer;border:none;border-radius:8px;padding:8px 14px;font-size:14px;background:var(--primary);color:#fff}
button.ghost{background:#eef1f7;color:var(--text)}
button:active{transform:translateY(1px)}
.hint{color:var(--muted);font-size:12px;margin:6px 0;line-height:1.5}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;background:#fafbfc}
tr:hover td{background:#f7f9fc}
.num{text-align:right;font-variant-numeric:tabular-nums}
.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;max-height:60vh;overflow:auto}
.item{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.item .l{font-weight:600}
.item .p{color:var(--muted);font-size:12px;word-break:break-all}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:6px;margin-left:6px}
.tag.sys{background:#fdecea;color:var(--danger)}
.tag.usr{background:#e8f5e9;color:#2e7d32}
.fg{font-size:15px}
.empty{text-align:center;color:var(--muted);padding:24px}
.warn{color:var(--warn)}
@media (max-width:768px){.wrap{padding:12px}table{font-size:12px}th,td{padding:6px 7px}header h1{font-size:15px}}
</style></head>
<body>
<header><a href="/anti-theft/console">← 控制台</a><h1>应用使用分析</h1><span id="devName" style="font-size:13px;opacity:.85"></span></header>
<div class="wrap">
  <div class="card" id="tokCard" style="display:none">
    <div class="hint">控制端 Token 未保存，请粘贴后保存：</div>
    <div class="ops"><input type="text" id="tokInput" placeholder="控制端 Token" style="flex:1;min-width:160px"><button onclick="saveTok2()">保存</button></div>
  </div>
  <div class="card">
    <div class="ops">
      <button onclick="requestUsage()">获取/刷新数据</button>
      <button class="ghost" onclick="loadData()">重新载入</button>
      <span id="statusHint" class="hint"></span>
    </div>
    <div class="hint">数据由手机端「应用使用情况」指令采集后上传（手动触发，不自动上报）。点击「获取/刷新数据」下发指令，手机（在线/WS 秒级，离线最多 15 分钟）响应后，再点「重新载入」。</div>
  </div>
  <div class="card">
    <div class="tabs">
      <button class="tab active" id="tab-usage" onclick="switchTab('usage')">使用时长 / 打开记录</button>
      <button class="tab" id="tab-fg" onclick="switchTab('fg')">当前前台 App</button>
      <button class="tab" id="tab-inst" onclick="switchTab('inst')">已装 App</button>
    </div>
    <div class="tab-content active" id="content-usage">
      <div class="ops">
        <span class="hint" style="margin:0">时间范围：</span>
        <select id="rangeSel" onchange="renderUsage()">
          <option value="today">今天</option>
          <option value="yesterday">昨天</option>
          <option value="3d">近三天</option>
          <option value="week">本周</option>
          <option value="lastweek">上一周</option>
          <option value="month">近一个月</option>
          <option value="lastmonth">上个月</option>
        </select>
        <span class="hint" id="usageSummary" style="margin:0"></span>
      </div>
      <div id="usageTable"></div>
    </div>
    <div class="tab-content" id="content-fg">
      <div id="fgBox"><div class="empty">暂无数据</div></div>
    </div>
    <div class="tab-content" id="content-inst">
      <div class="ops"><input type="text" id="instFilter" placeholder="筛选应用名/包名" oninput="renderInstalled()" style="flex:1;min-width:160px"></div>
      <div id="instBox"><div class="empty">暂无数据</div></div>
    </div>
  </div>
</div>
<script>
const LS='at_ctrl_token';
let TOKEN=localStorage.getItem(LS)||'';
let SEL=__DID__;
let DATA=null;
function hdr(){return TOKEN?{'Authorization':'Bearer '+TOKEN}:{}}
async function api(p,o={}){o.headers=Object.assign(hdr(),o.headers||{});const r=await fetch(p,o);if(!r.ok)throw new Error(r.status+' '+(await r.text()));return r}
function fmt(ts){if(!ts)return '-';const d=new Date(ts);const p=n=>(n<10?'0':'')+n;return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function timeAgo(ts){if(!ts)return '-';const s=Math.max(0,Math.floor((Date.now()-ts)/1000));if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';if(s<86400)return Math.floor(s/3600)+'小时前';return Math.floor(s/86400)+'天前';}
function fmtDur(ms){if(!ms)return '0';const s=Math.floor(ms/1000);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;if(h>0)return h+'小时'+m+'分';if(m>0)return m+'分'+sec+'秒';return sec+'秒';}
function startOfDay(d){const x=new Date(d);x.setHours(0,0,0,0);return x.getTime();}
function startOfWeek(d){const x=new Date(startOfDay(d));const dow=(x.getDay()+6)%7;x.setDate(x.getDate()-dow);return x.getTime();}
function startOfMonth(d){const x=new Date(startOfDay(d));x.setDate(1);return x.getTime();}
function rangeBounds(sel){const now=Date.now();const D=86400000;
  if(sel==='today')return[startOfDay(now),now];
  if(sel==='yesterday'){const t=startOfDay(now);return[t-D,t];}
  if(sel==='3d')return[now-3*D,now];
  if(sel==='week')return[startOfWeek(now),now];
  if(sel==='lastweek'){const w=startOfWeek(now);return[w-7*D,w];}
  if(sel==='month')return[now-30*D,now];
  if(sel==='lastmonth'){const m=startOfMonth(now);const lm=new Date(m);lm.setMonth(lm.getMonth()-1);return[lm.getTime(),m];}
  return[0,now];}
function switchTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));document.getElementById('tab-'+name).classList.add('active');document.getElementById('content-'+name).classList.add('active');}
function saveTok2(){TOKEN=document.getElementById('tokInput').value.trim();if(!TOKEN){alert('请输入 Token');return;}localStorage.setItem(LS,TOKEN);document.getElementById('tokCard').style.display='none';loadData();}
async function requestUsage(){if(!SEL){alert('缺少设备');return;}try{await api('/anti-theft/command',{method:'POST',body:JSON.stringify({device_id:SEL,cmd:'usage'})});document.getElementById('statusHint').textContent='已下发：获取应用使用数据（在线/WS 秒级，离线最多 15 分钟）；稍后点「重新载入」。';}catch(e){alert('下发失败：'+e.message);}}
async function loadData(){if(!TOKEN){document.getElementById('tokCard').style.display='block';return;}if(!SEL){document.getElementById('statusHint').textContent='缺少 device_id（请从控制台设备页进入）。';return;}try{const j=await(await api('/anti-theft/usage-data?device_id='+encodeURIComponent(SEL))).json();DATA=j;document.getElementById('devName').textContent=j.ts?('采集于 '+timeAgo(j.ts)):'未采集';renderUsage();renderFg();renderInstalled();}catch(e){document.getElementById('statusHint').textContent='载入失败：'+e.message;}}
function renderUsage(){if(!DATA){return;}const sel=document.getElementById('rangeSel').value;const[s,e]=rangeBounds(sel);const rows=(DATA.usage||[]).filter(b=>!isSystemPkg(b.pkg));const labels={};(DATA.installed||[]).forEach(a=>labels[a.pkg]=a.label);const map={};rows.forEach(b=>{const lu=b.last_used;if(lu==null||lu<s||lu>=e)return;const p=b.pkg;if(!map[p])map[p]={total:0,last:0,first:lu,launches:0,label:b.label||labels[p]||p};map[p].total+=(b.total_ms||0);if(lu>map[p].last)map[p].last=lu;if(lu<map[p].first)map[p].first=lu;map[p].launches+=(b.launches||0);});const arr=Object.keys(map).map(p=>Object.assign({pkg:p},map[p]));arr.sort((a,b)=>b.last-a.last);
  const box=document.getElementById('usageTable');document.getElementById('usageSummary').textContent='共 '+arr.length+' 个应用';
  if(!arr.length){box.innerHTML='<div class="empty">该范围内无使用记录（或尚未采集数据）</div>';return;}
  let h='<table><tr><th>#</th><th>应用</th><th>最近使用</th><th class="num">区间使用时长</th><th class="num">打开次数</th></tr>';
  arr.forEach((a,i)=>{h+='<tr><td>'+(i+1)+'</td><td>'+escapeHtml(a.label)+'<br><span style="color:var(--muted);font-size:12px">'+escapeHtml(a.pkg)+'</span></td><td>'+timeAgo(a.last)+'</td><td class="num">'+fmtDur(a.total)+'</td><td class="num">'+(a.launches||0)+'</td></tr>';});
  h+='</table>';box.innerHTML=h;}
function renderFg(){const box=document.getElementById('fgBox');let fg=DATA&&DATA.foreground;if(!fg||isSystemPkg(fg.pkg)){box.innerHTML='<div class="empty">暂无前台 App 数据（采集时未授权或无前台应用）</div>';return;}const labels={};(DATA.installed||[]).forEach(a=>labels[a.pkg]=a.label);const name=fg.label||labels[fg.pkg]||fg.pkg;box.innerHTML='<div class="fg"><b>'+escapeHtml(name)+'</b> <span style="color:var(--muted)">'+escapeHtml(fg.pkg)+'</span></div><div class="hint">采集时间：'+fmt(fg.ts)+'（'+timeAgo(fg.ts)+'）</div><div class="hint">注：为采集时刻快照，非实时。</div>';}
function renderInstalled(){if(!DATA)return;const f=(document.getElementById('instFilter').value||'').toLowerCase();const list=(DATA.installed||[]).filter(a=>!f||(a.label||'').toLowerCase().includes(f)||(a.pkg||'').toLowerCase().includes(f));const box=document.getElementById('instBox');if(!list.length){box.innerHTML='<div class="empty">无匹配应用</div>';return;}let h='<div class="hint" style="margin:0 0 8px">共 '+list.length+' 个用户应用（已隐藏系统应用）</div><div class="list">';list.forEach(a=>{h+='<div class="item"><div class="l">'+escapeHtml(a.label||a.pkg)+'</div><div class="p">'+escapeHtml(a.pkg)+'</div></div>';});h+='</div>';box.innerHTML=h;}
function isSystemPkg(pkg){const p=pkg||'';const list=['android','com.android.','com.google.android.','com.qualcomm.','com.qti.','com.miui.','com.mi.','com.xiaomi.','com.bsp.','com.mobiletools.','com.huawei.','com.samsung.','com.lge.','com.oneplus.','com.coloros.','com.oppo.','com.vivo.','com.realme.','com.heytap.','com.meizu.','com.smartisan.','com.lenovo.','com.zui.','com.motorola.','com.sony.','com.asus.','com.mediatek.','com.sec.','com.sonyericsson.','com.amazon.'];return list.some(x=>p===x||p.startsWith(x));}
function escapeHtml(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
loadData();
</script>
</body></html>"""
    return html.replace("__DID__", did)


@router.get("/anti-theft/notify")
async def at_notify_page(device_id: str = Query(None)):
    return Response(content=_notify_html(device_id), media_type="text/html; charset=utf-8")


def _notify_html(device_id):
    """通知监听线索独立页面：展示捕获的系统通知（含聊天预览）。"""
    did = json.dumps(device_id or "")
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>通知线索</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8ef;--text:#1f2733;--muted:#6b7686;--primary:#3b6cff}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px}
header{position:sticky;top:0;z-index:5;padding:12px 16px;background:linear-gradient(90deg,#1f2a44,#2d3d5e);color:#fff;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;flex:1}
header a{color:#fff;text-decoration:none;font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border-radius:12px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:14px}
.ops{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{cursor:pointer;border:none;border-radius:8px;padding:8px 14px;font-size:14px;background:var(--primary);color:#fff}
.hint{color:var(--muted);font-size:12px;margin:6px 0;line-height:1.5}
.item{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;display:flex;gap:10px;align-items:flex-start}
.item .chk{flex-shrink:0;margin-top:2px}
.item .body{flex:1;min-width:0}
.item .app{font-weight:600}
.item .app .p{color:var(--muted);font-weight:400;font-size:12px;word-break:break-all}
.item .title{margin-top:4px;font-weight:600}
.item .text{color:var(--text);margin-top:2px;white-space:pre-wrap;word-break:break-word}
.item .ts{color:var(--muted);font-size:12px;margin-top:4px}
.empty{text-align:center;color:var(--muted);padding:24px}
.danger{background:#ff4d4f}
.secondary{background:#6b7686}
@media (max-width:768px){.wrap{padding:12px}header h1{font-size:15px}}
</style></head>
<body>
<header><a href="/anti-theft/console">← 控制台</a><h1>通知线索</h1><span id="devName" style="font-size:13px;opacity:.85"></span></header>
<div class="wrap">
  <div class="card" id="tokCard" style="display:none">
    <div class="hint">控制端 Token 未保存，请粘贴后保存：</div>
    <div class="ops"><input type="text" id="tokInput" placeholder="控制端 Token" style="flex:1;min-width:160px"><button onclick="saveTok()">保存</button></div>
  </div>
  <div class="card">
    <div class="ops">
      <button onclick="loadData()">重新载入</button>
      <button class="secondary" onclick="toggleSelectAll()">全选</button>
      <button class="danger" onclick="deleteSelected()">删除选中</button>
      <button class="danger" onclick="clearDevice()" style="margin-left:auto">清空本设备</button>
    </div>
    <div id="statusHint" class="hint"></div>
    <div class="hint">本页展示手机通知监听捕获的系统通知（含微信/QQ 等聊天预览），按时间倒序。只能拿到通知栏展示的内容（发件人+预览文本）。</div>
  </div>
  <div class="card"><div id="listBox"><div class="empty">暂无数据</div></div></div>
</div>
<script>
const LS='at_ctrl_token';
let TOKEN=localStorage.getItem(LS)||'';
let SEL=__DID__;
let DATA=null;
const PKG_NAMES={'com.tencent.mm':'微信','com.tencent.mobileqq':'QQ','com.tencent.qqlite':'QQ轻聊','com.android.mms':'短信','com.google.android.apps.messaging':'短信','com.samsung.android.messaging':'短信','com.apple.android.mms':'iMessage','com.alibaba.android.rimet':'钉钉','com.tencent.wework':'企业微信','com.sina.weibo':'微博','com.ss.android.ugc.aweme':'抖音','com.tencent.karaoke':'全民K歌'};
function hdr(){return TOKEN?{'Authorization':'Bearer '+TOKEN}:{}}
async function api(p,o={}){o.headers=Object.assign(hdr(),o.headers||{});const r=await fetch(p,o);if(!r.ok)throw new Error(r.status+' '+(await r.text()));return r}
function fmt(ts){if(!ts)return '-';const d=new Date(ts);const p=n=>(n<10?'0':'')+n;return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function timeAgo(ts){if(!ts)return '-';const s=Math.max(0,Math.floor((Date.now()-ts)/1000));if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';if(s<86400)return Math.floor(s/3600)+'小时前';return Math.floor(s/86400)+'天前';}
function saveTok(){TOKEN=document.getElementById('tokInput').value.trim();if(!TOKEN){alert('请输入 Token');return;}localStorage.setItem(LS,TOKEN);document.getElementById('tokCard').style.display='none';loadData();}
async function loadData(){if(!TOKEN){document.getElementById('tokCard').style.display='block';return;}if(!SEL){document.getElementById('statusHint').textContent='缺少 device_id（请从控制台设备页进入）。';return;}try{const j=await(await api('/anti-theft/notify-data?device_id='+encodeURIComponent(SEL))).json();DATA=j;document.getElementById('devName').textContent=(j.items&&j.items.length)?('最近 '+timeAgo(j.items[0].ts)):'无数据';render();}catch(e){document.getElementById('statusHint').textContent='载入失败：'+e.message;}}
function render(){const box=document.getElementById('listBox');const items=(DATA&&DATA.items)||[];if(!items.length){box.innerHTML='<div class="empty">暂无通知线索（确认手机已开启「通知监听」且产生过通知）</div>';return;}const checked=getSelectedIds();document.getElementById('statusHint').textContent='共 '+items.length+' 条，已选 '+checked.length+' 条';let h='';items.forEach(it=>{const name=PKG_NAMES[it.pkg]||it.pkg;const isChecked=checked.includes(it.id)?'checked':'';h+='<div class="item"><input type="checkbox" class="chk" data-id="'+it.id+'" onchange="onCheckChange()" '+isChecked+'><div class="body"><div class="app">'+esc(name)+'<span class="p"> '+esc(it.pkg)+'</span></div>'+(it.title?'<div class="title">'+esc(it.title)+'</div>':'')+(it.text?'<div class="text">'+esc(it.text)+'</div>':'')+'<div class="ts">'+fmt(it.ts)+'（'+timeAgo(it.ts)+'）</div></div></div>';});box.innerHTML=h;}
function getSelectedIds(){return Array.from(document.querySelectorAll('.item .chk:checked')).map(cb=>parseInt(cb.dataset.id,10));}
function onCheckChange(){const n=getSelectedIds().length;document.getElementById('statusHint').textContent='共 '+(DATA.items.length)+' 条，已选 '+n+' 条';}
function toggleSelectAll(){const cbs=document.querySelectorAll('.item .chk');const allChecked=cbs.length>0&&Array.from(cbs).every(cb=>cb.checked);cbs.forEach(cb=>cb.checked=!allChecked);onCheckChange();}
async function deleteSelected(){const ids=getSelectedIds();if(!ids.length){alert('请先勾选要删除的通知');return;}if(!confirm('确定删除选中的 '+ids.length+' 条通知？'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});const j=await r.json();loadData();setTimeout(()=>alert('已删除 '+j.deleted+' 条'),0);}catch(e){alert('删除失败：'+e.message);}}
async function clearDevice(){if(!SEL){alert('缺少 device_id');return;}if(!confirm('确定清空本设备的所有通知线索？此操作不可恢复。'))return;try{const r=await api('/anti-theft/notify-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:SEL,all:true})});const j=await r.json();loadData();setTimeout(()=>alert('已清空 '+j.deleted+' 条'),0);}catch(e){alert('清空失败：'+e.message);}}
function esc(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
loadData();
</script>
</body></html>"""
    return html.replace("__DID__", did)
