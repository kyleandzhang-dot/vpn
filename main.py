from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timedelta
import hashlib
import hmac
import os
import secrets
import sqlite3
import uvicorn
import random
import string
import json
import uuid
import urllib.parse
import logging
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

from payment_hupijiao import router as payment_router, init_payment_db
from xui_client import (
    create_or_renew_subscription,
    apply_node_residential_routing,
    residential_client_uuid,
)

DB_FILE = "vpn_data.db"
logger = logging.getLogger("vpn_backend")

# 当前客户端统一使用 device_id 识别用户；Bearer 设备令牌仍保留为可选增强能力。
# 如需强制令牌鉴权，可把 ALLOW_LEGACY_DEVICE_AUTH 显式设置为 false。
ALLOW_LEGACY_DEVICE_AUTH = os.getenv("ALLOW_LEGACY_DEVICE_AUTH", "true").lower() == "true"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
ADMIN_AUTH_ENABLED = os.getenv("ADMIN_AUTH_ENABLED", "false").lower() == "true"
ENABLE_UNVERIFIED_NATIVE_RECHARGE = os.getenv(
    "ENABLE_UNVERIFIED_NATIVE_RECHARGE", "false"
).lower() == "true"

# ================= 邀请阶梯规则 =================
# (累计邀请人数, 达到该档位在原有基础上再增加的奖励天数，叠加发放)
# 1人 +2天，3人 再+1天(累计3天)，5人 再+4天(累计7天)，10人 再+23天(累计30天/一个月)
REFERRAL_MILESTONES = [(1, 2), (3, 1), (5, 4), (10, 23)]

# ================= 每日签到规则 =================
# 每日签到赠送的免费时长（分钟）。纯福利性质，用于防止用户流失，不与订阅计费方式挂钩。
# 原"新设备首次连接送30分钟"的福利已取消，改为用户通过签到领取（含首次签到=注册）。
CHECKIN_REWARD_MINUTES = 30

# 远程配置默认值（数据库里没有对应 key 时使用）
DEFAULT_CONFIG = {
    "buy_qq": "1772757914",
    "agent_qq": "1772757914",
    "announcement": "",          # 公告内容，留空则客户端不弹窗
    "announcement_title": "公告",
    # ---- App 版本检测相关（按平台区分，后台可直接改，无需发版）----
    "android_latest_version_code": "1",   # 需与 Android 客户端 BuildConfig.VERSION_CODE 对应的整数一致
    "android_latest_version_name": "1.0.0",
    "android_download_url": "",           # 最新 APK 下载直链
    "android_force_update": "false",      # "true"/"false" 字符串
    "android_changelog": "",              # 更新日志，换行用 \n

    "windows_latest_version_code": "1",   # Windows 端自行定义的整数版本号，只要每次发版递增即可
    "windows_latest_version_name": "1.0.0",
    "windows_download_url": "",           # 最新安装包（.exe/.msi）下载直链
    "windows_force_update": "false",
    "windows_changelog": "",
}

SUPPORTED_APP_PLATFORMS = ["android", "windows"]

# 教程图片支持的平台：安卓 / 苹果 / Windows
SUPPORTED_TUTORIAL_PLATFORMS = ["android", "ios", "windows"]

# ================= 用户 UID 工具函数 =================
# 放在 init_db() 之前定义：init_db() 在模块加载时就会被调用（见文件下方 init_db()），
# 里面要用这个函数给老用户回填 uid，所以必须在那次调用发生前就已定义好。
def generate_uid(cursor):
    """生成一个不重复的6位数字UID（10万-99万），满足"至少5位数字、不能重复"的要求，
    留出比5位更充裕的容量，减少后期用户量大了以后的碰撞概率。"""
    while True:
        uid = str(random.randint(100000, 999999))
        cursor.execute("SELECT 1 FROM users WHERE uid = ?", (uid,))
        if not cursor.fetchone():
            return uid

# ================= 1. 数据库初始化 =================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            device_id TEXT PRIMARY KEY,
            expire_time DATETIME,
            current_node_id TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS activation_codes (code TEXT PRIMARY KEY, days INTEGER, is_used BOOLEAN DEFAULT 0, used_by TEXT, used_time DATETIME)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            remark TEXT,
            config_json TEXT,
            is_active BOOLEAN DEFAULT 1,
            xray_inbound_tag TEXT DEFAULT '',
            xui_base_url TEXT DEFAULT '',
            xui_path TEXT DEFAULT '',
            xui_api_token_ciphertext TEXT DEFAULT '',
            xui_inbound_id INTEGER
        )''')
        # 节点和用户都可以拥有多个标签。标签本身以节点侧为准：只有至少一个节点
        # 正在使用的标签，才允许分配给用户。
        cursor.execute('''CREATE TABLE IF NOT EXISTS node_tags (
            node_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (node_id, tag)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_tags (
            device_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (device_id, tag)
        )''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_tags_tag ON node_tags(tag)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tags_tag ON user_tags(tag)")
        cursor.execute('''CREATE TABLE IF NOT EXISTS manual_user_tags (
            device_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (device_id, tag)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS residential_tag_grants (
            device_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (device_id, node_id, tag)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS residential_egresses (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            protocol TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT NOT NULL,
            password_ciphertext TEXT NOT NULL,
            country TEXT DEFAULT '',
            detected_ip TEXT DEFAULT '',
            status TEXT DEFAULT 'untested',
            last_test_error TEXT DEFAULT '',
            is_active BOOLEAN DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS market_apps (
            id TEXT PRIMARY KEY,
            name TEXT,
            icon_url TEXT,
            price TEXT DEFAULT '免费',
            version TEXT,
            apk_url TEXT,
            desc TEXT,
            package_name TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )''')

        # 邀请关系表：一个被邀请人（invitee）只能绑定一次邀请关系
        cursor.execute('''CREATE TABLE IF NOT EXISTS referrals (
            invitee_device_id TEXT PRIMARY KEY,
            inviter_device_id TEXT,
            created_time DATETIME
        )''')

        # 已发放的阶梯奖励记录，防止同一档位重复发放
        cursor.execute('''CREATE TABLE IF NOT EXISTS referral_rewards (
            inviter_device_id TEXT,
            milestone INTEGER,
            rewarded_time DATETIME,
            PRIMARY KEY (inviter_device_id, milestone)
        )''')

        # 每日签到记录表：一个设备一天只能签到一次，用 (device_id, checkin_date) 联合主键去重
        cursor.execute('''CREATE TABLE IF NOT EXISTS checkins (
            device_id TEXT,
            checkin_date TEXT,
            checkin_time DATETIME,
            PRIMARY KEY (device_id, checkin_date)
        )''')

        # 通用远程配置表：QQ号、公告等 key-value，后台可直接改，无需发版
        cursor.execute('''CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        # 教程图片表：按平台（android/ios/windows）分类，每个平台可挂多张图文教程
        cursor.execute('''CREATE TABLE IF NOT EXISTS tutorial_images (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            image_url TEXT NOT NULL,
            title TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )''')

        # ---- 兼容旧数据库：补列 ----
        cursor.execute("PRAGMA table_info(market_apps)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if "package_name" not in existing_cols:
            cursor.execute("ALTER TABLE market_apps ADD COLUMN package_name TEXT DEFAULT ''")

        cursor.execute("PRAGMA table_info(users)")
        user_cols = [col[1] for col in cursor.fetchall()]
        if "invite_code" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN invite_code TEXT")
        if "invited_by" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN invited_by TEXT")
        if "uid" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN uid TEXT")
        if "api_token_hash" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN api_token_hash TEXT")
        if "api_token_created_at" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN api_token_created_at DATETIME")
        if "residential_egress_id" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN residential_egress_id TEXT")
        if "residential_node_id" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN residential_node_id TEXT")
        if "remark" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN remark TEXT DEFAULT ''")
        cursor.execute("PRAGMA table_info(nodes)")
        node_cols = [col[1] for col in cursor.fetchall()]
        if "xray_inbound_tag" not in node_cols:
            cursor.execute("ALTER TABLE nodes ADD COLUMN xray_inbound_tag TEXT DEFAULT ''")
        if "xui_base_url" not in node_cols:
            cursor.execute("ALTER TABLE nodes ADD COLUMN xui_base_url TEXT DEFAULT ''")
        if "xui_path" not in node_cols:
            cursor.execute("ALTER TABLE nodes ADD COLUMN xui_path TEXT DEFAULT ''")
        if "xui_api_token_ciphertext" not in node_cols:
            cursor.execute("ALTER TABLE nodes ADD COLUMN xui_api_token_ciphertext TEXT DEFAULT ''")
        if "xui_inbound_id" not in node_cols:
            cursor.execute("ALTER TABLE nodes ADD COLUMN xui_inbound_id INTEGER")
        cursor.execute("SELECT value FROM app_config WHERE key='manual_user_tags_migrated_v1'")
        if not cursor.fetchone():
            cursor.execute('''INSERT OR IGNORE INTO manual_user_tags (device_id, tag)
                              SELECT device_id, tag FROM user_tags''')
            cursor.execute('''INSERT INTO app_config (key, value)
                              VALUES ('manual_user_tags_migrated_v1', 'true')''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_token_hash ON users(api_token_hash)")
        # 当前产品规则是一位用户独享一个住宅出口，避免同一固定 IP 被重复分配。
        cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_users_residential_egress
                          ON users(residential_egress_id)
                          WHERE residential_egress_id IS NOT NULL''')

        # 老用户迁移到令牌鉴权时，由管理员签发一次性领取码。数据库只保存领取码哈希，
        # 明文只在创建时返回一次，且默认15分钟过期。
        cursor.execute('''CREATE TABLE IF NOT EXISTS device_claim_codes (
            code_hash TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            expire_time DATETIME NOT NULL,
            used_time DATETIME,
            created_time DATETIME NOT NULL
        )''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_claim_codes_device ON device_claim_codes(device_id)")
        conn.commit()

        # UID 唯一索引：SQLite 的 UNIQUE 索引允许多个 NULL 共存，
        # 所以即使老用户此时 uid 还是 NULL，建索引也不会报错，
        # 之后逐个回填时会真正校验唯一性。
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_uid ON users(uid)")
        conn.commit()

        # 给历史老用户回填 uid（新用户从下面业务接口里生成）
        cursor.execute("SELECT device_id FROM users WHERE uid IS NULL OR uid = ''")
        rows_needing_uid = cursor.fetchall()
        for (dev_id,) in rows_needing_uid:
            cursor.execute("UPDATE users SET uid = ? WHERE device_id = ?", (generate_uid(cursor), dev_id))
        conn.commit()

init_db()
init_payment_db()  # 初始化 payment_orders 表

app = FastAPI(title="秒连 VPN 后台 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def protect_admin_api(request: Request, call_next):
    """内网默认不鉴权；若未来开放公网，可通过环境变量重新开启。"""
    if ADMIN_AUTH_ENABLED and request.url.path.startswith("/api/admin/"):
        if not ADMIN_API_KEY:
            return JSONResponse(
                status_code=503,
                content={"code": 503, "msg": "服务端尚未配置 ADMIN_API_KEY"}
            )
        provided_key = request.headers.get("X-Admin-Key", "")
        if not hmac.compare_digest(provided_key, ADMIN_API_KEY):
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
                content={"code": 401, "msg": "管理员密钥无效"}
            )
    return await call_next(request)

app.include_router(payment_router)  # 挂载虎皮椒支付路由

# ================= 2. 请求体模型 =================
class NodeRequest(BaseModel):
    device_id: str | None = None  # 仅供旧客户端迁移校验；正式鉴权身份来自 Bearer Token
    node_id: str | None = None  # 用户手动指定的节点ID；不传/为空 = 自动负载均衡
class RegisterDeviceRequest(BaseModel): device_id: str
class ClaimDeviceRequest(BaseModel): device_id: str; claim_code: str
class CreateDeviceClaimCodeRequest(BaseModel): uid: str; valid_minutes: int = Field(default=15, ge=1, le=1440)
class RevokeDeviceTokenRequest(BaseModel): uid: str
class RechargeRequest(BaseModel): device_id: str | None = None; code: str
class RechargeByUidRequest(BaseModel): uid: str; code: str
def normalize_tags(tags: list[str]) -> list[str]:
    """清理标签输入，保留显示文字，同时按大小写不敏感方式去重。"""
    cleaned = []
    seen = set()
    for raw_tag in tags:
        tag = str(raw_tag).strip()
        if not tag:
            continue
        if len(tag) > 32:
            raise ValueError("单个标签不能超过32个字符")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(tag)
    if len(cleaned) > 20:
        raise ValueError("每个节点或用户最多设置20个标签")
    return cleaned


class AddNodeRequest(BaseModel):
    node_id: str
    remark: str
    config_json: str
    tags: list[str] = Field(default_factory=list)
    xray_inbound_tag: str | None = None
    xui_base_url: str = ""
    xui_path: str = ""
    xui_api_token: str | None = None
    xui_inbound_id: int | None = Field(default=None, ge=1)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)

    @field_validator("xui_base_url")
    @classmethod
    def validate_xui_base_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if clean and not (clean.startswith("http://") or clean.startswith("https://")):
            raise ValueError("3x-ui 面板地址必须以 http:// 或 https:// 开头")
        return clean

    @field_validator("xui_path")
    @classmethod
    def validate_xui_path(cls, value: str) -> str:
        clean = value.strip().strip("/")
        return f"/{clean}" if clean else ""


class UpdateNodeRequest(AddNodeRequest):
    pass


def split_xui_panel_url(panel_url: str, legacy_path: str = "") -> tuple[str, str]:
    """Store a pasted full panel URL as origin + base path for httpx compatibility."""
    clean_url = (panel_url or "").strip().rstrip("/")
    if not clean_url:
        clean_path = (legacy_path or "").strip().strip("/")
        return "", f"/{clean_path}" if clean_path else ""
    parsed = urllib.parse.urlsplit(clean_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("3x-ui 面板 URL 格式无效")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or legacy_path or ""
    lower_path = path.lower()
    panel_index = lower_path.find("/panel/")
    if panel_index >= 0:
        path = path[:panel_index]
    elif lower_path.endswith("/panel"):
        path = path[:-6]
    clean_path = path.strip().strip("/")
    return origin, f"/{clean_path}" if clean_path else ""


def combine_xui_panel_url(base_url: str, path: str) -> str:
    return f"{(base_url or '').rstrip('/')}{path or ''}" if base_url else ""

class DeleteNodeRequest(BaseModel): node_id: str
class DeleteUserRequest(BaseModel): device_id: str
class UpdateUserRequest(BaseModel): device_id: str; add_days: int
class SetUserTimeRequest(BaseModel): device_id: str; expire_time: str


class SetUserTagsRequest(BaseModel):
    device_id: str
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)


class ResidentialEgressRequest(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=80)
    protocol: str = Field(default="socks", pattern="^(http|socks)$")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=255)
    password: str | None = Field(default=None, max_length=1024)
    country: str = Field(default="", max_length=80)
    is_active: bool = True

    @field_validator("host")
    @classmethod
    def validate_proxy_host(cls, value: str) -> str:
        host = value.strip()
        if "://" in host or "/" in host or any(ch.isspace() for ch in host):
            raise ValueError("代理地址只填写域名或 IP，不能包含协议、路径或空格")
        return host


