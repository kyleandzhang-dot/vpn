"""
虎皮椒支付模块
========================================================
接入方式：在 main.py 里
    from payment_hupijiao import router as payment_router, init_payment_db
    init_payment_db()                 # 放在 init_db() 之后
    app.include_router(payment_router)

数据库沿用 main.py 里的 vpn_data.db（同一个 sqlite 文件），
新增一张 payment_orders 表用来记录订单、防止 webhook 重复到账。

========================================================
发放策略（按 source + platform 分流）：
========================================================
1. source = "app"（客户端内跳转购买）
   → 已知 device_id，支付成功直接给该设备加时长，无需用户操作。

2. source = "website" 且 platform in ("android", "windows")
   → 网站购买时不确定用在哪台设备/哪个客户端，支付成功后生成一个
     激活码，写回订单记录；网站轮询订单状态接口即可拿到这个码，
     用户自行在 App 里"输入激活码"兑换（复用 main.py 已有的
     /api/v1/recharge 逻辑和 activation_codes 表）。

3. source = "website" 且 platform in ("ios", "mac")
   → 暂不支持，接口先留着但直接拒绝下单，返回明确提示。
     注意：iOS 端如果是应用内消费的数字服务(VPN 订阅通常属于此类)，
     苹果政策一般要求走 App Store 内购(IAP)，不能引导用户去站外
     充值，所以这块以后大概率是接 Apple IAP，而不是简单地"支持"
     虎皮椒当前这套外部支付流程。
========================================================
"""

import time
import uuid
import random
import string
import hashlib
import sqlite3
import httpx
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

DB_FILE = "vpn_data.db"  # 必须和 main.py 里的 DB_FILE 保持一致

# ================= 虎皮椒支付配置（换成你自己的） =================
HUPIJIAO_APP_ID = "201906177579"
HUPIJIAO_APP_SECRET = "729ff851d182920a35e683334f91d5ff"
HUPIJIAO_API_URL = "https://api.xunhupay.com/payment/do.html"  # 虎皮椒下单接口地址，以你后台实际给的为准

# 这里必须填你服务器【公网可访问】的地址，虎皮椒才能把回调打进来
# payment_hupijiao.py 里的配置[cite: 2]
NOTIFY_BASE_URL = "https://shop.jmsht.one"  # 虎皮椒异步回调通知发货地址[cite: 2]
RETURN_URL = "https://shop.jmsht.one"            # 用户付完款点击“返回”后的跳转页[cite: 2]

router = APIRouter(tags=["Payment"])

# ================= 充值套餐配置 =================
# product_id -> 价格(元) / 增加天数 / 展示名
# 价格和天数一定要写死在服务端，不能信任客户端传的金额，否则会被篡改支付金额
RECHARGE_PRODUCTS = {
    1: {"name": "月卡",  "price": 0.01,  "days": 30},
    2: {"name": "年卡",  "price": 0.02, "days": 365},
    3: {"name": "永久",  "price": 0.03, "days": 1800},
}

VALID_SOURCES = ("app", "website")
VALID_PLATFORMS = ("android", "windows", "ios", "mac")
CODE_REDEEM_PLATFORMS = ("android", "windows")  # 网站购买后走"发激活码"的平台
UNSUPPORTED_PLATFORMS = ("ios", "mac")          # 网站购买暂不支持的平台


