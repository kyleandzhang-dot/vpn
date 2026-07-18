import io
import os
import json
import uuid
import base64
import logging
import asyncio
import httpx

logger = logging.getLogger("xui_subscription")

# ================= 配置（全部走环境变量，不要在代码里硬编码真实值） =================

# 面板管理地址（登录后台、增删改 inbound 用）
XUI_HOST = os.getenv("XUI_HOST", "https://127.0.0.1:2053")
XUI_PATH = os.getenv("XUI_PATH", "/voeM3TymjnD2DsYGKn")
XUI_API_TOKEN = os.getenv("XUI_API_TOKEN", "gbLxoecwk2KtxKY2liUjXGcl7RG3mPvWZiZf97XKLQCPR4vz")

# 订阅服务地址（客户端拿节点列表用的地址，通常和面板不是同一个端口/路径！）
XUI_SUB_HOST = os.getenv("XUI_SUB_HOST", "https://api-x7f2.jmsht.one:2096")
XUI_SUB_PATH = os.getenv("XUI_SUB_PATH", "/sub")     # 订阅前缀，如 /sub 或 /subscribe

# 代理地址配置
XUI_PROXY = os.getenv("XUI_PROXY", None)

# 【核心配置】：保持这里为空列表，系统就会自动获取所有节点
MANUAL_INBOUND_IDS: list[int] = []  # 例如 [1, 2, 3]，留空则自动获取全部

# 并发上限，避免把面板打挂
CONCURRENCY = int(os.getenv("XUI_SYNC_CONCURRENCY", "10"))

# 单请求超时 & 失败重试
REQUEST_TIMEOUT = float(os.getenv("XUI_REQUEST_TIMEOUT", "15.0"))
MAX_RETRIES = int(os.getenv("XUI_MAX_RETRIES", "2"))


def _require_config():
    if XUI_API_TOKEN in ("", "改成你的真实token"):
        raise RuntimeError("XUI_API_TOKEN 还是占位符，请改成真实 token 再运行")


def _build_client() -> httpx.AsyncClient:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {XUI_API_TOKEN}",
        "X-Requested-With": "XMLHttpRequest",
    }
    return httpx.AsyncClient(
        base_url=XUI_HOST,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        verify=False,   
        trust_env=True,
        proxy=XUI_PROXY,  
    )


def _derive_client_uuid(device_id: str) -> str:
    """把 device_id 映射成稳定的 uuid，同一个 device_id 永远得到同一个 uuid"""
    try:
        uuid.UUID(device_id)
        return device_id
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, device_id))


def _derive_sub_id(client_uuid: str) -> str:
    return f"sub_{client_uuid.replace('-', '')[:16]}"


async def _request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = await client.request(method, url, **kwargs)
            if res.status_code != 200:
                logger.warning(
                    f"非 200 响应 {method} {url} -> {res.status_code}, body[:300]={res.text[:300]!r}"
                )
            return res
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
    logger.warning(f"请求最终失败 {method} {url}: {last_exc}")
    return None


# 【核心逻辑 1】：动态获取面板当前所有节点(inbound)的 id 的函数
async def fetch_all_inbound_ids(client: httpx.AsyncClient) -> list[int]:
    """
    动态获取面板当前所有节点(inbound)的 id，不用再手动维护 1-100 这种范围。
    对应面板接口: GET {XUI_PATH}/panel/api/inbounds/list
    """
    # 将此处的 "POST" 更改为 "GET"
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/inbounds/list")

    if res is None:
        raise RuntimeError("获取节点列表失败：请求超时/连接失败，请检查 XUI_HOST 是否可达")
    if res.status_code != 200:
        raise RuntimeError(
            f"获取节点列表失败：HTTP {res.status_code}，"
            f"大概率是 XUI_PATH 配错了，或者这套面板的 API 路径/鉴权方式和预期不一致。"
            f"返回内容: {res.text[:300]}"
        )
    try:
        data = res.json()
    except Exception:
        raise RuntimeError(f"获取节点列表失败：返回内容不是合法 JSON: {res.text[:300]}")

    if not data.get("success"):
        raise RuntimeError(f"获取节点列表失败：面板返回 success=false, msg={data.get('msg')}")

    inbounds = data.get("obj") or []
    ids = [item["id"] for item in inbounds if "id" in item]
    logger.info(f"从面板获取到 {len(ids)} 个当前节点: {ids}")
    return ids


def _build_client_payload(device_id: str, client_uuid: str, sub_id: str, expiry_ms: int) -> dict:
    # 核心判断：如果 device_id 是以 "web_" 开头，说明是 iOS/Mac 的网站直购，取 uid 前 5 位
    # 否则（如安卓、Windows 的真实设备 ID），保持原样不变
    email_display = client_uuid[:5] if device_id.startswith("web_") else device_id

    return {
        "id": client_uuid,
        "email": email_display, 
        "expiryTime": expiry_ms,
        "enable": True,
        "subId": sub_id,
        "limitIp": 3,
        "totalGB": 0,
        "tgId": "",
        "reset": 0,
    }