class ResidentialEgressIdRequest(BaseModel):
    id: str


class BindResidentialEgressRequest(BaseModel):
    device_id: str
    residential_egress_id: str | None = None
    node_id: str | None = None


class SetUserRemarkRequest(BaseModel):
    device_id: str
    remark: str = Field(default="", max_length=200)

class AddMarketAppRequest(BaseModel):
    id: str
    name: str
    icon_url: str = ""
    price: str = "免费"
    version: str = ""
    apk_url: str
    desc: str = ""
    package_name: str = ""
    sort_order: int = 0

class UpdateMarketAppRequest(BaseModel):
    id: str
    name: str
    icon_url: str = ""
    price: str = "免费"
    version: str = ""
    apk_url: str
    desc: str = ""
    package_name: str = ""
    sort_order: int = 0
    is_active: bool = True

class DeleteMarketAppRequest(BaseModel): id: str

class AddTutorialImageRequest(BaseModel):
    id: str
    platform: str = Field(..., description="教程所属平台：android / ios / windows")
    image_url: str
    title: str = ""
    sort_order: int = 0

    @field_validator("platform")
    @classmethod
    def validate_tutorial_platform(cls, value: str) -> str:
        clean_value = value.lower().strip()
        if clean_value not in SUPPORTED_TUTORIAL_PLATFORMS:
            raise ValueError(f"非法参数：教程平台必须是 {SUPPORTED_TUTORIAL_PLATFORMS} 之一")
        return clean_value

class UpdateTutorialImageRequest(BaseModel):
    id: str
    platform: str
    image_url: str
    title: str = ""
    sort_order: int = 0
    is_active: bool = True

    @field_validator("platform")
    @classmethod
    def validate_tutorial_platform(cls, value: str) -> str:
        clean_value = value.lower().strip()
        if clean_value not in SUPPORTED_TUTORIAL_PLATFORMS:
            raise ValueError(f"非法参数：教程平台必须是 {SUPPORTED_TUTORIAL_PLATFORMS} 之一")
        return clean_value

class DeleteTutorialImageRequest(BaseModel): id: str

class BindInviteRequest(BaseModel):
    device_id: str | None = None
    invite_code: str

class CheckinRequest(BaseModel):
    device_id: str | None = None

class SetConfigRequest(BaseModel):
    key: str
    value: str

class NativeAppRechargeRequest(BaseModel):
    days: int = Field(..., gt=0, le=3650, description="充值天数，必须大于0")
    platform: str = Field(..., description="充值平台，严格限制为 ios 或 android")

    @field_validator("platform")
    @classmethod
    def validate_native_platform(cls, value: str) -> str:
        clean_value = value.lower().strip()
        if clean_value not in ["ios", "android"]:
            raise ValueError("非法参数：充值操作系统平台必须是 ios 或 android")
        return clean_value


# ================= 标签权限工具函数 =================
def replace_node_tags(cursor, node_id: str, tags: list[str]):
    cursor.execute("DELETE FROM node_tags WHERE node_id = ?", (node_id,))
    cursor.executemany(
        "INSERT INTO node_tags (node_id, tag) VALUES (?, ?)",
        [(node_id, tag) for tag in tags]
    )


def rebuild_effective_user_tags(cursor, device_id: str):
    cursor.execute("DELETE FROM user_tags WHERE device_id = ?", (device_id,))
    cursor.execute('''INSERT OR IGNORE INTO user_tags (device_id, tag)
                      SELECT device_id, tag FROM manual_user_tags WHERE device_id = ?''',
                   (device_id,))
    cursor.execute('''INSERT OR IGNORE INTO user_tags (device_id, tag)
                      SELECT device_id, tag FROM residential_tag_grants WHERE device_id = ?''',
                   (device_id,))


def replace_user_tags(cursor, device_id: str, tags: list[str]):
    """Replace administrator-selected tags while preserving residential auto grants."""
    cursor.execute("DELETE FROM manual_user_tags WHERE device_id = ?", (device_id,))
    cursor.executemany(
        "INSERT INTO manual_user_tags (device_id, tag) VALUES (?, ?)",
        [(device_id, tag) for tag in tags]
    )
    rebuild_effective_user_tags(cursor, device_id)


def refresh_residential_tag_grants(cursor, device_id: str, node_id: str | None):
    cursor.execute("DELETE FROM residential_tag_grants WHERE device_id = ?", (device_id,))
    if node_id:
        cursor.execute('''INSERT OR IGNORE INTO residential_tag_grants (device_id, node_id, tag)
                          SELECT ?, node_id, tag FROM node_tags WHERE node_id = ?''',
                       (device_id, node_id))
    rebuild_effective_user_tags(cursor, device_id)


def refresh_node_residential_tag_grants(cursor, node_id: str):
    cursor.execute("SELECT device_id FROM users WHERE residential_node_id = ?", (node_id,))
    for (device_id,) in cursor.fetchall():
        refresh_residential_tag_grants(cursor, device_id, node_id)


def cleanup_orphan_user_tags(cursor):
    """节点侧已完全不存在的标签，自动从所有用户身上移除。"""
    cursor.execute('''DELETE FROM manual_user_tags
                      WHERE NOT EXISTS (
                          SELECT 1 FROM node_tags
                          WHERE node_tags.tag = manual_user_tags.tag
                      )''')
    cursor.execute('''DELETE FROM residential_tag_grants
                      WHERE NOT EXISTS (
                          SELECT 1 FROM node_tags
                          WHERE node_tags.tag = residential_tag_grants.tag
                      )''')
    cursor.execute('''DELETE FROM user_tags
                      WHERE NOT EXISTS (
                          SELECT 1 FROM node_tags
                          WHERE node_tags.tag = user_tags.tag
                      )''')


def clear_inaccessible_current_nodes(cursor):
    """标签修改后，清掉用户已无权继续使用的当前节点关联。"""
    cursor.execute('''UPDATE users SET current_node_id = NULL
                      WHERE current_node_id IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM node_tags nt
                            WHERE nt.node_id = users.current_node_id
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM node_tags nt
                            JOIN user_tags ut ON ut.tag = nt.tag
                            WHERE nt.node_id = users.current_node_id
                              AND ut.device_id = users.device_id
                        )''')


def get_tag_map(cursor, table: str, id_column: str) -> dict[str, list[str]]:
    """一次性读取标签映射；table/id_column 仅由服务端常量调用。"""
    cursor.execute(f"SELECT {id_column}, tag FROM {table} ORDER BY tag COLLATE NOCASE")
    result = {}
    for owner_id, tag in cursor.fetchall():
        result.setdefault(owner_id, []).append(tag)
    return result