# ================= 建表（含旧库补列） =================
def init_payment_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS payment_orders (
            order_id TEXT PRIMARY KEY,
            device_id TEXT,
            product_id INTEGER,
            amount REAL,
            days INTEGER,
            status TEXT DEFAULT 'PENDING',
            payment_method TEXT,
            trade_no TEXT,
            source TEXT DEFAULT 'app',
            platform TEXT,
            activation_code TEXT,
            created_time DATETIME,
            paid_time DATETIME
        )''')

        # 兼容旧库：补列
        cursor.execute("PRAGMA table_info(payment_orders)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        for col_name, col_type in [
            ("source", "TEXT DEFAULT 'app'"),
            ("platform", "TEXT"),
            ("activation_code", "TEXT"),
        ]:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE payment_orders ADD COLUMN {col_name} {col_type}")

        conn.commit()


# ================= 请求体模型 =================
class CreatePaymentRequest(BaseModel):
    product_id: int
    payment_method: str = "alipay"     # alipay / wechat
    source: str = "app"                # "app"（客户端内购买）/ "website"（网站购买）
    device_id: Optional[str] = None    # source="app" 时必填，用于支付成功后直接加时长
    platform: Optional[str] = None     # source="website" 时必填: android / windows / ios / mac


# ================= 工具函数 =================
def _generate_sign(data: dict, app_secret: str) -> str:
    filtered = {k: str(v) for k, v in data.items() if v != "" and v is not None and k != "hash"}
    sorted_keys = sorted(filtered.keys())
    sign_str = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)
    sign_str += app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def _generate_activation_code(cursor) -> str:
    """生成一个不重复的 10 位激活码，写入 activation_codes 表（沿用 main.py 里的表结构）"""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        cursor.execute("SELECT 1 FROM activation_codes WHERE code = ?", (code,))
        if not cursor.fetchone():
            return code


async def _create_hupijiao_order(order_id: str, amount: float, pay_type: str) -> str:
    notify_url = f"{NOTIFY_BASE_URL}/api/v1/payment/hupijiao/webhook"

    order_data = {
        "version": "1.1",
        "appid": HUPIJIAO_APP_ID,
        "trade_order_id": order_id,
        "total_fee": str(amount),
        "title": "日用百货",
        "time": str(int(time.time())),
        "notify_url": notify_url,
        "return_url": RETURN_URL,
        "callback_url": RETURN_URL,
        "plugins": "fastapi",
        "type": pay_type,  # alipay / wechat
    }
    order_data["hash"] = _generate_sign(order_data, HUPIJIAO_APP_SECRET)

    async with httpx.AsyncClient() as client:
        response = await client.post(HUPIJIAO_API_URL, data=order_data)
        result = response.json()
        if result.get("errcode") == 0:
            return result.get("url")
        raise Exception(f"虎皮椒下单失败: {result.get('errmsg')}")


# ================= 接口 =================
@router.get("/api/v1/payment/products")
async def get_payment_products():
    """客户端/网站拉取充值套餐列表"""
    return {"code": 200, "data": [
        {"product_id": pid, **info} for pid, info in RECHARGE_PRODUCTS.items()
    ]}


@router.post("/api/v1/payment/create")
async def create_payment(req: CreatePaymentRequest):
    """创建订单并返回虎皮椒支付链接（网站那边一般直接用这个 url 生成二维码）"""
    product = RECHARGE_PRODUCTS.get(req.product_id)
    if not product:
        return {"code": 400, "msg": "无效的充值档位"}

    if req.payment_method not in ("alipay", "wechat"):
        return {"code": 400, "msg": "不支持的支付方式"}

    if req.source not in VALID_SOURCES:
        return {"code": 400, "msg": "无效的购买来源 source"}

    # ---- app 内购买：必须带 device_id，用于支付成功后直接加时长 ----
    if req.source == "app":
        if not req.device_id:
            return {"code": 400, "msg": "App 内购买必须提供 device_id"}
        platform = None

    # ---- 网站购买：必须带 platform，按平台决定是否支持 ----
    else:
        if req.platform not in VALID_PLATFORMS:
            return {"code": 400, "msg": "网站购买必须提供有效的 platform (android/windows/ios/mac)"}
        if req.platform in UNSUPPORTED_PLATFORMS:
            return {"code": 400, "msg": f"{req.platform} 平台的网站购买暂未开放，敬请期待"}
        platform = req.platform

    order_id = str(uuid.uuid4())
    now = datetime.now()

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO payment_orders
               (order_id, device_id, product_id, amount, days, status, payment_method,
                source, platform, created_time)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)''',
            (order_id, req.device_id, req.product_id, product["price"], product["days"],
             req.payment_method, req.source, platform, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()

    try:
        pay_url = await _create_hupijiao_order(order_id, product["price"], req.payment_method)
    except Exception as e:
        print(f"❌ 虎皮椒下单失败: {e}")
        return {"code": 500, "msg": "支付渠道暂时不可用"}

    return {"code": 200, "data": {
        "order_id": order_id,
        "pay_url": pay_url,
        "amount": product["price"],
        "days": product["days"],
        "source": req.source,
        "platform": platform
    }}


@router.get("/api/v1/payment/status/{order_id}")
async def check_payment_status(order_id: str):
    """
    轮询订单状态。
    - app 购买：paid 后设备时长已直接加好，data 里不会有 activation_code。
    - 网站购买(android/windows)：paid 后 activation_code 会有值，网站拿这个码生成/展示给用户。
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, activation_code, source, platform FROM payment_orders WHERE order_id = ?",
            (order_id,)
        )
        row = cursor.fetchone()

    if not row:
        return {"code": 404, "msg": "订单不存在"}

    status, activation_code, source, platform = row
    return {"code": 200, "data": {
        "status": "paid" if status == "SUCCESS" else "pending",
        "activation_code": activation_code,
        "source": source,
        "platform": platform
    }}


@router.post("/api/v1/payment/hupijiao/webhook")
async def hupijiao_webhook(request: Request):
    """
    虎皮椒异步回调通知。
    必须返回纯文本 "success"，否则虎皮椒会持续重试。
    """
    try:
        form = await request.form()
        data = dict(form)
    except Exception as e:
        print(f"❌ 解析 Form 数据失败: {e}")
        return PlainTextResponse("fail")

    print(f"🔥 [虎皮椒 Webhook] 收到回调: {data}")

    received_hash = data.get("hash")
    if not received_hash:
        return PlainTextResponse("fail")

    expected_hash = _generate_sign(data, HUPIJIAO_APP_SECRET)
    if expected_hash != received_hash:
        print(f"❌ [虎皮椒] 签名验证失败，收到: {received_hash} 期待: {expected_hash}")
        return PlainTextResponse("fail")

    if data.get("status") != "OD":
        # 非成功状态，直接告知已收到，避免虎皮椒无意义重试
        return PlainTextResponse("success")

    order_id = data.get("trade_order_id")
    trade_no = data.get("trade_order_id")
    if not order_id:
        return PlainTextResponse("fail")

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT device_id, days, status, source, platform FROM payment_orders WHERE order_id = ?",
            (order_id,)
        )
        order = cursor.fetchone()

        if not order:
            print(f"❌ 订单不存在: {order_id}")
            return PlainTextResponse("fail")

        device_id, days, status, source, platform = order

        if status == "SUCCESS":
            # 已经处理过，直接返回 success，防止重复加时长/重复发码
            print(f"⚠️ 订单已处理过，跳过 (ID: {order_id})")
            return PlainTextResponse("success")

        cursor.execute(
            "UPDATE payment_orders SET status='SUCCESS', trade_no=?, paid_time=? WHERE order_id=?",
            (trade_no, now_str, order_id)
        )

        # ---- 分流：app 直接加时长 / 网站(android,windows) 发激活码 ----
        if source == "app":
            cursor.execute("SELECT expire_time FROM users WHERE device_id=?", (device_id,))
            user_row = cursor.fetchone()

            if user_row:
                current_expire = datetime.strptime(user_row[0], "%Y-%m-%d %H:%M:%S")
                base = current_expire if current_expire > now else now
                new_expire = base + timedelta(days=days)
                cursor.execute(
                    "UPDATE users SET expire_time=? WHERE device_id=?",
                    (new_expire.strftime("%Y-%m-%d %H:%M:%S"), device_id)
                )
            else:
                # 理论上下单前设备已经调用过 get_node 建过记录，这里做个兜底
                new_expire = now + timedelta(days=days)
                cursor.execute(
                    "INSERT INTO users (device_id, expire_time) VALUES (?, ?)",
                    (device_id, new_expire.strftime("%Y-%m-%d %H:%M:%S"))
                )
            print(f"✅ [App购买] 充值到账: 设备 {device_id} +{days}天, 新到期时间 {new_expire}")

        elif source == "website" and platform in CODE_REDEEM_PLATFORMS:
            code = _generate_activation_code(cursor)
            cursor.execute(
                "INSERT INTO activation_codes (code, days) VALUES (?, ?)",
                (code, days)
            )
            cursor.execute(
                "UPDATE payment_orders SET activation_code=? WHERE order_id=?",
                (code, order_id)
            )
            print(f"✅ [网站购买-{platform}] 已生成激活码 {code}，天数 {days}，等待用户在 App 内兑换")

        else:
            # 理论上 create_payment 阶段已经拦掉了不支持的平台，这里只是兜底日志
            print(f"⚠️ 订单 {order_id} 的 source/platform 组合未处理: {source}/{platform}")

        conn.commit()

    return PlainTextResponse("success")