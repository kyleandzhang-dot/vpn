from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
import sqlite3
import uvicorn
import random
import string
import json

from payment_hupijiao import router as payment_router, init_payment_db

DB_FILE = "vpn_data.db"

# ================= 邀请阶梯规则 =================
# (累计邀请人数, 达到该档位在原有基础上再增加的奖励天数，叠加发放)
# 1人 +2天，3人 再+1天(累计3天)，5人 再+4天(累计7天)，10人 再+23天(累计30天/一个月)
REFERRAL_MILESTONES = [(1, 2), (3, 1), (5, 4), (10, 23)]

# 远程配置默认值（数据库里没有对应 key 时使用）
DEFAULT_CONFIG = {
    "buy_qq": "1772757914",
    "agent_qq": "1772757914",
    "announcement": "",          # 公告内容，留空则客户端不弹窗
    "announcement_title": "公告",
}

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
        cursor.execute('''CREATE TABLE IF NOT EXISTS nodes (node_id TEXT PRIMARY KEY, remark TEXT, config_json TEXT, is_active BOOLEAN DEFAULT 1)''')
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

        # 通用远程配置表：QQ号、公告等 key-value，后台可直接改，无需发版
        cursor.execute('''CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT
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

app.include_router(payment_router)  # 挂载虎皮椒支付路由

# ================= 2. 请求体模型 =================
class NodeRequest(BaseModel): device_id: str
class RechargeRequest(BaseModel): device_id: str; code: str
class AddNodeRequest(BaseModel): node_id: str; remark: str; config_json: str
class UpdateNodeRequest(BaseModel): node_id: str; remark: str; config_json: str
class DeleteNodeRequest(BaseModel): node_id: str
class DeleteUserRequest(BaseModel): device_id: str
class UpdateUserRequest(BaseModel): device_id: str; add_days: int
class SetUserTimeRequest(BaseModel): device_id: str; expire_time: str

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

class BindInviteRequest(BaseModel):
    device_id: str
    invite_code: str

class SetConfigRequest(BaseModel):
    key: str
    value: str

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

# ================= 4. 客户端核心接口 =================
@app.post("/api/v1/get_node")
async def get_node(req: NodeRequest):
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT expire_time, invite_code FROM users WHERE device_id = ?", (req.device_id,))
        result = cursor.fetchone()

        if result is None:
            # 新设备首次连接，赠送 10 分钟体验时间，并生成专属邀请码
            expire_time = now + timedelta(minutes=10)
            invite_code = generate_invite_code(cursor)
            cursor.execute(
                "INSERT INTO users (device_id, expire_time, invite_code) VALUES (?, ?, ?)",
                (req.device_id, expire_time.strftime("%Y-%m-%d %H:%M:%S"), invite_code)
            )
        else:
            expire_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
            invite_code = result[1]
            if not invite_code:
                # 兼容老用户：之前没有邀请码的补发一个
                invite_code = generate_invite_code(cursor)
                cursor.execute("UPDATE users SET invite_code=? WHERE device_id=?", (invite_code, req.device_id))

        if now > expire_time:
            conn.commit()
            return {"code": 403, "msg": "时长已过期，请充值", "data": None}

        # 负载均衡分配节点
        cursor.execute('''SELECT n.node_id, n.config_json FROM nodes n LEFT JOIN users u ON n.node_id = u.current_node_id WHERE n.is_active = 1 GROUP BY n.node_id ORDER BY COUNT(u.device_id) ASC LIMIT 1''')
        best_node = cursor.fetchone()

        if not best_node:
            conn.commit()
            return {"code": 500, "msg": "当前无可用节点", "data": None}

        cursor.execute("UPDATE users SET current_node_id = ? WHERE device_id = ?", (best_node[0], req.device_id))
        conn.commit()

    return {"code": 200, "msg": "成功", "data": {
        "expire_time": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
        "node": json.loads(best_node[1]),
        "invite_code": invite_code
    }}

@app.post("/api/v1/recharge")
async def recharge(req: RechargeRequest):
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT days, is_used FROM activation_codes WHERE code = ?", (req.code,))
        code_record = cursor.fetchone()
        if not code_record or code_record[1]: return {"code": 400, "msg": "激活码无效或已被使用"}

        cursor.execute("SELECT expire_time FROM users WHERE device_id = ?", (req.device_id,))
        user_record = cursor.fetchone()

        base_time = datetime.strptime(user_record[0], "%Y-%m-%d %H:%M:%S") if user_record and datetime.strptime(user_record[0], "%Y-%m-%d %H:%M:%S") > now else now
        new_expire = base_time + timedelta(days=code_record[0])

        if user_record:
            cursor.execute("UPDATE users SET expire_time = ? WHERE device_id = ?", (new_expire.strftime("%Y-%m-%d %H:%M:%S"), req.device_id))
        else:
            invite_code = generate_invite_code(cursor)
            cursor.execute(
                "INSERT INTO users (device_id, expire_time, invite_code) VALUES (?, ?, ?)",
                (req.device_id, new_expire.strftime("%Y-%m-%d %H:%M:%S"), invite_code)
            )

        cursor.execute("UPDATE activation_codes SET is_used = 1, used_by = ?, used_time = ? WHERE code = ?", (req.device_id, now.strftime("%Y-%m-%d %H:%M:%S"), req.code))
        conn.commit()
    return {"code": 200, "msg": "充值成功", "data": {"new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S")}}

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

@app.post("/api/v1/check_status")
async def check_status(req: NodeRequest):
    """心跳接口：前端每分钟来问一次，如果过期返回 403"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT expire_time FROM users WHERE device_id = ?", (req.device_id,))
        result = cursor.fetchone()

        if not result:
            return {"code": 404, "msg": "用户不存在"}

        expire_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > expire_time:
            return {"code": 403, "msg": "已过期"}

    return {"code": 200, "msg": "正常"}

# ================= 5. 邀请好友系统接口 =================
@app.post("/api/v1/bind_invite")
async def bind_invite(req: BindInviteRequest):
    """新用户填写好友的邀请码，建立邀请关系并检查是否触发邀请人的阶梯奖励"""
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT invited_by FROM users WHERE device_id=?", (req.device_id,))
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
        if inviter_id == req.device_id:
            return {"code": 400, "msg": "不能邀请自己"}

        cursor.execute(
            "INSERT INTO referrals (invitee_device_id, inviter_device_id, created_time) VALUES (?,?,?)",
            (req.device_id, inviter_id, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        cursor.execute("UPDATE users SET invited_by=? WHERE device_id=?", (inviter_id, req.device_id))

        check_and_grant_referral_rewards(cursor, inviter_id, now)
        conn.commit()

    return {"code": 200, "msg": "绑定成功，好友助力已生效"}

@app.get("/api/v1/invite_info")
async def invite_info(device_id: str):
    """查询自己的邀请码、已邀请人数、已发放的档位、下一档还差多少人"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT invite_code FROM users WHERE device_id=?", (device_id,))
        r = cursor.fetchone()
        if not r:
            return {"code": 404, "msg": "用户不存在"}
        invite_code = r[0]

        cursor.execute("SELECT COUNT(*) FROM referrals WHERE inviter_device_id=?", (device_id,))
        count = cursor.fetchone()[0]

        cursor.execute("SELECT milestone FROM referral_rewards WHERE inviter_device_id=?", (device_id,))
        rewarded = [x[0] for x in cursor.fetchall()]

    next_milestone = next((m for m, d in REFERRAL_MILESTONES if m > count), None)
    return {"code": 200, "data": {
        "invite_code": invite_code,
        "invited_count": count,
        "rewarded_milestones": rewarded,
        "milestones": REFERRAL_MILESTONES,
        "next_milestone": next_milestone
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
        cursor.execute('''SELECT n.node_id, n.remark, n.is_active, COUNT(u.device_id), n.config_json FROM nodes n LEFT JOIN users u ON n.node_id = u.current_node_id GROUP BY n.node_id''')
        return {"code": 200, "data": [{"node_id": r[0], "remark": r[1], "is_active": r[2], "load": r[3], "config_json": r[4]} for r in cursor.fetchall()]}

@app.post("/api/admin/add_node")
async def add_node(req: AddNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            json.loads(req.config_json)
            cursor.execute("INSERT INTO nodes (node_id, remark, config_json) VALUES (?, ?, ?)", (req.node_id, req.remark, req.config_json))
            conn.commit()
            return {"code": 200, "msg": "节点添加成功"}
        except Exception as e:
            return {"code": 400, "msg": f"添加失败，JSON 格式错误: {str(e)}"}

@app.post("/api/admin/update_node")
async def update_node(req: UpdateNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            json.loads(req.config_json)
            cursor.execute("UPDATE nodes SET remark = ?, config_json = ? WHERE node_id = ?", (req.remark, req.config_json, req.node_id))
            conn.commit()
            return {"code": 200, "msg": "节点修改成功"}
        except Exception as e:
            return {"code": 400, "msg": f"修改失败，JSON 格式错误: {str(e)}"}

@app.post("/api/admin/delete_node")
async def delete_node(req: DeleteNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nodes WHERE node_id = ?", (req.node_id,))
        cursor.execute("UPDATE users SET current_node_id = NULL WHERE current_node_id = ?", (req.node_id,))
        conn.commit()
    return {"code": 200, "msg": "节点已删除"}

# --- 用户管理 ---
@app.get("/api/admin/users")
async def get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT device_id, expire_time, current_node_id, invite_code FROM users ORDER BY expire_time DESC")
        return {"code": 200, "data": [
            {"device_id": r[0], "expire_time": r[1], "current_node_id": r[2] or '未连接', "invite_code": r[3] or ''}
            for r in cursor.fetchall()
        ]}

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
            conn.commit()
            return {"code": 200, "msg": "时间设置成功"}
        except ValueError:
            return {"code": 400, "msg": "时间格式错误，必须为 YYYY-MM-DD HH:MM:SS"}

@app.post("/api/admin/delete_user")
async def delete_user(req: DeleteUserRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE device_id = ?", (req.device_id,))
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)