async def _sync_single_inbound(
    client: httpx.AsyncClient, inbound_id: int, device_id: str, expiry_ms: int
) -> tuple[int, bool, str]:
    """
    通用单节点同步方法：兼容所有 x-ui / 3x-ui 版本。
    先获取整个 inbound，修改 settings 中的 clients 列表后，再全量提交更新。
    """
    client_uuid = _derive_client_uuid(device_id)
    sub_id = _derive_sub_id(client_uuid)
    payload_client = _build_client_payload(device_id, client_uuid, sub_id, expiry_ms)

    # 1. 获取当前节点的完整配置
    res = await _request_with_retry(
        client, "GET",
        f"{XUI_PATH}/panel/api/inbounds/get/{inbound_id}"
    )
    if not res or res.status_code != 200:
        return inbound_id, False, f"获取节点失败 HTTP {res.status_code if res else 'None'}"

    try:
        data = res.json()
        if not data.get("success"):
            return inbound_id, False, "节点获取失败: success=false"
        inbound = data.get("obj", {})
    except Exception as e:
        return inbound_id, False, f"解析节点数据异常: {e}"

    # 2. 解析 settings，将新用户更新或追加进去
    try:
        settings = json.loads(inbound.get("settings", "{}"))
    except Exception:
        settings = {"clients": []}

    if "clients" not in settings:
        settings["clients"] = []

    client_exists = False
    for i, c in enumerate(settings["clients"]):
        # 如果 uuid 或者 email 一致，说明是旧用户续期
        if c.get("id") == client_uuid or c.get("email") == device_id:
            # 只更新我们需要修改的字段，保留该用户原本的特有配置（如 flow）
            settings["clients"][i].update(payload_client)
            client_exists = True
            break

    # 如果是新用户，直接追加到列表末尾
    if not client_exists:
        settings["clients"].append(payload_client)

    # 将字典重新转回 JSON 字符串
    inbound["settings"] = json.dumps(settings, ensure_ascii=False)

    # 3. 将修改后的完整配置提交回面板进行更新
    update_res = await _request_with_retry(
        client, "POST",
        f"{XUI_PATH}/panel/api/inbounds/update/{inbound_id}",
        json=inbound
    )

    if update_res and update_res.status_code == 200:
        try:
            up_data = update_res.json()
            if up_data.get("success"):
                return inbound_id, True, "updated" if client_exists else "added"
        except Exception:
            pass

    status = update_res.status_code if update_res else 'no_response'
    return inbound_id, False, f"全量更新节点失败 HTTP {status}"


async def create_or_renew_subscription(device_id: str, expiry_ms: int) -> dict:
    """
    同步用户到所有目标节点，返回统一订阅链接。
    """
    _require_config()

    client = _build_client()

    try:
        # 【核心逻辑 2】：判断 MANUAL_INBOUND_IDS，为空则自动去抓取全部节点
        if MANUAL_INBOUND_IDS:
            inbound_ids = MANUAL_INBOUND_IDS
        else:
            inbound_ids = await fetch_all_inbound_ids(client)

        if not inbound_ids:
            await client.aclose()
            return {
                "success": False,
                "msg": "面板当前没有任何节点，或者获取节点列表失败，请检查 XUI_HOST/XUI_PATH/XUI_API_TOKEN 配置",
                "sub_link": None,
                "synced_nodes": 0,
                "total_nodes": 0,
                "failed_nodes": [],
            }

        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def sem_task(iid: int):
            async with semaphore:
                return await _sync_single_inbound(client, iid, device_id, expiry_ms)

        logger.info(f"开始同步用户 {device_id} 到 {len(inbound_ids)} 个节点")
        # 并发执行所有节点的绑定操作
        results = await asyncio.gather(*[sem_task(iid) for iid in inbound_ids])
    finally:
        await client.aclose()

    success_results = [r for r in results if r[1]]
    failed_results = [r for r in results if not r[1]]

    if failed_results:
        logger.warning(
            f"用户 {device_id} 同步失败节点: "
            + ", ".join(f"id={iid}({reason})" for iid, _, reason in failed_results)
        )

    logger.info(f"用户 {device_id} 同步完成，成功 {len(success_results)}/{len(inbound_ids)}")

    client_uuid = _derive_client_uuid(device_id)
    sub_id = _derive_sub_id(client_uuid)

    sub_link = f"{XUI_SUB_HOST}{XUI_SUB_PATH}/{sub_id}"

    return {
        "success": len(success_results) > 0,
        "sub_link": sub_link,
        "synced_nodes": len(success_results),
        "total_nodes": len(inbound_ids),
        "failed_nodes": [iid for iid, _, _ in failed_results],
    }


def make_qrcode_base64(data: str) -> str:
    import qrcode

    qr = qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffered.getvalue()).decode()}"