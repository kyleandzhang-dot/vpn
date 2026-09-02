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
发放策略（网站购买按平台分流）：
========================================================
1. platform = android / windows
   → 必须输入 UID，下单时用 UID 反查 device_id；支付成功后直接给该账号加时长。

2. platform = ios / mac
   → 不要求 UID；支付成功后创建独立订阅，订单状态接口返回订阅链接与二维码。
========================================================
"""

import time
import uuid
import hashlib
import sqlite3
import httpx
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from xui_client import create_or_renew_subscription, make_qrcode_base64

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
    1: {"name": "月卡",  "price": 29,  "days": 30},
    2: {"name": "年卡",  "price": 99, "days": 365},
    3: {"name": "永久",  "price": 198, "days": 3650},
}

VALID_SOURCES = ("app", "website")
VALID_PLATFORMS = ("android", "windows", "ios", "mac")
UID_RECHARGE_PLATFORMS = ("android", "windows")       # 网站购买后按 UID 直接给账号续期
SUBSCRIPTION_PLATFORMS = ("ios", "mac")               # 网站购买后返回订阅链接/二维码
# 注意：iOS 走这条路子相当于绕开了 Apple IAP，属于灰色地带，苹果审核/政策层面有下架风险，
# 自己权衡；这里只是按你的要求实现技术方案，不代表这是合规推荐做法。


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
    source: str = "app"                # 只记录购买入口，不再决定发放方式
    platform: str                      # 必填：android / windows / ios / mac
    uid: Optional[str] = None          # android/windows 必填；ios/mac 不需要


# ================= 工具函数 =================
def _generate_sign(data: dict, app_secret: str) -> str:
    filtered = {k: str(v) for k, v in data.items() if v != "" and v is not None and k != "hash"}
    sorted_keys = sorted(filtered.keys())
    sign_str = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)
    sign_str += app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


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


# ================= 测试接口（模拟网站下单发货真实逻辑） =================
class TestXuiRequest(BaseModel):
    days: int = 30           # 只需要填天数，默认 30 天
    platform: str = "ios"    # 只需要填系统: ios / mac / android / windows
    uid: Optional[str] = None


@router.post("/api/admin/test_xui_subscription", summary="模拟网站下单后的发货测试")
async def test_xui_subscription(req: TestXuiRequest):
    """
    模拟网站购买支付成功后的正式分发流程：
    不需要手填订单号，系统后台自动生成模拟订单ID。
    - 平台为 ios / mac：直接调用 3x-ui 建订阅/续期，返回订阅链接和二维码 base64
    - 平台为 android / windows：按 UID 给已有账号直接续期，不返回二维码
    """
    platform = req.platform.lower().strip()
    if platform not in VALID_PLATFORMS:
        return {
            "code": 400, 
            "msg": f"不支持的平台 [{req.platform}]，仅支持: {', '.join(VALID_PLATFORMS)}"
        }

    now = datetime.now()
    # 后台自动派生一个测试用的模拟订单号，无需手动填写
    mock_order_id = f"test_{uuid.uuid4().hex[:12]}"

    # 分支 1: ios / mac 走订阅链接与二维码流程
    if platform in SUBSCRIPTION_PLATFORMS:
        identity = f"web_{mock_order_id[:12]}"
        new_expire = now + timedelta(days=req.days)
        expiry_ms = int(new_expire.timestamp() * 1000)
        try:
            xui_result = await create_or_renew_subscription(device_id=identity, expiry_ms=expiry_ms)
            sub_link = xui_result.get("sub_link")
            qr_base64 = None
            if sub_link:
                try:
                    qr_base64 = make_qrcode_base64(sub_link)
                except Exception as e:
                    print(f"生成二维码图片失败: {e}")

            return {
                "code": 200, 
                "msg": "模拟网站购买 iOS/Mac 订阅分发成功", 
                "data": {
                    "platform": platform,
                    "days": req.days,
                    "sub_link": sub_link,
                    "qr_base64": qr_base64,
                    "identity_used": identity
                }
            }
        except Exception as e:
            return {"code": 500, "msg": f"调用 3x-ui 生成订阅失败: {e}"}

    # 分支 2: android / windows 按 UID 直接给账号续期
    elif platform in UID_RECHARGE_PLATFORMS:
        clean_uid = (req.uid or "").strip()
        if not clean_uid:
            return {"code": 400, "msg": "Android/Windows 测试必须提供 UID"}

        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT device_id, expire_time FROM users WHERE uid = ?", (clean_uid,))
            user_row = cursor.fetchone()
            if not user_row:
                return {"code": 404, "msg": "UID不存在"}

            device_id, expire_time_str = user_row
            current_expire = datetime.strptime(expire_time_str, "%Y-%m-%d %H:%M:%S")
            base = current_expire if current_expire > now else now
            new_expire = base + timedelta(days=req.days)
            cursor.execute(
                "UPDATE users SET expire_time = ? WHERE device_id = ?",
                (new_expire.strftime("%Y-%m-%d %H:%M:%S"), device_id)
            )
            conn.commit()

        return {
            "code": 200, 
            "msg": "模拟网站购买 Android/Windows UID 充值成功", 
            "data": {
                "platform": platform,
                "days": req.days,
                "uid": clean_uid,
                "new_expire_time": new_expire.strftime("%Y-%m-%d %H:%M:%S"),
                "qr_base64": None
            }
        }


# ================= 接口 =================
@router.get("/api/v1/payment/products")
async def get_payment_products():
    """客户端/网站拉取充值套餐列表"""
    return {"code": 200, "data": [
        {"product_id": pid, **info} for pid, info in RECHARGE_PRODUCTS.items()
    ]}


@router.post("/api/v1/payment/create")
async def create_payment(req: CreatePaymentRequest):
    """创建订单：Android/Windows 按 UID 充值，iOS/Mac 支付后返回订阅二维码。"""
    product = RECHARGE_PRODUCTS.get(req.product_id)
    if not product:
        return {"code": 400, "msg": "无效的充值档位"}

    payment_method = req.payment_method.lower().strip()
    if payment_method not in ("alipay", "wechat"):
        return {"code": 400, "msg": "不支持的支付方式"}

    source = req.source.lower().strip()
    if source not in VALID_SOURCES:
        return {"code": 400, "msg": "无效的购买来源 source"}

    platform = req.platform.lower().strip()
    if platform not in VALID_PLATFORMS:
        return {
            "code": 400,
            "msg": f"不支持的平台 [{req.platform}]，仅支持: {', '.join(VALID_PLATFORMS)}"
        }

    order_id = str(uuid.uuid4())

    # Android / Windows：统一要求 UID，并在下单时绑定到真实账号。
    if platform in UID_RECHARGE_PLATFORMS:
        clean_uid = (req.uid or "").strip()
        if not clean_uid:
            return {"code": 400, "msg": "Android/Windows 充值请输入您的UID"}

        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT device_id FROM users WHERE uid = ?", (clean_uid,))
            user_row = cursor.fetchone()

        if not user_row:
            return {"code": 404, "msg": "UID不存在，请先打开客户端联网后再充值"}
        device_id = user_row[0]

    # iOS / Mac：不绑定 UID，使用订单派生的独立身份生成订阅二维码。
    elif platform in SUBSCRIPTION_PLATFORMS:
        device_id = f"web_{order_id.replace('-', '')[:16]}"

    else:
        return {"code": 400, "msg": "无法确定发放方式"}

    now = datetime.now()

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO payment_orders
               (order_id, device_id, product_id, amount, days, status, payment_method,
                source, platform, created_time)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)''',
            (order_id, device_id, req.product_id, product["price"], product["days"],
             payment_method, source, platform, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()

    try:
        pay_url = await _create_hupijiao_order(order_id, product["price"], payment_method)
    except Exception as e:
        print(f"❌ 虎皮椒下单失败: {e}")
        return {"code": 500, "msg": "支付渠道暂时不可用"}

    return {"code": 200, "data": {
        "order_id": order_id,
        "pay_url": pay_url,
        "amount": product["price"],
        "days": product["days"],
        "source": source,
        "platform": platform,
        "delivery_type": "qr_subscription" if platform in SUBSCRIPTION_PLATFORMS else "uid_recharge"
    }}


@router.get("/api/v1/payment/status/{order_id}")
async def check_payment_status(order_id: str):
    """
    轮询订单状态。
    Android/Windows 支付成功只确认 UID 已到账；iOS/Mac 额外返回订阅链接和二维码。
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

    status, subscription_link, source, platform = row
    is_qr_delivery = platform in SUBSCRIPTION_PLATFORMS

    qr_base64 = None
    if is_qr_delivery and subscription_link:
        try:
            qr_base64 = make_qrcode_base64(subscription_link)
        except Exception as e:
            print(f"⚠️ 生成二维码失败: {e}")

    return {"code": 200, "data": {
        "status": "paid" if status == "SUCCESS" else "pending",
        "activation_code": subscription_link if is_qr_delivery else None,
        "sub_link": subscription_link if is_qr_delivery else None,
        "qr_base64": qr_base64,
        "source": source,
        "platform": platform,
        "delivery_type": "qr_subscription" if is_qr_delivery else "uid_recharge"
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
    trade_no = data.get("transaction_id") or data.get("open_order_id") or order_id
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

        is_qr_delivery = platform in SUBSCRIPTION_PLATFORMS

        if device_id:
            if is_qr_delivery:
                # iOS / Mac 不绑定 UID：直接用订单身份生成一份新订阅。
                new_expire = now + timedelta(days=days)
                print(f"✅ [苹果订阅] 订单 {order_id} +{days}天, 到期时间 {new_expire}")
            else:
                # Android / Windows 已在下单时通过 UID 绑定到这个 device_id。
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
                elif platform in UID_RECHARGE_PLATFORMS:
                    # 正常情况下不会发生：下单阶段已经验证过 UID 对应的账号。
                    print(f"⚠️ UID 充值订单 {order_id} 对应账号已不存在: {device_id}")
                    conn.rollback()
                    return PlainTextResponse("fail")
                print(f"✅ [UID充值] 设备 {device_id} +{days}天, 新到期时间 {new_expire}")

            # 两类订单都同步 3x-ui；只有 iOS/Mac 的状态接口会把链接和二维码返回给前端。
            try:
                expiry_ms = int(new_expire.timestamp() * 1000)
                xui_result = await create_or_renew_subscription(device_id=device_id, expiry_ms=expiry_ms)
                subscription_link = xui_result.get("sub_link")
                if not xui_result.get("success") or not subscription_link:
                    raise RuntimeError(xui_result.get("msg") or "未生成订阅链接")
                cursor.execute(
                    "UPDATE payment_orders SET activation_code=? WHERE order_id=?",
                    (subscription_link, order_id)
                )
                print(f"✅ [x3-ui] 订阅已生成/续期: {subscription_link}")
            except Exception as e:
                if is_qr_delivery:
                    print(f"⚠️ [x3-ui] 苹果订阅生成失败，需要人工核对订单: {order_id} - {e}")
                    # 苹果订单必须拿到二维码才算发放成功。返回 fail 让支付渠道重试回调。
                    conn.rollback()
                    return PlainTextResponse("fail")
                else:
                    # 账号时长已经到账，节点同步失败不回滚充值，只记日志排查。
                    print(f"⚠️ [x3-ui] 节点同步失败，但 UID 账号已到账: {device_id} - {e}")

        else:
            # 理论上 create_payment 阶段已经拦掉了缺 device_id 的情况。
            print(f"⚠️ 订单 {order_id} 缺少 device_id，无法处理: source={source}")
            conn.rollback()
            return PlainTextResponse("fail")

        cursor.execute(
            "UPDATE payment_orders SET status='SUCCESS', trade_no=?, paid_time=? WHERE order_id=?",
            (trade_no, now_str, order_id)
        )

        conn.commit()

    return PlainTextResponse("success")