# ================= 住宅出口工具函数 =================
def _residential_cipher() -> Fernet:
    key = os.getenv("RESIDENTIAL_PROXY_ENCRYPTION_KEY", "").strip()
    if not key:
        key_file = Path(os.getenv(
            "RESIDENTIAL_PROXY_KEY_FILE", "residential_proxy.key"
        ).strip())
        try:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            if key_file.exists():
                key = key_file.read_text(encoding="ascii").strip()
            else:
                generated_key = Fernet.generate_key().decode("ascii")
                try:
                    with key_file.open("x", encoding="ascii") as handle:
                        handle.write(generated_key)
                    try:
                        os.chmod(key_file, 0o600)
                    except OSError:
                        pass
                    key = generated_key
                except FileExistsError:
                    key = key_file.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RuntimeError(f"无法创建住宅密码本地密钥文件: {exc}") from exc
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("RESIDENTIAL_PROXY_ENCRYPTION_KEY 格式无效，必须是 Fernet 密钥") from exc


def encrypt_proxy_password(password: str) -> str:
    return _residential_cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_proxy_password(ciphertext: str) -> str:
    try:
        return _residential_cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("住宅出口密码无法解密，请检查加密密钥是否与保存时一致") from exc


def encrypt_node_api_token(token: str) -> str:
    return _residential_cipher().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_node_api_token(ciphertext: str) -> str:
    try:
        return _residential_cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("节点 3x-ui API Token 无法解密，请检查加密密钥") from exc


def residential_assignments(cursor, node_id: str) -> list[dict]:
    cursor.execute('''SELECT e.id, e.protocol, e.host, e.port, e.username,
                             e.password_ciphertext, u.device_id, u.expire_time,
                             n.node_id
                      FROM residential_egresses e
                      JOIN users u ON u.residential_egress_id = e.id
                      JOIN nodes n ON n.node_id = u.residential_node_id
                      WHERE e.is_active = 1 AND n.is_active = 1 AND n.node_id = ?
                      ORDER BY e.id''', (node_id,))
    return [
        {
            "id": row[0],
            "protocol": row[1],
            "host": row[2],
            "port": row[3],
            "username": row[4],
            "password": decrypt_proxy_password(row[5]),
            "device_id": row[6],
            "expire_time": row[7],
            "node_id": row[8],
        }
        for row in cursor.fetchall()
    ]


def get_node_panel_config(cursor, node_id: str) -> dict | None:
    cursor.execute('''SELECT node_id, xui_base_url, xui_path,
                             xui_api_token_ciphertext, xui_inbound_id
                      FROM nodes WHERE node_id = ?''', (node_id,))
    row = cursor.fetchone()
    if not row:
        return None
    token = decrypt_node_api_token(row[3]) if row[3] else ""
    return {
        "node_id": row[0], "base_url": row[1] or "", "path": row[2] or "",
        "api_token": token, "inbound_id": row[4],
    }


async def sync_residential_nodes(cursor, node_ids: set[str] | list[str]) -> tuple[bool, str]:
    try:
        messages = []
        for node_id in sorted({item for item in node_ids if item}):
            panel = get_node_panel_config(cursor, node_id)
            if not panel:
                return False, f"住宅出口路由同步失败: 节点 {node_id} 不存在"
            assignments = residential_assignments(cursor, node_id)
            ok, message = await apply_node_residential_routing(panel, assignments)
            if not ok:
                return False, message
            messages.append(message)
        return True, "；".join(messages) if messages else "无需同步住宅出口"
    except Exception as exc:
        return False, f"住宅出口路由同步失败: {exc}"


async def restore_residential_nodes_after_rollback(conn, node_ids) -> None:
    try:
        await sync_residential_nodes(conn.cursor(), node_ids)
    except Exception as exc:
        logger.warning("数据库回滚后恢复住宅出口远程状态失败: %s", exc)


@app.on_event("startup")
async def restore_residential_routes_on_startup():
    """重启后重新生成托管规则，并清除旧版没有指定节点的全局住宅规则。"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT residential_node_id FROM users WHERE residential_node_id IS NOT NULL")
            ok, msg = await sync_residential_nodes(cursor, {row[0] for row in cursor.fetchall()})
        if ok:
            logger.info(msg)
        else:
            logger.warning(msg)
    except Exception as exc:
        # 住宅同步失败不能阻止主 API 启动；管理员绑定时仍会再次同步并得到明确错误。
        logger.warning("启动时同步住宅出口失败: %s", exc)


async def probe_residential_proxy(proxy: dict) -> tuple[bool, str, str]:
    """Test the proxy from the VPN backend without exposing credentials to the browser."""
    scheme = "socks5" if proxy["protocol"] == "socks" else "http"
    user = urllib.parse.quote(proxy["username"], safe="")
    password = urllib.parse.quote(proxy["password"], safe="")
    host = proxy["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    proxy_url = f"{scheme}://{user}:{password}@{host}:{proxy['port']}"
    test_url = os.getenv(
        "RESIDENTIAL_PROXY_TEST_URL", "https://ip.oxylabs.io/location"
    ).strip()
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(test_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        detected_ip = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detected_ip = str(
                    payload.get("ip") or payload.get("query") or payload.get("address") or ""
                )
        except ValueError:
            text_value = response.text.strip()
            if len(text_value) <= 64:
                detected_ip = text_value
        return True, detected_ip, ""
    except Exception as exc:
        return False, "", str(exc)[:500]


# ================= 设备令牌鉴权工具函数 =================
def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_device_token(cursor, device_id: str) -> str:
    """签发256位以上随机设备令牌，数据库中只保存SHA-256哈希。"""
    while True:
        token = f"vpn_{secrets.token_urlsafe(32)}"
        token_hash = hash_secret(token)
        cursor.execute("SELECT 1 FROM users WHERE api_token_hash = ?", (token_hash,))
        if not cursor.fetchone():
            break
    cursor.execute(
        "UPDATE users SET api_token_hash = ?, api_token_created_at = ? WHERE device_id = ?",
        (token_hash, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), device_id)
    )
    return token


def parse_bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.strip().partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def authenticate_device_token(cursor, authorization: str | None) -> dict | None:
    token = parse_bearer_token(authorization)
    if not token:
        return None
    cursor.execute(
        """SELECT device_id, expire_time, invite_code, uid
           FROM users WHERE api_token_hash = ?""",
        (hash_secret(token),)
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "device_id": row[0],
        "expire_time": row[1],
        "invite_code": row[2],
        "uid": row[3],
    }


def resolve_authenticated_device(
    cursor,
    authorization: str | None,
    legacy_device_id: str | None = None
) -> dict:
    """优先使用 Bearer 令牌；允许时回退到客户端提供的 device_id。"""
    identity = authenticate_device_token(cursor, authorization)
    clean_legacy_id = (legacy_device_id or "").strip()
    if identity:
        if clean_legacy_id and clean_legacy_id != identity["device_id"]:
            raise HTTPException(status_code=403, detail="请求中的设备ID与访问令牌不匹配")
        return identity

    if ALLOW_LEGACY_DEVICE_AUTH and clean_legacy_id:
        cursor.execute(
            "SELECT device_id, expire_time, invite_code, uid FROM users WHERE device_id = ?",
            (clean_legacy_id,)
        )
        row = cursor.fetchone()
        if not row:
            expire_time, invite_code, uid = get_or_register_user(
                cursor, clean_legacy_id, datetime.now()
            )
            row = (
                clean_legacy_id,
                expire_time.strftime("%Y-%m-%d %H:%M:%S"),
                invite_code,
                uid,
            )
        if row:
            return {
                "device_id": row[0], "expire_time": row[1],
                "invite_code": row[2], "uid": row[3],
            }

    raise HTTPException(
        status_code=401,
        detail="缺少或无效的设备访问令牌",
        headers={"WWW-Authenticate": "Bearer"}
    )


def normalize_claim_code(value: str) -> str:
    return value.replace("-", "").replace(" ", "").upper()


def generate_claim_code() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"

# ================= 3. 邀请系统工具函数 =================
def generate_invite_code(cursor):
    """生成一个不重复的 6 位邀请码"""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        cursor.execute("SELECT 1 FROM users WHERE invite_code = ?", (code,))
        if not cursor.fetchone():
            return code

def check_and_grant_referral_rewards(cursor, inviter_device_id, now):
    """
    检查邀请人当前累计邀请数是否达到新的阶梯，达到则发放对应天数奖励。
    每个阶梯只发放一次（用 referral_rewards 表去重）。
    这里采用"达到该档位则账号有效期额外增加该档位对应天数"的叠加发放方式：
    1人档 +2天，3人档再 +1天，5人档再 +4天，10人档再 +23天，最终满10人共 +30天。
    """
    cursor.execute("SELECT COUNT(*) FROM referrals WHERE inviter_device_id = ?", (inviter_device_id,))
    count = cursor.fetchone()[0]

    for milestone, days in REFERRAL_MILESTONES:
        if count < milestone:
            continue
        cursor.execute(
            "SELECT 1 FROM referral_rewards WHERE inviter_device_id=? AND milestone=?",
            (inviter_device_id, milestone)
        )
        if cursor.fetchone():
            continue  # 这个阶梯已经发放过了，跳过

        cursor.execute("SELECT expire_time FROM users WHERE device_id=?", (inviter_device_id,))
        r = cursor.fetchone()
        if r:
            current_expire = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
            base = current_expire if current_expire > now else now
        else:
            base = now

        new_expire = base + timedelta(days=days)
        cursor.execute(
            "UPDATE users SET expire_time=? WHERE device_id=?",
            (new_expire.strftime("%Y-%m-%d %H:%M:%S"), inviter_device_id)
        )
        cursor.execute(
            "INSERT INTO referral_rewards (inviter_device_id, milestone, rewarded_time) VALUES (?,?,?)",
            (inviter_device_id, milestone, now.strftime("%Y-%m-%d %H:%M:%S"))
        )

def get_or_register_user(cursor, device_id, now):
    """查用户记录；不存在就自动建号（生成邀请码+UID，expire_time设为已过期），
    存在但缺邀请码/UID的老用户顺便补发。get_node 和 invite_info 共用这一份逻辑，
    这样不管用户是先点了"连接"还是只是打开了App（invite_info 在首页初始化时就会调），
    都会在第一次请求时就完成自动注册、拿到自己的UID，不用非要点一次连接才建号。
    返回 (expire_time: datetime, invite_code: str, uid: str)"""
    cursor.execute("SELECT expire_time, invite_code, uid FROM users WHERE device_id = ?", (device_id,))
    result = cursor.fetchone()

    if result is None:
        expire_time = now - timedelta(seconds=1)
        invite_code = generate_invite_code(cursor)
        uid = generate_uid(cursor)
        cursor.execute(
            "INSERT OR IGNORE INTO users (device_id, expire_time, invite_code, uid) VALUES (?, ?, ?, ?)",
            (device_id, expire_time.strftime("%Y-%m-%d %H:%M:%S"), invite_code, uid)
        )
        # 极端并发下可能撞车，撞车了就用已存在的那条记录
        cursor.execute("SELECT expire_time, invite_code, uid FROM users WHERE device_id = ?", (device_id,))
        expire_time_str, invite_code, uid = cursor.fetchone()
        expire_time = datetime.strptime(expire_time_str, "%Y-%m-%d %H:%M:%S")
    else:
        expire_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        invite_code = result[1]
        uid = result[2]
        if not invite_code:
            invite_code = generate_invite_code(cursor)
            cursor.execute("UPDATE users SET invite_code=? WHERE device_id=?", (invite_code, device_id))
        if not uid:
            uid = generate_uid(cursor)
            cursor.execute("UPDATE users SET uid=? WHERE device_id=?", (uid, device_id))

    return expire_time, invite_code, uid

# ================= 设备认证接口 =================
@app.post("/api/v1/auth/register")
async def register_device(req: RegisterDeviceRequest):
    """仅供全新设备注册。已有用户必须使用管理员签发的一次性领取码。"""
    device_id = req.device_id.strip()
    if not device_id or len(device_id) > 200:
        return {"code": 400, "msg": "设备ID格式不正确", "data": None}

    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE device_id = ?", (device_id,))
        if cursor.fetchone():
            return {"code": 409, "msg": "设备已存在，请使用一次性领取码绑定", "data": None}

        expire_time, _invite_code, uid = get_or_register_user(cursor, device_id, now)
        token = issue_device_token(cursor, device_id)
        conn.commit()
    return {"code": 200, "msg": "设备注册成功", "data": {
        "access_token": token,
        "token_type": "Bearer",
        "device_id": device_id,
        "uid": uid,
        "expire_time": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
    }}


@app.post("/api/v1/auth/claim")
async def claim_existing_device(req: ClaimDeviceRequest):
    """老用户用管理员签发的一次性领取码获取设备令牌。"""
    device_id = req.device_id.strip()
    code_hash = hash_secret(normalize_claim_code(req.claim_code))
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT device_id, expire_time, used_time
               FROM device_claim_codes WHERE code_hash = ?""",
            (code_hash,)
        )
        claim = cursor.fetchone()
        if not claim or claim[0] != device_id:
            return {"code": 400, "msg": "领取码无效", "data": None}
        if claim[2]:
            return {"code": 400, "msg": "领取码已使用", "data": None}
        if now > datetime.strptime(claim[1], "%Y-%m-%d %H:%M:%S"):
            return {"code": 400, "msg": "领取码已过期", "data": None}

        cursor.execute("SELECT uid FROM users WHERE device_id = ?", (device_id,))
        user = cursor.fetchone()
        if not user:
            return {"code": 404, "msg": "用户不存在", "data": None}

        token = issue_device_token(cursor, device_id)
        cursor.execute(
            "UPDATE device_claim_codes SET used_time = ? WHERE code_hash = ?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), code_hash)
        )
        conn.commit()
    return {"code": 200, "msg": "设备令牌领取成功", "data": {
        "access_token": token,
        "token_type": "Bearer",
        "device_id": device_id,
        "uid": user[0],
    }}


