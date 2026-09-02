import html
import base64
import io
import os
from urllib.parse import urlencode

import httpx

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False


load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
_RESEND_FROM_RAW = os.getenv("RESEND_FROM", "onboarding@resend.dev").strip()
if "<" in _RESEND_FROM_RAW and ">" in _RESEND_FROM_RAW:
    _sender_address = _RESEND_FROM_RAW.split("<", 1)[1].split(">", 1)[0].strip()
else:
    _sender_address = _RESEND_FROM_RAW
RESEND_FROM = f"喵脸 <{_sender_address}>"
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "https://miaolian.jmsht.one").rstrip("/")
PAYMENT_NOTIFY_EMAIL = os.getenv("PAYMENT_NOTIFY_EMAIL", "1728578441@qq.com").strip().lower()


async def send_email(
    to_email: str,
    subject: str,
    email_html: str,
    attachments: list[dict] | None = None,
) -> tuple[bool, str]:
    """通过 Resend 发信。失败时返回错误，不中断支付发货流程。"""
    recipient = str(to_email or "").strip().lower()
    if not recipient:
        return False, "收件邮箱为空"
    if not RESEND_API_KEY:
        print(f"[email_utils] RESEND_API_KEY 未配置，邮件未发送: {subject}")
        return False, "RESEND_API_KEY 未配置"

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            payload = {
                "from": RESEND_FROM,
                "to": [recipient],
                "subject": subject,
                "html": email_html,
            }
            if attachments:
                payload["attachments"] = attachments

            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            print(f"[email_utils] Resend 发送失败，HTTP {response.status_code}")
            return False, f"Resend HTTP {response.status_code}"
        return True, "邮件已发送"
    except Exception as exc:
        print(f"[email_utils] 邮件发送异常: {type(exc).__name__}")
        return False, type(exc).__name__


