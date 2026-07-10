from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
import sqlite3
import uvicorn
import random
import string
import json

DB_FILE = "vpn_data.db"

# ================= 1. 数据库初始化 =================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (device_id TEXT PRIMARY KEY, expire_time DATETIME, current_node_id TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS activation_codes (code TEXT PRIMARY KEY, days INTEGER, is_used BOOLEAN DEFAULT 0, used_by TEXT, used_time DATETIME)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS nodes (node_id TEXT PRIMARY KEY, remark TEXT, config_json TEXT, is_active BOOLEAN DEFAULT 1)''')
        conn.commit()

init_db()

app = FastAPI(title="秒连 VPN 后台 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= 2. 请求体模型 =================
class NodeRequest(BaseModel): device_id: str
class RechargeRequest(BaseModel): device_id: str; code: str
class AddNodeRequest(BaseModel): node_id: str; remark: str; config_json: str
class UpdateNodeRequest(BaseModel): node_id: str; remark: str; config_json: str
class DeleteNodeRequest(BaseModel): node_id: str
class DeleteUserRequest(BaseModel): device_id: str
class UpdateUserRequest(BaseModel): device_id: str; add_days: int
class SetUserTimeRequest(BaseModel): device_id: str; expire_time: str

# ================= 3. 客户端核心接口 =================
@app.post("/api/v1/get_node")
async def get_node(req: NodeRequest):
    now = datetime.now()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT expire_time FROM users WHERE device_id = ?", (req.device_id,))
        result = cursor.fetchone()
        
        if result is None:
            # 【核心修改】新设备首次连接，仅赠送 10 分钟体验时间！
            expire_time = now + timedelta(minutes=10)
            cursor.execute("INSERT INTO users (device_id, expire_time) VALUES (?, ?)", (req.device_id, expire_time.strftime("%Y-%m-%d %H:%M:%S")))
        else:
            expire_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
            
        if now > expire_time:
            return {"code": 403, "msg": "时长已过期，请充值", "data": None}
            
        # 负载均衡分配节点
        cursor.execute('''SELECT n.node_id, n.config_json FROM nodes n LEFT JOIN users u ON n.node_id = u.current_node_id WHERE n.is_active = 1 GROUP BY n.node_id ORDER BY COUNT(u.device_id) ASC LIMIT 1''')
        best_node = cursor.fetchone()
        
        if not best_node: return {"code": 500, "msg": "当前无可用节点", "data": None}
        
        cursor.execute("UPDATE users SET current_node_id = ? WHERE device_id = ?", (best_node[0], req.device_id))
        conn.commit()
        
    return {"code": 200, "msg": "成功", "data": {"expire_time": expire_time.strftime("%Y-%m-%d %H:%M:%S"), "node": json.loads(best_node[1])}}

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
            cursor.execute("INSERT INTO users (device_id, expire_time) VALUES (?, ?)", (req.device_id, new_expire.strftime("%Y-%m-%d %H:%M:%S")))
            
        cursor.execute("UPDATE activation_codes SET is_used = 1, used_by = ?, used_time = ? WHERE code = ?", (req.device_id, now.strftime("%Y-%m-%d %H:%M:%S"), req.code))
        conn.commit()
    return {"code": 200, "msg": "充值成功", "data": {"new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S")}}

# ================= 4. 管理员后台接口 =================

# --- 节点管理 ---
@app.get("/api/admin/nodes")
async def get_all_nodes():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # 把 config_json 也返回给前端，方便修改时填入输入框
        cursor.execute('''SELECT n.node_id, n.remark, n.is_active, COUNT(u.device_id), n.config_json FROM nodes n LEFT JOIN users u ON n.node_id = u.current_node_id GROUP BY n.node_id''')
        return {"code": 200, "data": [{"node_id": r[0], "remark": r[1], "is_active": r[2], "load": r[3], "config_json": r[4]} for r in cursor.fetchall()]}

@app.post("/api/admin/add_node")
async def add_node(req: AddNodeRequest):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            json.loads(req.config_json) # 格式防呆校验
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
        cursor.execute("SELECT device_id, expire_time, current_node_id FROM users ORDER BY expire_time DESC")
        return {"code": 200, "data": [{"device_id": r[0], "expire_time": r[1], "current_node_id": r[2] or '未连接'} for r in cursor.fetchall()]}

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
            # 校验时间格式
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)