@app.post("/api/v1/auth/rotate")
async def rotate_device_token(authorization: str | None = Header(default=None)):
    """使用当前有效令牌换取新令牌；旧令牌立即失效。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization)
        token = issue_device_token(cursor, identity["device_id"])
        conn.commit()
    return {"code": 200, "msg": "设备令牌已更新", "data": {
        "access_token": token,
        "token_type": "Bearer",
    }}


@app.get("/api/v1/auth/me")
async def get_authenticated_device(authorization: str | None = Header(default=None)):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization)
    return {"code": 200, "msg": "成功", "data": {
        "device_id": identity["device_id"],
        "uid": identity["uid"],
        "expire_time": identity["expire_time"],
    }}

# ================= 4. 客户端核心接口 =================
@app.post("/api/v1/get_node")
async def get_node(req: NodeRequest, authorization: str | None = Header(default=None)):
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, req.device_id)
        device_id = identity["device_id"]
        expire_time = datetime.strptime(identity["expire_time"], "%Y-%m-%d %H:%M:%S")
        invite_code = identity["invite_code"]
        uid = identity["uid"]

        if now > expire_time:
            conn.commit()
            return {"code": 403, "msg": "时长已过期，请充值", "data": None}

        if req.node_id:
            # 用户手动指定节点时也必须校验标签权限，不能绕过节点列表直接连接。
            cursor.execute(
                '''SELECT n.node_id, n.config_json
                   FROM nodes n
                   WHERE n.node_id = ? AND n.is_active = 1
                     AND (
                         NOT EXISTS (SELECT 1 FROM node_tags nt0 WHERE nt0.node_id = n.node_id)
                         OR EXISTS (
                             SELECT 1
                             FROM node_tags nt
                             JOIN user_tags ut ON ut.tag = nt.tag
                             WHERE nt.node_id = n.node_id AND ut.device_id = ?
                         )
                     )''',
                (req.node_id, device_id)
            )
            best_node = cursor.fetchone()
            if not best_node:
                conn.commit()
                return {"code": 404, "msg": "指定的节点不存在、已下线或无权使用", "data": None}
        else:
            # 公共节点（无标签）对所有用户可见；私有节点需与用户至少命中一个标签。
            cursor.execute('''SELECT n.node_id, n.config_json
                              FROM nodes n
                              LEFT JOIN users u ON n.node_id = u.current_node_id
                              WHERE n.is_active = 1
                                AND (
                                    NOT EXISTS (SELECT 1 FROM node_tags nt0 WHERE nt0.node_id = n.node_id)
                                    OR EXISTS (
                                        SELECT 1
                                        FROM node_tags nt
                                        JOIN user_tags ut ON ut.tag = nt.tag
                                        WHERE nt.node_id = n.node_id AND ut.device_id = ?
                                    )
                                )
                              GROUP BY n.node_id
                              ORDER BY COUNT(u.device_id) ASC, n.node_id ASC
                              LIMIT 1''', (device_id,))
            best_node = cursor.fetchone()

            if not best_node:
                conn.commit()
                return {"code": 500, "msg": "当前无可用节点", "data": None}

        cursor.execute("UPDATE users SET current_node_id = ? WHERE device_id = ?", (best_node[0], device_id))
        cursor.execute('''SELECT residential_egress_id, residential_node_id
                          FROM users WHERE device_id = ?''', (device_id,))
        residential_binding = cursor.fetchone()
        node_config = json.loads(best_node[1])
        if (residential_binding and residential_binding[0]
                and residential_binding[1] == best_node[0]):
            dedicated_uuid = residential_client_uuid(device_id, best_node[0])
            node_config["uuid"] = dedicated_uuid
            if "id" in node_config:
                node_config["id"] = dedicated_uuid
        conn.commit()

    return {"code": 200, "msg": "成功", "data": {
        "expire_time": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
        "node": node_config,
        "invite_code": invite_code,
        "uid": uid
    }}

@app.get("/api/v1/nodes")
async def list_available_nodes(
    device_id: str | None = None,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    authorization: str | None = Header(default=None)
):
    """客户端获取当前用户有权使用的节点列表。
    只返回公共节点和与用户标签匹配的启用节点，不返回config_json。
    get_node还会再次执行同样的权限校验，防止绕过列表直接请求配置。
    """
    legacy_device_id = (device_id or x_device_id or "").strip()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, legacy_device_id)
        resolved_device_id = identity["device_id"]
        cursor.execute('''SELECT n.node_id, n.remark, COUNT(u.device_id)
                       FROM nodes n
                       LEFT JOIN users u ON n.node_id = u.current_node_id
                       WHERE n.is_active = 1
                         AND (
                             NOT EXISTS (
                                 SELECT 1 FROM node_tags nt0 WHERE nt0.node_id = n.node_id
                             )
                             OR EXISTS (
                                 SELECT 1
                                 FROM node_tags nt
                                 JOIN user_tags ut ON ut.tag = nt.tag
                                 WHERE nt.node_id = n.node_id AND ut.device_id = ?
                             )
                         )
                       GROUP BY n.node_id ORDER BY n.remark ASC''',
                       (resolved_device_id,))
        rows = cursor.fetchall()
    return {"code": 200, "msg": "成功", "data": [
        {"node_id": r[0], "remark": r[1], "load": r[2]}
        for r in rows
    ]}

@app.post("/api/v1/recharge")
async def recharge(req: RechargeRequest, authorization: str | None = Header(default=None)):
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, req.device_id)
        device_id = identity["device_id"]
        cursor.execute("SELECT days, is_used FROM activation_codes WHERE code = ?", (req.code,))
        code_record = cursor.fetchone()
        if not code_record or code_record[1]: return {"code": 400, "msg": "激活码无效或已被使用"}

        current_expire = datetime.strptime(identity["expire_time"], "%Y-%m-%d %H:%M:%S")

        base_time = current_expire if current_expire > now else now
        new_expire = base_time + timedelta(days=code_record[0])

        cursor.execute("UPDATE users SET expire_time = ? WHERE device_id = ?", (new_expire.strftime("%Y-%m-%d %H:%M:%S"), device_id))
        cursor.execute("UPDATE activation_codes SET is_used = 1, used_by = ?, used_time = ? WHERE code = ?", (device_id, now.strftime("%Y-%m-%d %H:%M:%S"), req.code))
        conn.commit()
    return {"code": 200, "msg": "充值成功", "data": {"new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S")}}

@app.post("/api/v1/recharge_by_uid")
async def recharge_by_uid(req: RechargeByUidRequest, authorization: str | None = Header(default=None)):
    """跟 /api/v1/recharge 逻辑一致，只是用 uid 而不是 device_id 来定位账号。
    用户的 uid 是注册时自动分配的6位数字，方便客服/人工充值场景下用户口头报一个
    短数字即可定位账号，不用抄一长串 device_id。
    注意：uid 只在用户已经打开过App（自动注册）之后才存在，如果传入的uid查不到人，
    直接返回404，不会像 /api/v1/recharge 那样顺便帮它建号（因为不知道对应哪个device_id）。"""
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization)
        if req.uid.strip() != identity["uid"]:
            raise HTTPException(status_code=403, detail="只能为当前令牌所属UID充值")
        cursor.execute("SELECT days, is_used FROM activation_codes WHERE code = ?", (req.code,))
        code_record = cursor.fetchone()
        if not code_record or code_record[1]:
            return {"code": 400, "msg": "激活码无效或已被使用"}

        cursor.execute("SELECT device_id, expire_time FROM users WHERE uid = ?", (req.uid,))
        user_record = cursor.fetchone()
        if not user_record:
            return {"code": 404, "msg": "UID不存在"}
        device_id, expire_time_str = user_record

        current_expire = datetime.strptime(expire_time_str, "%Y-%m-%d %H:%M:%S")
        base_time = current_expire if current_expire > now else now
        new_expire = base_time + timedelta(days=code_record[0])

        cursor.execute("UPDATE users SET expire_time = ? WHERE device_id = ?", (new_expire.strftime("%Y-%m-%d %H:%M:%S"), device_id))
        cursor.execute("UPDATE activation_codes SET is_used = 1, used_by = ?, used_time = ? WHERE code = ?", (device_id, now.strftime("%Y-%m-%d %H:%M:%S"), req.code))
        conn.commit()
    return {"code": 200, "msg": "充值成功", "data": {"new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S"), "uid": req.uid}}

@app.get("/api/v1/market/apps")
async def get_market_apps():
    """客户端应用市场列表：只返回上架中的应用，按 sort_order 排序"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT id, name, icon_url, price, version, apk_url, desc, package_name
                       FROM market_apps WHERE is_active = 1 ORDER BY sort_order ASC, id ASC''')
        rows = cursor.fetchall()
    return {"code": 200, "msg": "成功", "data": [
        {"id": r[0], "name": r[1], "icon_url": r[2], "price": r[3], "version": r[4], "apk_url": r[5], "desc": r[6], "package_name": r[7]}
        for r in rows
    ]}

@app.get("/api/v1/tutorial_images")
async def get_tutorial_images(platform: str = "android"):
    """客户端教程弹窗调用：按平台（android/ios/windows）返回该平台已上架的教程图，按 sort_order 排序。
    支持一个平台挂多张图（图文教程分步展示）。"""
    clean_platform = platform.lower().strip()
    if clean_platform not in SUPPORTED_TUTORIAL_PLATFORMS:
        return {"code": 400, "msg": f"不支持的平台: {platform}，仅支持 {SUPPORTED_TUTORIAL_PLATFORMS}", "data": []}

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT id, platform, image_url, title, sort_order FROM tutorial_images
               WHERE platform = ? AND is_active = 1 ORDER BY sort_order ASC, id ASC''',
            (clean_platform,)
        )
        rows = cursor.fetchall()
    return {"code": 200, "msg": "成功", "data": [
        {"id": r[0], "platform": r[1], "image_url": r[2], "title": r[3], "sort_order": r[4]}
        for r in rows
    ]}

