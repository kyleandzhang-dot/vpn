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
   → 不需要 UID；支付成功后生成 10 位激活码，用户在客户端内兑换。

2. platform = ios / mac
   → 不要求 UID；支付成功后创建独立订阅，订单状态接口返回订阅链接与二维码。
========================================================
"""

import time
import uuid
import secrets
import string
import hashlib
import re
import sqlite3
import httpx
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from email_utils import send_admin_payment_notification_email, send_payment_delivery_email
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
CODE_REDEEM_PLATFORMS = ("android", "windows")       # 支付成功后返回客户端可兑换的激活码
SUBSCRIPTION_PLATFORMS = ("ios", "mac")              # 支付成功后返回 V2Box 订阅链接/二维码
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
            receiver_email TEXT,
            email_status TEXT DEFAULT 'PENDING',
            email_sent_time DATETIME,
            admin_email_status TEXT DEFAULT 'PENDING',
            admin_email_sent_time DATETIME,
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
            ("receiver_email", "TEXT"),
            ("email_status", "TEXT DEFAULT 'PENDING'"),
            ("email_sent_time", "DATETIME"),
            ("admin_email_status", "TEXT DEFAULT 'PENDING'"),
            ("admin_email_sent_time", "DATETIME"),
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
    email: str | None = Field(default=None, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_email(value)


class ResendDeliveryRequest(BaseModel):
    order_id: str = Field(..., min_length=8, max_length=80)
    email: str = Field(..., min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return _normalize_email(value)


# ================= 工具函数 =================
def _generate_sign(data: dict, app_secret: str) -> str:
    filtered = {k: str(v) for k, v in data.items() if v != "" and v is not None and k != "hash"}
    sorted_keys = sorted(filtered.keys())
    sign_str = "&".join(f"{k}={filtered[k]}" for k in sorted_keys)
    sign_str += app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def _normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", email, re.IGNORECASE):
        raise ValueError("请输入有效的收货邮箱")
    return email


def _mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 1 else local[:1]
    return f"{visible}***@{domain}"


def _generate_activation_code(cursor, length: int = 10) -> str:
    """生成一个未使用且不重复的激活码。"""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(length))
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


async def _send_order_delivery_email(
    order_id: str,
    *,
    expected_email: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """读取已完成订单并发送凭证。邮件失败只记录状态，不回滚已完成的支付发货。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT product_id, amount, days, status, platform, activation_code,
                      receiver_email, email_status, email_sent_time
               FROM payment_orders WHERE order_id = ?""",
            (order_id,),
        )
        row = cursor.fetchone()

    if not row:
        return False, "订单不存在"

    product_id, amount, days, status, platform, credential, receiver_email, email_status, sent_time = row
    if expected_email and _normalize_email(expected_email) != str(receiver_email or "").lower():
        return False, "订单号或邮箱不匹配"
    if status != "SUCCESS" or not credential:
        return False, "订单尚未完成发货"
    if not receiver_email:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "UPDATE payment_orders SET email_status='SKIPPED' WHERE order_id=?",
                (order_id,),
            )
            conn.commit()
        return True, "买家未填写邮箱，跳过凭证邮件"
    if email_status == "SENT" and not force:
        return True, "邮件此前已发送"

    if force and email_status == "SENT" and sent_time:
        try:
            elapsed = datetime.now() - datetime.strptime(sent_time, "%Y-%m-%d %H:%M:%S")
            if elapsed.total_seconds() < 60:
                return False, "邮件刚刚发送过，请一分钟后再试"
        except (TypeError, ValueError):
            pass

    product = RECHARGE_PRODUCTS.get(product_id, {})
    ok, message = await send_payment_delivery_email(
        to_email=receiver_email,
        order_id=order_id,
        product_name=product.get("name", "专线时长"),
        amount=amount,
        days=days,
        platform=platform,
        credential=credential,
    )

    with sqlite3.connect(DB_FILE) as conn:
        if ok:
            conn.execute(
                "UPDATE payment_orders SET email_status='SENT', email_sent_time=? WHERE order_id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
            )
        else:
            conn.execute(
                "UPDATE payment_orders SET email_status='FAILED' WHERE order_id=?",
                (order_id,),
            )
        conn.commit()

    return ok, message


async def _send_admin_payment_notification(order_id: str) -> tuple[bool, str]:
    """每笔成功支付仅发送一次管理员通知；买家补发凭证不会调用这里。"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT product_id, amount, days, status, platform, receiver_email,
                      paid_time, admin_email_status
               FROM payment_orders WHERE order_id = ?""",
            (order_id,),
        )
        row = cursor.fetchone()

    if not row:
        return False, "订单不存在"

    product_id, amount, days, status, platform, receiver_email, paid_time, admin_status = row
    if status != "SUCCESS":
        return False, "订单尚未支付成功"
    if admin_status == "SENT":
        return True, "管理员通知此前已发送"

    product = RECHARGE_PRODUCTS.get(product_id, {})
    ok, message = await send_admin_payment_notification_email(
        order_id=order_id,
        product_name=product.get("name", "专线时长"),
        amount=amount,
        days=days,
        platform=platform,
        buyer_email=receiver_email,
        paid_time=paid_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    with sqlite3.connect(DB_FILE) as conn:
        if ok:
            conn.execute(
                """UPDATE payment_orders
                   SET admin_email_status='SENT', admin_email_sent_time=?
                   WHERE order_id=?""",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id),
            )
        else:
            conn.execute(
                "UPDATE payment_orders SET admin_email_status='FAILED' WHERE order_id=?",
                (order_id,),
            )
        conn.commit()

    return ok, message


# ================= 测试接口（模拟网站下单发货真实逻辑） =================
class TestXuiRequest(BaseModel):
    days: int = 30           # 只需要填天数，默认 30 天
    platform: str = "ios"    # 只需要填系统: ios / mac / android / windows


@router.post("/api/admin/test_xui_subscription", summary="模拟网站下单后的发货测试")
async def test_xui_subscription(req: TestXuiRequest):
    """
    模拟网站购买支付成功后的正式分发流程：
    不需要手填订单号，系统后台自动生成模拟订单ID。
    - 平台为 ios / mac：直接调用 3x-ui 建订阅/续期，返回订阅链接和二维码 base64
    - 平台为 android / windows：生成客户端可兑换的激活码
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

    # 分支 2: android / windows 生成激活码
    elif platform in CODE_REDEEM_PLATFORMS:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            code = _generate_activation_code(cursor)
            cursor.execute(
                "INSERT INTO activation_codes (code, days) VALUES (?, ?)",
                (code, req.days)
            )
            conn.commit()

        return {
            "code": 200, 
            "msg": "模拟网站购买 Android/Windows 激活码生成成功", 
            "data": {
                "platform": platform,
                "days": req.days,
                "activation_code": code,
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
    """创建订单：Android/Windows 返回激活码，iOS/Mac 返回订阅二维码。"""
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

    # Android / Windows：支付成功后生成激活码，不绑定任何 UID 或设备。
    if platform in CODE_REDEEM_PLATFORMS:
        device_id = None

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
                source, platform, receiver_email, email_status, admin_email_status, created_time)
               VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, 'PENDING', ?)''',
            (order_id, device_id, req.product_id, product["price"], product["days"],
             payment_method, source, platform, req.email,
             "PENDING" if req.email else "SKIPPED",
             now.strftime("%Y-%m-%d %H:%M:%S"))
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
        "email_masked": _mask_email(req.email),
        "delivery_type": "qr_subscription" if platform in SUBSCRIPTION_PLATFORMS else "activation_code"
    }}


@router.get("/api/v1/payment/status/{order_id}")
async def check_payment_status(order_id: str):
    """
    轮询订单状态。
    Android/Windows 支付成功后返回激活码；iOS/Mac 返回订阅链接和二维码。
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT status, activation_code, source, platform, receiver_email, email_status
               FROM payment_orders WHERE order_id = ?""",
            (order_id,)
        )
        row = cursor.fetchone()

    if not row:
        return {"code": 404, "msg": "订单不存在"}

    status, fulfillment_value, source, platform, receiver_email, email_status = row
    is_qr_delivery = platform in SUBSCRIPTION_PLATFORMS

    qr_base64 = None
    if is_qr_delivery and fulfillment_value:
        try:
            qr_base64 = make_qrcode_base64(fulfillment_value)
        except Exception as e:
            print(f"⚠️ 生成二维码失败: {e}")

    return {"code": 200, "data": {
        "status": "paid" if status == "SUCCESS" else "pending",
        "activation_code": fulfillment_value if not is_qr_delivery else None,
        "sub_link": fulfillment_value if is_qr_delivery else None,
        "qr_base64": qr_base64,
        "source": source,
        "platform": platform,
        "email_masked": _mask_email(receiver_email),
        "email_status": (email_status or "PENDING").lower(),
        "delivery_type": "qr_subscription" if is_qr_delivery else "activation_code"
    }}


@router.post("/api/v1/payment/resend")
async def resend_payment_delivery(req: ResendDeliveryRequest):
    """用户凭订单号和原收货邮箱重新发送凭证，接口不直接泄露凭证内容。"""
    ok, message = await _send_order_delivery_email(
        req.order_id,
        expected_email=req.email,
        force=True,
    )
    if not ok:
        return {"code": 400, "msg": message}
    return {"code": 200, "msg": "凭证邮件已重新发送，请检查收件箱和垃圾邮件"}


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
        # 串行处理同一时间到达的重复回调，避免并发生成两张激活码。
        cursor.execute("BEGIN IMMEDIATE")
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
            # 凭证已生成，不重复发货；若此前邮件失败，仍允许在事务结束后补发。
            print(f"⚠️ 订单已处理过，跳过重复发货 (ID: {order_id})")
            conn.commit()
        else:
            is_qr_delivery = platform in SUBSCRIPTION_PLATFORMS

            if platform in CODE_REDEEM_PLATFORMS:
                activation_code = _generate_activation_code(cursor)
                cursor.execute(
                    "INSERT INTO activation_codes (code, days) VALUES (?, ?)",
                    (activation_code, days)
                )
                cursor.execute(
                    "UPDATE payment_orders SET activation_code=? WHERE order_id=?",
                    (activation_code, order_id)
                )
                print(f"✅ [激活码发货] 订单 {order_id} 生成 {days} 天激活码")

            elif is_qr_delivery and device_id:
                # iOS / Mac 不绑定 UID：直接用订单身份生成一份新订阅。
                new_expire = now + timedelta(days=days)
                print(f"✅ [苹果订阅] 订单 {order_id} +{days}天, 到期时间 {new_expire}")
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
                    print(f"⚠️ [x3-ui] 苹果订阅生成失败，需要人工核对订单: {order_id} - {e}")
                    # 苹果订单必须拿到二维码才算发放成功。返回 fail 让支付渠道重试回调。
                    conn.rollback()
                    return PlainTextResponse("fail")

            else:
                print(f"⚠️ 订单 {order_id} 无法确定发货方式: platform={platform}, source={source}")
                conn.rollback()
                return PlainTextResponse("fail")

            cursor.execute(
                "UPDATE payment_orders SET status='SUCCESS', trade_no=?, paid_time=? WHERE order_id=?",
                (trade_no, now_str, order_id)
            )
            conn.commit()

    buyer_email_ok, buyer_email_message = await _send_order_delivery_email(order_id)
    admin_email_ok, admin_email_message = await _send_admin_payment_notification(order_id)
    if not buyer_email_ok:
        print(f"⚠️ 订单 {order_id} 买家凭证邮件发送失败: {buyer_email_message}")
    if not admin_email_ok:
        print(f"⚠️ 订单 {order_id} 管理员收款通知发送失败: {admin_email_message}")
    if not buyer_email_ok or not admin_email_ok:
        # 凭证已安全落库且重复回调不会重复发货；返回 fail 让支付渠道稍后重试邮件通知。
        return PlainTextResponse("fail")

    return PlainTextResponse("success")