def make_credential_qr_base64(credential: str) -> str:
    """生成用于邮件内嵌和附件下载的 PNG 二维码 Base64。"""
    import qrcode

    qr = qrcode.QRCode(version=None, box_size=8, border=4)
    qr.add_data(str(credential))
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_payment_delivery_email_html(
    *,
    order_id: str,
    product_name: str,
    amount: float,
    days: int,
    platform: str,
    credential: str,
) -> str:
    platform_key = str(platform or "").lower()
    platform_label = {
        "android": "Android 安卓",
        "windows": "Windows",
        "ios": "iOS 苹果",
        "mac": "macOS 苹果",
    }.get(platform_key, platform_key or "未知平台")
    is_subscription = platform_key in ("ios", "mac")
    credential_label = "订阅链接" if is_subscription else "激活码"
    qr_help = "使用另一台设备打开邮件后，可用 V2Box 扫码导入" if is_subscription else "二维码作为凭证备份；客户端不支持扫码时，请复制上方激活码兑换"
    tutorial_platform = "ios" if platform_key == "mac" else platform_key
    tutorial_url = f"{PUBLIC_SITE_URL}/?{urlencode({'tutorial': tutorial_platform})}"

    safe_order_id = html.escape(str(order_id))
    safe_product_name = html.escape(str(product_name))
    safe_platform = html.escape(platform_label)
    safe_credential = html.escape(str(credential))
    safe_tutorial_url = html.escape(tutorial_url, quote=True)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',Arial,sans-serif;max-width:560px;margin:0 auto;padding:36px 20px;background:#f4f4f5;color:#18181b">
      <div style="text-align:center;margin-bottom:22px">
        <h1 style="font-size:25px;margin:0 0 8px">支付成功，凭证已送达</h1>
        <p style="font-size:13px;color:#71717a;margin:0">请妥善保存本邮件，避免关闭支付页面后凭证丢失</p>
      </div>
      <div style="background:#fff;border:1px solid #e4e4e7;border-radius:20px;padding:26px">
        <p style="font-size:14px;margin:0 0 6px"><strong>订单号：</strong>{safe_order_id}</p>
        <p style="font-size:14px;margin:0 0 6px"><strong>套餐：</strong>{safe_product_name}（{int(days)} 天）</p>
        <p style="font-size:14px;margin:0 0 6px"><strong>金额：</strong>¥{float(amount):.2f}</p>
        <p style="font-size:14px;margin:0 0 18px"><strong>使用平台：</strong>{safe_platform}</p>

        <div style="background:#fafafa;border:1px dashed #a1a1aa;border-radius:14px;padding:18px">
          <div style="font-size:12px;color:#71717a;margin-bottom:8px">您的{credential_label}</div>
          <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:{'12px' if is_subscription else '21px'};font-weight:700;line-height:1.7;word-break:break-all;letter-spacing:{'0' if is_subscription else '.08em'}">{safe_credential}</div>
        </div>

        <div style="margin-top:18px;text-align:center">
          <div style="font-size:12px;color:#71717a;margin-bottom:10px">凭证二维码</div>
          <img src="cid:credential-qr" alt="喵脸使用凭证二维码" width="220" height="220" style="display:block;width:220px;height:220px;margin:0 auto;border:1px solid #e4e4e7;border-radius:14px;padding:8px;background:#fff">
          <p style="font-size:11px;line-height:1.6;color:#71717a;margin:9px 0 0">{qr_help}</p>
        </div>

        <a href="{safe_tutorial_url}" style="display:block;margin-top:18px;padding:13px 16px;border-radius:12px;background:#18181b;color:#fff;text-align:center;text-decoration:none;font-size:14px;font-weight:700">查看 {safe_platform} 使用教程</a>
        <p style="font-size:12px;line-height:1.7;color:#71717a;margin:16px 0 0">如果凭证无法使用，请将本邮件中的订单号提供给客服核对。请勿把激活码或订阅链接转发给他人。</p>
      </div>
      <p style="text-align:center;color:#a1a1aa;font-size:11px;margin-top:18px">喵脸 · 系统自动发货邮件，请勿直接回复</p>
    </div>
    """


async def send_payment_delivery_email(**order_data) -> tuple[bool, str]:
    payload = dict(order_data)
    to_email = payload.pop("to_email")
    product_name = str(payload.get("product_name") or "专线时长")
    subject = f"【喵脸】{product_name}支付成功｜您的使用凭证"
    email_html = build_payment_delivery_email_html(**payload)
    try:
        qr_base64 = make_credential_qr_base64(str(payload["credential"]))
    except Exception as exc:
        print(f"[email_utils] 凭证二维码生成失败: {type(exc).__name__}")
        return False, f"二维码生成失败: {type(exc).__name__}"

    return await send_email(
        to_email,
        subject,
        email_html,
        attachments=[{
            "content": qr_base64,
            "filename": "miaolian-credential-qr.png",
            "content_id": "credential-qr",
        }],
    )


def build_admin_payment_notification_email_html(
    *,
    order_id: str,
    product_name: str,
    amount: float,
    days: int,
    platform: str,
    buyer_email: str | None,
    paid_time: str,
) -> str:
    platform_label = {
        "android": "Android 安卓",
        "windows": "Windows",
        "ios": "iOS 苹果",
        "mac": "macOS 苹果",
    }.get(str(platform or "").lower(), str(platform or "未知平台"))
    buyer_label = str(buyer_email or "未填写（不发送买家邮件）")

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',Arial,sans-serif;max-width:540px;margin:0 auto;padding:32px 20px;background:#f4f4f5;color:#18181b">
      <div style="background:#fff;border:1px solid #e4e4e7;border-radius:18px;padding:26px">
        <h1 style="font-size:22px;margin:0 0 18px">喵脸收到一笔成功支付</h1>
        <p style="font-size:14px;margin:0 0 7px"><strong>订单号：</strong>{html.escape(str(order_id))}</p>
        <p style="font-size:14px;margin:0 0 7px"><strong>套餐：</strong>{html.escape(str(product_name))}（{int(days)} 天）</p>
        <p style="font-size:14px;margin:0 0 7px"><strong>金额：</strong>¥{float(amount):.2f}</p>
        <p style="font-size:14px;margin:0 0 7px"><strong>平台：</strong>{html.escape(platform_label)}</p>
        <p style="font-size:14px;margin:0 0 7px"><strong>买家邮箱：</strong>{html.escape(buyer_label)}</p>
        <p style="font-size:14px;margin:0"><strong>支付时间：</strong>{html.escape(str(paid_time))}</p>
      </div>
      <p style="text-align:center;color:#a1a1aa;font-size:11px;margin-top:16px">喵脸 · 支付成功自动通知</p>
    </div>
    """


async def send_admin_payment_notification_email(**order_data) -> tuple[bool, str]:
    product_name = str(order_data.get("product_name") or "专线时长")
    subject = f"【喵脸收款】¥{float(order_data.get('amount') or 0):.2f}｜{product_name}"
    email_html = build_admin_payment_notification_email_html(**order_data)
    return await send_email(PAYMENT_NOTIFY_EMAIL, subject, email_html)