@app.post("/api/v1/check_status")
async def check_status(req: NodeRequest, authorization: str | None = Header(default=None)):
    """心跳接口：前端每分钟来问一次，如果过期返回 403"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, req.device_id)
        cursor.execute("SELECT expire_time FROM users WHERE device_id = ?", (identity["device_id"],))
        result = cursor.fetchone()

        if not result:
            return {"code": 404, "msg": "用户不存在"}

        expire_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > expire_time:
            return {"code": 403, "msg": "已过期"}

    return {"code": 200, "msg": "正常"}

@app.post("/api/v1/client/subscription/recharge", summary="iOS与Android内购充值同步接口")
async def handle_native_app_recharge(
    payload: NativeAppRechargeRequest,
    x_device_id: str | None = Header(default=None, description="仅供旧客户端迁移校验"),
    authorization: str | None = Header(default=None)
):
    """
    iOS与Android客户端在调用原生应用市场内付成功后直接触发。
    系统从 Bearer Token 提取 device_id，自动进行时长续费计算并调用底层接口同步节点。
    """
    if not ENABLE_UNVERIFIED_NATIVE_RECHARGE:
        raise HTTPException(
            status_code=503,
            detail="原生内购充值尚未接入商店收据校验，已按安全默认关闭"
        )
    now = datetime.now()
    add_timedelta = timedelta(days=payload.days)

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, x_device_id)
        device_id = identity["device_id"]
        current_expire = datetime.strptime(identity["expire_time"], "%Y-%m-%d %H:%M:%S")

        base_time = current_expire if current_expire > now else now
        new_expire = base_time + add_timedelta
        new_expire_str = new_expire.strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("UPDATE users SET expire_time=? WHERE device_id=?", (new_expire_str, device_id))
        conn.commit()

    target_expiry_ms = int(new_expire.timestamp() * 1000)

    try:
        xui_result = await create_or_renew_subscription(device_id=device_id, expiry_ms=target_expiry_ms)
        if not xui_result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"底层节点更新失败: {xui_result.get('msg', '未知错误')}"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"调用底层订阅服务异常: {str(e)}"
        )

    order_id = f"native_{uuid.uuid4().hex[:16]}"
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO payment_orders
               (order_id, device_id, product_id, amount, days, status, payment_method,
                source, platform, activation_code, created_time, paid_time)
               VALUES (?, ?, 0, 0.0, ?, 'SUCCESS', 'native_iap', 'app', ?, ?, ?, ?)''',
            (order_id, device_id, payload.days, payload.platform, xui_result.get("sub_link"), now_str, now_str)
        )
        conn.commit()

    return {
        "code": 200,
        "msg": "充值到账成功，账号时间与节点已同步",
        "data": {
            "platform": payload.platform,
            "added_days": payload.days,
            "new_expire_time": new_expire_str,
            "sub_link": xui_result.get("sub_link")
        }
    }

# ================= 5. 邀请好友系统接口 =================
@app.post("/api/v1/bind_invite")
async def bind_invite(req: BindInviteRequest, authorization: str | None = Header(default=None)):
    """新用户填写好友的邀请码，建立邀请关系并检查是否触发邀请人的阶梯奖励"""
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, req.device_id)
        device_id = identity["device_id"]

        cursor.execute("SELECT invited_by FROM users WHERE device_id=?", (device_id,))
        row = cursor.fetchone()
        if row is None:
            return {"code": 404, "msg": "请先启动一次 App 建立设备记录后再绑定邀请码"}
        if row[0]:
            return {"code": 400, "msg": "您已绑定过邀请关系，不能重复绑定"}

        invite_code = req.invite_code.strip().upper()
        cursor.execute("SELECT device_id FROM users WHERE invite_code=?", (invite_code,))
        inviter = cursor.fetchone()
        if not inviter:
            return {"code": 400, "msg": "邀请码不存在"}
        inviter_id = inviter[0]
        if inviter_id == device_id:
            return {"code": 400, "msg": "不能邀请自己"}

        cursor.execute(
            "INSERT INTO referrals (invitee_device_id, inviter_device_id, created_time) VALUES (?,?,?)",
            (device_id, inviter_id, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        cursor.execute("UPDATE users SET invited_by=? WHERE device_id=?", (inviter_id, device_id))

        check_and_grant_referral_rewards(cursor, inviter_id, now)
        conn.commit()

    return {"code": 200, "msg": "绑定成功，好友助力已生效"}

@app.get("/api/v1/invite_info")
async def invite_info(
    device_id: str | None = None,
    authorization: str | None = Header(default=None)
):
    """查询自己的邀请码、UID、已邀请人数、已发放的档位、下一档还差多少人。
    这里也会跟 get_node 一样自动注册用户——因为首页 _initData() 一进来就会调
    这个接口，让用户"只要打开App"就能拿到自己的UID，不用非要点一次连接。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, device_id)
        resolved_device_id = identity["device_id"]
        invite_code = identity["invite_code"]
        uid = identity["uid"]

        cursor.execute("SELECT COUNT(*) FROM referrals WHERE inviter_device_id=?", (resolved_device_id,))
        count = cursor.fetchone()[0]

        cursor.execute("SELECT milestone FROM referral_rewards WHERE inviter_device_id=?", (resolved_device_id,))
        rewarded = [x[0] for x in cursor.fetchall()]
        conn.commit()

    next_milestone = next((m for m, d in REFERRAL_MILESTONES if m > count), None)
    return {"code": 200, "data": {
        "invite_code": invite_code,
        "uid": uid,
        "invited_count": count,
        "rewarded_milestones": rewarded,
        "milestones": REFERRAL_MILESTONES,
        "next_milestone": next_milestone
    }}

# ================= 5.1 每日签到接口 =================
def _calc_checkin_streak(cursor, device_id, today):
    """从今天（或最近一次签到日）往前数连续签到了多少天，用于前端展示"连续签到N天" """
    cursor.execute(
        "SELECT checkin_date FROM checkins WHERE device_id=? ORDER BY checkin_date DESC",
        (device_id,)
    )
    dates = [datetime.strptime(r[0], "%Y-%m-%d").date() for r in cursor.fetchall()]
    if not dates:
        return 0

    streak = 0
    cursor_date = today
    date_set = set(dates)
    while cursor_date in date_set:
        streak += 1
        cursor_date -= timedelta(days=1)
    return streak

@app.post("/api/v1/checkin")
async def checkin(req: CheckinRequest, authorization: str | None = Header(default=None)):
    """每日签到，成功后账号有效期增加 CHECKIN_REWARD_MINUTES 分钟，每设备每天只能签到一次"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, req.device_id)
        device_id = identity["device_id"]
        current_expire = datetime.strptime(identity["expire_time"], "%Y-%m-%d %H:%M:%S")

        # 用 INSERT OR IGNORE 把"今天是否已签到"这件事交给数据库唯一约束原子判断，
        # 而不是先 SELECT 再 INSERT——避免手快连点/重复请求时出现 UNIQUE 报 500
        cursor.execute(
            "INSERT OR IGNORE INTO checkins (device_id, checkin_date, checkin_time) VALUES (?,?,?)",
            (device_id, today_str, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        if cursor.rowcount == 0:
            # 没有实际插入新行 = 今天已经签到过了（含并发重复请求的情况）
            conn.commit()
            return {"code": 400, "msg": "今天已经签到过啦，明天再来吧"}

        base = current_expire if current_expire > now else now
        new_expire = base + timedelta(minutes=CHECKIN_REWARD_MINUTES)

        cursor.execute("UPDATE users SET expire_time=? WHERE device_id=?",
                        (new_expire.strftime("%Y-%m-%d %H:%M:%S"), device_id))
        conn.commit()

        streak = _calc_checkin_streak(cursor, device_id, now.date())

    return {"code": 200, "msg": f"签到成功，获得{CHECKIN_REWARD_MINUTES}分钟", "data": {
        "reward_minutes": CHECKIN_REWARD_MINUTES,
        "new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S"),
        "streak_days": streak
    }}

@app.get("/api/v1/checkin_status")
async def checkin_status(
    device_id: str | None = None,
    authorization: str | None = Header(default=None)
):
    """查询今天是否已签到、连续签到天数，供客户端渲染签到按钮状态"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        identity = resolve_authenticated_device(cursor, authorization, device_id)
        resolved_device_id = identity["device_id"]

        cursor.execute(
            "SELECT 1 FROM checkins WHERE device_id=? AND checkin_date=?",
            (resolved_device_id, today_str)
        )
        checked_today = cursor.fetchone() is not None
        streak = _calc_checkin_streak(cursor, resolved_device_id, now.date())

    return {"code": 200, "data": {
        "checked_today": checked_today,
        "streak_days": streak,
        "reward_minutes": CHECKIN_REWARD_MINUTES
    }}

# ================= 6. 远程配置接口（QQ号/公告等） =================
@app.get("/api/v1/config")
async def get_config():
    """客户端启动时拉取的运营配置：客服QQ、公告等，后台随时可改，无需发版"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_config")
        rows = dict(cursor.fetchall())
    merged = {**DEFAULT_CONFIG, **rows}
    return {"code": 200, "data": merged}

@app.get("/api/admin/config")
async def admin_get_config():
    return await get_config()

@app.get("/api/v1/app_version")
async def get_app_version(platform: str = "android"):
    """客户端启动时调用，检测是否有新版本。version_code 用整数比较，避免字符串比较踩坑。
    platform: android 或 windows，不传默认 android"""
    clean_platform = platform.lower().strip()
    if clean_platform not in SUPPORTED_APP_PLATFORMS:
        return {"code": 400, "msg": f"不支持的平台: {platform}，仅支持 {SUPPORTED_APP_PLATFORMS}"}

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_config")
        rows = dict(cursor.fetchall())
    merged = {**DEFAULT_CONFIG, **rows}

    prefix = f"{clean_platform}_"
    try:
        latest_version_code = int(merged.get(f"{prefix}latest_version_code", "1"))
    except ValueError:
        latest_version_code = 1

    return {"code": 200, "data": {
        "platform": clean_platform,
        "latest_version_code": latest_version_code,
        "latest_version_name": merged.get(f"{prefix}latest_version_name", ""),
        "download_url": merged.get(f"{prefix}download_url", ""),
        "force_update": merged.get(f"{prefix}force_update", "false").lower() == "true",
        "changelog": merged.get(f"{prefix}changelog", ""),
    }}

@app.post("/api/admin/set_config")
async def set_config(req: SetConfigRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO app_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (req.key, req.value)
        )
        conn.commit()
    return {"code": 200, "msg": "配置已更新"}

# ================= 7. 管理员后台接口 =================

# --- 节点管理 ---
@app.get("/api/admin/nodes")
async def get_all_nodes():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT n.node_id, n.remark, n.is_active, COUNT(u.device_id),
                                 n.config_json, n.xui_base_url, n.xui_path,
                                 n.xui_api_token_ciphertext, n.xui_inbound_id
                          FROM nodes n
                          LEFT JOIN users u ON n.node_id = u.current_node_id
                          GROUP BY n.node_id
                          ORDER BY n.remark COLLATE NOCASE''')
        rows = cursor.fetchall()
        tag_map = get_tag_map(cursor, "node_tags", "node_id")
        return {"code": 200, "data": [
            {
                "node_id": r[0], "remark": r[1], "is_active": r[2],
                "load": r[3], "config_json": r[4],
                "xui_base_url": r[5] or "", "xui_path": r[6] or "",
                "xui_panel_url": combine_xui_panel_url(r[5] or "", r[6] or ""),
                "xui_api_token_set": bool(r[7]), "xui_inbound_id": r[8],
                "tags": tag_map.get(r[0], [])
            }
            for r in rows
        ]}


@app.get("/api/admin/tags")
async def get_all_tags():
    """返回节点侧实际存在的标签，以及每个标签覆盖的节点数和用户数。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT nt.tag,
                                 COUNT(DISTINCT nt.node_id) AS node_count,
                                 COUNT(DISTINCT ut.device_id) AS user_count
                          FROM node_tags nt
                          LEFT JOIN user_tags ut ON ut.tag = nt.tag
                          GROUP BY nt.tag
                          ORDER BY nt.tag COLLATE NOCASE''')
        rows = cursor.fetchall()
    return {"code": 200, "data": [
        {"tag": r[0], "node_count": r[1], "user_count": r[2]} for r in rows
    ]}

@app.post("/api/admin/add_node")
async def add_node(req: AddNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            json.loads(req.config_json)
            xui_base_url, xui_path = split_xui_panel_url(req.xui_base_url, req.xui_path)
            token_ciphertext = encrypt_node_api_token(req.xui_api_token) if req.xui_api_token else ""
            cursor.execute(
                '''INSERT INTO nodes
                   (node_id, remark, config_json, xray_inbound_tag, xui_base_url,
                    xui_path, xui_api_token_ciphertext, xui_inbound_id)
                   VALUES (?, ?, ?, '', ?, ?, ?, ?)''',
                (req.node_id, req.remark, req.config_json, xui_base_url,
                 xui_path, token_ciphertext, req.xui_inbound_id)
            )
            replace_node_tags(cursor, req.node_id, req.tags)
            conn.commit()
            return {"code": 200, "msg": "节点添加成功"}
        except sqlite3.IntegrityError:
            return {"code": 400, "msg": "节点 ID 已存在"}
        except Exception as e:
            return {"code": 400, "msg": f"添加失败: {str(e)}"}

@app.post("/api/admin/update_node")
async def update_node(req: UpdateNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            json.loads(req.config_json)
            xui_base_url, xui_path = split_xui_panel_url(req.xui_base_url, req.xui_path)
            cursor.execute('''SELECT xui_api_token_ciphertext, xui_base_url, xui_path, xui_inbound_id
                              FROM nodes WHERE node_id = ?''', (req.node_id,))
            existing = cursor.fetchone()
            if not existing:
                return {"code": 404, "msg": "节点不存在"}
            token_ciphertext = (
                encrypt_node_api_token(req.xui_api_token)
                if req.xui_api_token else existing[0]
            )
            old_panel = {
                "node_id": req.node_id, "base_url": existing[1] or "",
                "path": existing[2] or "", "inbound_id": existing[3],
                "api_token": decrypt_node_api_token(existing[0]) if existing[0] else "",
            }
            cursor.execute(
                '''UPDATE nodes SET remark = ?, config_json = ?, xui_base_url = ?,
                   xui_path = ?, xui_api_token_ciphertext = ?, xui_inbound_id = ?
                   WHERE node_id = ?''',
                (req.remark, req.config_json, xui_base_url, xui_path,
                 token_ciphertext, req.xui_inbound_id, req.node_id)
            )
            if cursor.rowcount == 0:
                return {"code": 404, "msg": "节点不存在"}
            replace_node_tags(cursor, req.node_id, req.tags)
            refresh_node_residential_tag_grants(cursor, req.node_id)
            cleanup_orphan_user_tags(cursor)
            clear_inaccessible_current_nodes(cursor)
            cursor.execute(
                "SELECT 1 FROM users WHERE residential_node_id = ? LIMIT 1", (req.node_id,)
            )
            if cursor.fetchone():
                new_panel = get_node_panel_config(cursor, req.node_id)
                panel_location_changed = any(
                    old_panel.get(key) != new_panel.get(key)
                    for key in ("base_url", "path", "inbound_id")
                )
                if panel_location_changed and all(old_panel.get(key) for key in ("base_url", "api_token", "inbound_id")):
                    old_ok, old_msg = await apply_node_residential_routing(old_panel, [])
                    if not old_ok:
                        conn.rollback()
                        return JSONResponse(status_code=502, content={"code": 502, "msg": old_msg})
                ok, msg = await sync_residential_nodes(cursor, {req.node_id})
                if not ok:
                    if panel_location_changed and new_panel:
                        try:
                            await apply_node_residential_routing(new_panel, [])
                        except Exception:
                            pass
                    conn.rollback()
                    await restore_residential_nodes_after_rollback(conn, {req.node_id})
                    return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
            conn.commit()
            return {"code": 200, "msg": "节点修改成功"}
        except Exception as e:
            return {"code": 400, "msg": f"修改失败: {str(e)}"}

@app.post("/api/admin/delete_node")
async def delete_node(req: DeleteNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM nodes WHERE node_id = ?", (req.node_id,))
        if not cursor.fetchone():
            return {"code": 404, "msg": "节点不存在"}
        cursor.execute("SELECT device_id FROM users WHERE residential_node_id = ?", (req.node_id,))
        affected_devices = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            "UPDATE users SET residential_egress_id = NULL, residential_node_id = NULL WHERE residential_node_id = ?",
            (req.node_id,)
        )
        removed_residential_binding = cursor.rowcount > 0
        for device_id in affected_devices:
            refresh_residential_tag_grants(cursor, device_id, None)
        if removed_residential_binding:
            ok, msg = await sync_residential_nodes(cursor, {req.node_id})
            if not ok:
                conn.rollback()
                await restore_residential_nodes_after_rollback(conn, {req.node_id})
                return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        cursor.execute("DELETE FROM node_tags WHERE node_id = ?", (req.node_id,))
        cursor.execute("DELETE FROM nodes WHERE node_id = ?", (req.node_id,))
        cursor.execute("UPDATE users SET current_node_id = NULL WHERE current_node_id = ?", (req.node_id,))
        cleanup_orphan_user_tags(cursor)
        conn.commit()
    return {"code": 200, "msg": "节点已删除"}

# --- 住宅出口管理 ---
@app.get("/api/admin/residential_egresses")
async def get_residential_egresses():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT e.id, e.name, e.protocol, e.host, e.port, e.username,
                                 e.country, e.detected_ip, e.status, e.last_test_error,
                                 e.is_active, e.updated_at,
                                 (SELECT COUNT(*) FROM users uc WHERE uc.residential_egress_id = e.id),
                                 u.device_id, u.uid, u.residential_node_id, n.remark
                          FROM residential_egresses e
                          LEFT JOIN users u ON u.residential_egress_id = e.id
                          LEFT JOIN nodes n ON n.node_id = u.residential_node_id
                          ORDER BY e.created_at DESC''')
        return {"code": 200, "data": [
            {
                "id": row[0], "name": row[1], "protocol": row[2],
                "host": row[3], "port": row[4], "username": row[5],
                "password_set": True, "country": row[6] or "",
                "detected_ip": row[7] or "", "status": row[8] or "untested",
                "last_test_error": row[9] or "", "is_active": bool(row[10]),
                "updated_at": row[11], "assigned_count": row[12],
                "assigned_device_id": row[13], "assigned_uid": row[14],
                "assigned_node_id": row[15], "assigned_node_remark": row[16] or "",
            }
            for row in cursor.fetchall()
        ]}


@app.post("/api/admin/residential_egresses")
async def create_residential_egress(req: ResidentialEgressRequest):
    if not req.password:
        return {"code": 400, "msg": "新建住宅出口时必须填写代理密码"}
    try:
        password_ciphertext = encrypt_proxy_password(req.password)
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"code": 503, "msg": str(exc)})
    egress_id = uuid.uuid4().hex[:16]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            '''INSERT INTO residential_egresses
               (id, name, protocol, host, port, username, password_ciphertext,
                country, status, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'untested', ?, ?, ?)''',
            (egress_id, req.name.strip(), req.protocol, req.host, req.port,
             req.username, password_ciphertext, req.country.strip(),
             int(req.is_active), now, now)
        )
        conn.commit()
    return {"code": 200, "msg": "住宅出口已保存，请先测试再绑定用户", "data": {"id": egress_id}}


@app.post("/api/admin/update_residential_egress")
async def update_residential_egress(req: ResidentialEgressRequest):
    if not req.id:
        return {"code": 400, "msg": "缺少住宅出口 ID"}
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT password_ciphertext, protocol, host, port, username, is_active,
                      status, detected_ip, last_test_error
               FROM residential_egresses WHERE id = ?''',
            (req.id,)
        )
        existing = cursor.fetchone()
        if not existing:
            return {"code": 404, "msg": "住宅出口不存在"}
        try:
            password_ciphertext = (
                encrypt_proxy_password(req.password) if req.password else existing[0]
            )
        except RuntimeError as exc:
            return JSONResponse(status_code=503, content={"code": 503, "msg": str(exc)})

        proxy_changed = bool(req.password) or any((
            req.protocol != existing[1], req.host != existing[2], req.port != existing[3],
            req.username != existing[4], int(req.is_active) != int(existing[5]),
        ))
        next_status = "untested" if proxy_changed else existing[6]
        next_detected_ip = "" if proxy_changed else (existing[7] or "")
        next_error = "" if proxy_changed else (existing[8] or "")
        cursor.execute(
            '''UPDATE residential_egresses
               SET name=?, protocol=?, host=?, port=?, username=?, password_ciphertext=?,
                   country=?, is_active=?, status=?, detected_ip=?, last_test_error=?, updated_at=?
               WHERE id=?''',
            (req.name.strip(), req.protocol, req.host, req.port, req.username,
             password_ciphertext, req.country.strip(), int(req.is_active), next_status,
             next_detected_ip, next_error,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), req.id)
        )
        cursor.execute(
            "SELECT device_id, residential_node_id FROM users WHERE residential_egress_id = ?", (req.id,)
        )
        bound_user = cursor.fetchone()
        if bound_user and proxy_changed:
            if req.is_active:
                try:
                    plain_password = decrypt_proxy_password(password_ciphertext)
                except RuntimeError as exc:
                    conn.rollback()
                    return JSONResponse(status_code=503, content={"code": 503, "msg": str(exc)})
                test_ok, detected_ip, test_error = await probe_residential_proxy({
                    "protocol": req.protocol, "host": req.host, "port": req.port,
                    "username": req.username, "password": plain_password,
                })
                if not test_ok:
                    conn.rollback()
                    return JSONResponse(
                        status_code=502,
                        content={"code": 502, "msg": f"新配置测试失败，原配置已保留: {test_error}"}
                    )
                cursor.execute(
                    '''UPDATE residential_egresses
                       SET status='available', detected_ip=?, last_test_error='' WHERE id=?''',
                    (detected_ip, req.id)
                )
            else:
                cursor.execute('''UPDATE users
                                  SET residential_egress_id=NULL, residential_node_id=NULL
                                  WHERE device_id=?''', (bound_user[0],))
                refresh_residential_tag_grants(cursor, bound_user[0], None)
            affected_nodes = {bound_user[1]}
            ok, msg = await sync_residential_nodes(cursor, affected_nodes)
            if not ok:
                conn.rollback()
                await restore_residential_nodes_after_rollback(conn, affected_nodes)
                return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        conn.commit()
    return {"code": 200, "msg": "住宅出口已更新并应用"}


@app.post("/api/admin/test_residential_egress")
async def test_residential_egress(req: ResidentialEgressIdRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT protocol, host, port, username, password_ciphertext
                          FROM residential_egresses WHERE id = ?''', (req.id,))
        row = cursor.fetchone()
        if not row:
            return {"code": 404, "msg": "住宅出口不存在"}
        try:
            password = decrypt_proxy_password(row[4])
        except RuntimeError as exc:
            return JSONResponse(status_code=503, content={"code": 503, "msg": str(exc)})
        ok, detected_ip, error = await probe_residential_proxy({
            "protocol": row[0], "host": row[1], "port": row[2],
            "username": row[3], "password": password,
        })
        cursor.execute(
            '''UPDATE residential_egresses
               SET status=?, detected_ip=?, last_test_error=?, updated_at=? WHERE id=?''',
            ("available" if ok else "error", detected_ip, error,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), req.id)
        )
        conn.commit()
    if not ok:
        return JSONResponse(
            status_code=502,
            content={"code": 502, "msg": f"代理测试失败: {error}"}
        )
    return {"code": 200, "msg": "代理连接正常", "data": {"detected_ip": detected_ip}}


@app.post("/api/admin/bind_residential_egress")
async def bind_residential_egress(req: BindResidentialEgressRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT uid FROM users WHERE device_id = ?", (req.device_id,))
        user = cursor.fetchone()
        if not user:
            return {"code": 404, "msg": "用户不存在"}

        if bool(req.residential_egress_id) != bool(req.node_id):
            return {"code": 400, "msg": "绑定住宅出口时必须同时选择指定节点"}

        cursor.execute("SELECT residential_node_id FROM users WHERE device_id = ?", (req.device_id,))
        old_node_id = cursor.fetchone()[0]

        if req.residential_egress_id:
            cursor.execute(
                "SELECT status, is_active FROM residential_egresses WHERE id = ?",
                (req.residential_egress_id,)
            )
            egress = cursor.fetchone()
            if not egress:
                return {"code": 404, "msg": "住宅出口不存在"}
            if not egress[1]:
                return {"code": 400, "msg": "该住宅出口已停用"}
            if egress[0] != "available":
                return {"code": 400, "msg": "请先测试住宅出口，确认可用后再绑定"}
            cursor.execute('''SELECT xui_base_url, xui_path, xui_api_token_ciphertext,
                                     xui_inbound_id
                              FROM nodes WHERE node_id = ? AND is_active = 1''',
                           (req.node_id,))
            node_panel = cursor.fetchone()
            if not node_panel:
                return {"code": 400, "msg": "指定节点不存在或已停用"}
            if not node_panel[0] or not node_panel[2] or not node_panel[3]:
                return {"code": 400, "msg": "该节点尚未完整配置 3x-ui 面板地址、API Token 和入站 ID"}
            cursor.execute(
                '''SELECT uid FROM users
                   WHERE residential_egress_id = ? AND device_id <> ?''',
                (req.residential_egress_id, req.device_id)
            )
            occupied = cursor.fetchone()
            if occupied:
                return {"code": 409, "msg": f"该住宅出口已被 UID {occupied[0]} 占用"}

        cursor.execute(
            "UPDATE users SET residential_egress_id = ?, residential_node_id = ? WHERE device_id = ?",
            (req.residential_egress_id or None, req.node_id or None, req.device_id)
        )
        refresh_residential_tag_grants(cursor, req.device_id, req.node_id)
        affected_nodes = {old_node_id, req.node_id} - {None, ""}
        ok, msg = await sync_residential_nodes(cursor, affected_nodes)
        if not ok:
            conn.rollback()
            await restore_residential_nodes_after_rollback(conn, affected_nodes)
            return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        conn.commit()
    return {"code": 200, "msg": "住宅出口已应用；节点标签已自动授予，客户端将获得专属 UUID"}


@app.post("/api/admin/delete_residential_egress")
async def delete_residential_egress(req: ResidentialEgressIdRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM residential_egresses WHERE id = ?", (req.id,))
        if not cursor.fetchone():
            return {"code": 404, "msg": "住宅出口不存在"}
        cursor.execute(
            "SELECT device_id, residential_node_id FROM users WHERE residential_egress_id = ?",
            (req.id,)
        )
        affected_users = cursor.fetchall()
        cursor.execute(
            "UPDATE users SET residential_egress_id = NULL, residential_node_id = NULL WHERE residential_egress_id = ?",
            (req.id,)
        )
        for device_id, _ in affected_users:
            refresh_residential_tag_grants(cursor, device_id, None)
        affected_nodes = {row[1] for row in affected_users if row[1]}
        ok, msg = await sync_residential_nodes(cursor, affected_nodes)
        if not ok:
            conn.rollback()
            await restore_residential_nodes_after_rollback(conn, affected_nodes)
            return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        cursor.execute("DELETE FROM residential_egresses WHERE id = ?", (req.id,))
        conn.commit()
    return {"code": 200, "msg": "住宅出口已删除，原绑定用户已自动解除"}


# --- 用户管理 ---
@app.post("/api/admin/create_device_claim_code")
async def create_device_claim_code(req: CreateDeviceClaimCodeRequest):
    """为老用户签发一次性设备令牌领取码；明文只返回本次。"""
    now = datetime.now()
    expire_time = now + timedelta(minutes=req.valid_minutes)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT device_id FROM users WHERE uid = ?", (req.uid.strip(),))
        user = cursor.fetchone()
        if not user:
            return {"code": 404, "msg": "用户UID不存在", "data": None}
        device_id = user[0]

        cursor.execute(
            "DELETE FROM device_claim_codes WHERE device_id = ? AND used_time IS NULL",
            (device_id,)
        )
        claim_code = generate_claim_code()
        cursor.execute(
            """INSERT INTO device_claim_codes
               (code_hash, device_id, expire_time, created_time)
               VALUES (?, ?, ?, ?)""",
            (
                hash_secret(normalize_claim_code(claim_code)),
                device_id,
                expire_time.strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        conn.commit()
    return {"code": 200, "msg": "一次性领取码已生成", "data": {
        "uid": req.uid.strip(),
        "claim_code": claim_code,
        "expire_time": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
    }}


@app.post("/api/admin/revoke_device_token")
async def revoke_device_token(req: RevokeDeviceTokenRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET api_token_hash = NULL, api_token_created_at = NULL WHERE uid = ?",
            (req.uid.strip(),)
        )
        if cursor.rowcount == 0:
            return {"code": 404, "msg": "用户UID不存在"}
        conn.commit()
    return {"code": 200, "msg": "设备令牌已撤销"}


@app.get("/api/admin/users")
async def get_all_users(uid: str | None = None):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        clean_uid = (uid or "").strip()
        if clean_uid:
            cursor.execute(
                """SELECT device_id, expire_time, current_node_id, invite_code, uid, api_token_hash,
                          residential_egress_id, residential_node_id, remark
                   FROM users
                   WHERE instr(uid, ?) > 0
                   ORDER BY expire_time DESC""",
                (clean_uid,)
            )
        else:
            cursor.execute(
                """SELECT device_id, expire_time, current_node_id, invite_code, uid, api_token_hash,
                          residential_egress_id, residential_node_id, remark
                   FROM users ORDER BY expire_time DESC"""
            )
        rows = cursor.fetchall()
        tag_map = get_tag_map(cursor, "user_tags", "device_id")
        manual_tag_map = get_tag_map(cursor, "manual_user_tags", "device_id")
        residential_tag_map = get_tag_map(cursor, "residential_tag_grants", "device_id")
        cursor.execute("SELECT id, name, status FROM residential_egresses")
        egress_map = {row[0]: {"name": row[1], "status": row[2]} for row in cursor.fetchall()}
        cursor.execute("SELECT node_id, remark, xui_base_url, xui_inbound_id FROM nodes")
        node_map = {
            row[0]: {"remark": row[1] or row[0], "xui_configured": bool(row[2] and row[3])}
            for row in cursor.fetchall()
        }
        return {"code": 200, "data": [
            {
                "device_id": r[0], "expire_time": r[1],
                "current_node_id": r[2] or '未连接', "invite_code": r[3] or '',
                "uid": r[4] or '', "tags": tag_map.get(r[0], []),
                "manual_tags": manual_tag_map.get(r[0], []),
                "residential_auto_tags": residential_tag_map.get(r[0], []),
                "has_device_token": bool(r[5]),
                "residential_egress_id": r[6],
                "residential_egress_name": egress_map.get(r[6], {}).get("name", ""),
                "residential_egress_status": egress_map.get(r[6], {}).get("status", ""),
                "residential_node_id": r[7],
                "residential_node_remark": node_map.get(r[7], {}).get("remark", ""),
                "residential_node_xui_configured": node_map.get(r[7], {}).get("xui_configured", False),
                "remark": r[8] or ""
            }
            for r in rows
        ]}


@app.post("/api/admin/set_user_remark")
async def set_user_remark(req: SetUserRemarkRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET remark = ? WHERE device_id = ?",
            (req.remark.strip(), req.device_id)
        )
        if cursor.rowcount == 0:
            return {"code": 404, "msg": "用户不存在"}
        conn.commit()
    return {"code": 200, "msg": "用户备注已保存"}


@app.post("/api/admin/set_user_tags")
async def set_user_tags(req: SetUserTagsRequest):
    """设置用户标签；只能选择当前至少被一个节点使用的标签。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE device_id = ?", (req.device_id,))
        if not cursor.fetchone():
            return {"code": 404, "msg": "用户不存在"}

        if req.tags:
            placeholders = ",".join("?" for _ in req.tags)
            cursor.execute(
                f"SELECT DISTINCT tag FROM node_tags WHERE tag IN ({placeholders})",
                req.tags
            )
            existing = {row[0] for row in cursor.fetchall()}
            missing = [tag for tag in req.tags if tag not in existing]
            if missing:
                return {"code": 400, "msg": f"以下标签没有对应节点，不能分配给用户: {', '.join(missing)}"}

        replace_user_tags(cursor, req.device_id, req.tags)
        clear_inaccessible_current_nodes(cursor)
        conn.commit()
    return {"code": 200, "msg": "用户标签已更新"}

@app.post("/api/admin/update_user_time")
async def update_user_time(req: UpdateUserRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT expire_time FROM users WHERE device_id = ?", (req.device_id,))
        result = cursor.fetchone()
        if not result: return {"code": 404, "msg": "用户不存在"}
        current = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        new_time = current + timedelta(days=req.add_days)
        cursor.execute("UPDATE users SET expire_time = ? WHERE device_id = ?", (new_time.strftime("%Y-%m-%d %H:%M:%S"), req.device_id))
        cursor.execute("SELECT residential_node_id FROM users WHERE device_id = ?", (req.device_id,))
        residential_node = cursor.fetchone()[0]
        if residential_node:
            ok, msg = await sync_residential_nodes(cursor, {residential_node})
            if not ok:
                conn.rollback()
                await restore_residential_nodes_after_rollback(conn, {residential_node})
                return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        conn.commit()
    return {"code": 200, "msg": "修改成功"}

@app.post("/api/admin/set_user_time")
async def set_user_time(req: SetUserTimeRequest):
    """允许管理员自由设定精确的过期时间"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            datetime.strptime(req.expire_time, "%Y-%m-%d %H:%M:%S")
            cursor.execute("UPDATE users SET expire_time = ? WHERE device_id = ?", (req.expire_time, req.device_id))
            if cursor.rowcount == 0:
                return {"code": 404, "msg": "用户不存在"}
            cursor.execute("SELECT residential_node_id FROM users WHERE device_id = ?", (req.device_id,))
            residential_node = cursor.fetchone()[0]
            if residential_node:
                ok, msg = await sync_residential_nodes(cursor, {residential_node})
                if not ok:
                    conn.rollback()
                    await restore_residential_nodes_after_rollback(conn, {residential_node})
                    return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
            conn.commit()
            return {"code": 200, "msg": "时间设置成功"}
        except ValueError:
            return {"code": 400, "msg": "时间格式错误，必须为 YYYY-MM-DD HH:MM:SS"}

@app.post("/api/admin/delete_user")
async def delete_user(req: DeleteUserRequest):
    """删除用户，同时清理该设备关联的签到记录/邀请关系/邀请奖励发放记录，
    保证这个 device_id 删除后能从"全新设备"状态重新签到/被邀请，
    不会因为历史 checkins 等记录残留导致重新签到时被误判"今天已签到过"。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT residential_egress_id, residential_node_id FROM users WHERE device_id = ?", (req.device_id,)
        )
        user_row = cursor.fetchone()
        if not user_row:
            return {"code": 404, "msg": "用户不存在"}
        had_residential_egress = bool(user_row[0])
        cursor.execute("DELETE FROM user_tags WHERE device_id = ?", (req.device_id,))
        cursor.execute("DELETE FROM manual_user_tags WHERE device_id = ?", (req.device_id,))
        cursor.execute("DELETE FROM residential_tag_grants WHERE device_id = ?", (req.device_id,))
        cursor.execute("DELETE FROM device_claim_codes WHERE device_id = ?", (req.device_id,))
        cursor.execute("DELETE FROM users WHERE device_id = ?", (req.device_id,))
        cursor.execute("DELETE FROM checkins WHERE device_id = ?", (req.device_id,))
        cursor.execute(
            "DELETE FROM referrals WHERE invitee_device_id = ? OR inviter_device_id = ?",
            (req.device_id, req.device_id)
        )
        cursor.execute("DELETE FROM referral_rewards WHERE inviter_device_id = ?", (req.device_id,))
        if had_residential_egress:
            affected_nodes = {user_row[1]}
            ok, msg = await sync_residential_nodes(cursor, affected_nodes)
            if not ok:
                conn.rollback()
                await restore_residential_nodes_after_rollback(conn, affected_nodes)
                return JSONResponse(status_code=502, content={"code": 502, "msg": msg})
        conn.commit()
    return {"code": 200, "msg": "用户已删除"}

@app.get("/api/admin/referrals")
async def get_all_referrals():
    """后台查看邀请关系明细，方便核对/排查异常刷邀请"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT invitee_device_id, inviter_device_id, created_time FROM referrals ORDER BY created_time DESC")
        return {"code": 200, "data": [
            {"invitee_device_id": r[0], "inviter_device_id": r[1], "created_time": r[2]}
            for r in cursor.fetchall()
        ]}

# --- 卡密管理 ---
@app.post("/api/admin/generate_codes")
async def generate_codes(count: int = 10, days: int = 30):
    new_codes = []
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        for _ in range(count):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
            cursor.execute("INSERT INTO activation_codes (code, days) VALUES (?, ?)", (code, days))
            new_codes.append(code)
        conn.commit()
    return {"code": 200, "data": new_codes}

# --- 应用市场管理 ---
@app.get("/api/admin/market_apps")
async def get_all_market_apps():
    """管理端获取全部应用（含已下架），用于后台列表展示和编辑"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT id, name, icon_url, price, version, apk_url, desc, package_name, sort_order, is_active
                       FROM market_apps ORDER BY sort_order ASC, id ASC''')
        rows = cursor.fetchall()
    return {"code": 200, "data": [
        {"id": r[0], "name": r[1], "icon_url": r[2], "price": r[3], "version": r[4],
         "apk_url": r[5], "desc": r[6], "package_name": r[7], "sort_order": r[8], "is_active": bool(r[9])}
        for r in rows
    ]}

@app.post("/api/admin/add_market_app")
async def add_market_app(req: AddMarketAppRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''INSERT INTO market_apps (id, name, icon_url, price, version, apk_url, desc, package_name, sort_order)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (req.id, req.name, req.icon_url, req.price, req.version, req.apk_url, req.desc, req.package_name, req.sort_order))
            conn.commit()
            return {"code": 200, "msg": "应用添加成功"}
        except sqlite3.IntegrityError:
            return {"code": 400, "msg": "该 id 已存在"}
        except Exception as e:
            return {"code": 400, "msg": f"添加失败: {str(e)}"}

@app.post("/api/admin/update_market_app")
async def update_market_app(req: UpdateMarketAppRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''UPDATE market_apps SET name = ?, icon_url = ?, price = ?, version = ?,
                       apk_url = ?, desc = ?, package_name = ?, sort_order = ?, is_active = ? WHERE id = ?''',
                        (req.name, req.icon_url, req.price, req.version, req.apk_url,
                         req.desc, req.package_name, req.sort_order, req.is_active, req.id))
        conn.commit()
        if cursor.rowcount == 0:
            return {"code": 404, "msg": "应用不存在"}
    return {"code": 200, "msg": "应用修改成功"}

@app.post("/api/admin/delete_market_app")
async def delete_market_app(req: DeleteMarketAppRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM market_apps WHERE id = ?", (req.id,))
        conn.commit()
    return {"code": 200, "msg": "应用已删除"}

# --- 教程图片管理（按 安卓/苹果/Windows 分类，每类可绑定多张图文 URL） ---
@app.get("/api/admin/tutorial_images")
async def get_all_tutorial_images(platform: str = None):
    """管理端获取教程图片，含已下架，用于后台列表展示和编辑。
    不传 platform 则返回全部三个平台的数据，方便一次性管理。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if platform:
            clean_platform = platform.lower().strip()
            if clean_platform not in SUPPORTED_TUTORIAL_PLATFORMS:
                return {"code": 400, "msg": f"不支持的平台: {platform}，仅支持 {SUPPORTED_TUTORIAL_PLATFORMS}"}
            cursor.execute(
                '''SELECT id, platform, image_url, title, sort_order, is_active FROM tutorial_images
                   WHERE platform = ? ORDER BY sort_order ASC, id ASC''',
                (clean_platform,)
            )
        else:
            cursor.execute(
                '''SELECT id, platform, image_url, title, sort_order, is_active FROM tutorial_images
                   ORDER BY platform ASC, sort_order ASC, id ASC'''
            )
        rows = cursor.fetchall()
    return {"code": 200, "data": [
        {"id": r[0], "platform": r[1], "image_url": r[2], "title": r[3], "sort_order": r[4], "is_active": bool(r[5])}
        for r in rows
    ]}

@app.post("/api/admin/add_tutorial_image")
async def add_tutorial_image(req: AddTutorialImageRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''INSERT INTO tutorial_images (id, platform, image_url, title, sort_order)
                   VALUES (?, ?, ?, ?, ?)''',
                (req.id, req.platform, req.image_url, req.title, req.sort_order)
            )
            conn.commit()
            return {"code": 200, "msg": "教程图片添加成功"}
        except sqlite3.IntegrityError:
            return {"code": 400, "msg": "该 id 已存在"}
        except Exception as e:
            return {"code": 400, "msg": f"添加失败: {str(e)}"}

@app.post("/api/admin/update_tutorial_image")
async def update_tutorial_image(req: UpdateTutorialImageRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''UPDATE tutorial_images SET platform = ?, image_url = ?, title = ?, sort_order = ?, is_active = ?
               WHERE id = ?''',
            (req.platform, req.image_url, req.title, req.sort_order, req.is_active, req.id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"code": 404, "msg": "教程图片不存在"}
    return {"code": 200, "msg": "教程图片修改成功"}

@app.post("/api/admin/delete_tutorial_image")
async def delete_tutorial_image(req: DeleteTutorialImageRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tutorial_images WHERE id = ?", (req.id,))
        conn.commit()
    return {"code": 200, "msg": "教程图片已删除"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
