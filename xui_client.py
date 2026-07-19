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
XUI_HOST = os.getenv("XUI_HOST", "https://api-x7f2.jmsht.one:2053")
XUI_PATH = os.getenv("XUI_PATH", "/voeM3TymjnD2DsYGKn")
XUI_API_TOKEN = os.getenv("XUI_API_TOKEN", "gbLxoecwk2KtxKY2liUjXGcl7RG3mPvWZiZf97XKLQCPR4vz")

# 订阅服务地址（客户端拿节点列表用的地址，通常和面板不是同一个端口/路径！）
XUI_SUB_HOST = os.getenv("XUI_SUB_HOST", "https://api-x7f2.jmsht.one:2096")
XUI_SUB_PATH = os.getenv("XUI_SUB_PATH", "/sub")     # 订阅前缀，如 /sub 或 /subscribe

# 代理地址配置
XUI_PROXY = os.getenv("XUI_PROXY", None)

# 【核心配置】：保持这里为空列表，系统就会自动获取所有节点
MANUAL_INBOUND_IDS: list[int] = []  # 例如 [1, 2, 3]，留空则自动获取全部

# 【协议过滤】：只同步这些协议的 inbound，避免把 VLESS 的 client 结构
# 无差别写进 VMess/Trojan/Shadowsocks 等其他协议的 inbound，把它们的 settings 搞乱。
# 留空 = 不过滤（不建议，除非你确定面板上所有 inbound 协议一致）。
MANAGED_PROTOCOLS: set[str] = {p.strip() for p in os.getenv("XUI_MANAGED_PROTOCOLS", "vless").split(",") if p.strip()}

# 【新客户端默认字段】：可选，JSON 格式，比如 '{"flow": "xtls-rprx-vision", "encryption": "none"}'
# 如果配置了，新建 client 时会强制用这里的值（优先级最高），不再单纯依赖从旧 client 里"猜"。
# 强烈建议配上，尤其是你已经怀疑现有面板数据里有被写坏的 client 时——不配的话，
# 继承逻辑只能从现有 client 里挑，挑到坏数据的概率不是零。
try:
    XUI_DEFAULT_CLIENT_FIELDS: dict = json.loads(os.getenv("XUI_DEFAULT_CLIENT_FIELDS", "{}"))
except Exception:
    XUI_DEFAULT_CLIENT_FIELDS = {}

# 判断一个 client 的这些字段是否"看起来正常"（非空字符串/非 None）。
# 用来在挑模板、以及同步后自检时，识别"这个 client 本身就是坏的，别学它/别把它当结果放过"。
_SECURITY_RELEVANT_FIELDS = ("flow", "encryption")
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
    # local_address="0.0.0.0" 强制走 IPv4，避免容器内 IPv6 出站不通时，
    # httpx 异步客户端的 Happy Eyeballs 并发尝试 IPv6 地址导致整体连接失败
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(
        transport=transport,
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

    if MANAGED_PROTOCOLS:
        before = len(inbounds)
        inbounds = [item for item in inbounds if item.get("protocol") in MANAGED_PROTOCOLS]
        skipped = before - len(inbounds)
        if skipped:
            logger.info(f"按 MANAGED_PROTOCOLS={MANAGED_PROTOCOLS} 过滤掉 {skipped} 个非目标协议的 inbound")

    ids = [item["id"] for item in inbounds if "id" in item]
    logger.info(f"从面板获取到 {len(ids)} 个需要同步的节点: {ids}")
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


async def _get_existing_client(
    client: httpx.AsyncClient, inbound_id: int, client_uuid: str, device_id: str
):
    """
    只读地看一眼这个 inbound 现有的完整 settings：用来判断这次是新建还是续期，
    并且给续期/挑模板/回写提供数据。这里只 GET，不会把 inbound 整体写回去，
    所以本函数自身不存在"顺手把 streamSettings 之类字段覆盖掉"的风险。

    关键：把完整的 settings 字典也原样返回给调用方（而不只是 clients 数组），
    这样调用方后续组装写入请求时，可以把 decryption/fallbacks 等 inbound 级
    字段原样带回去，不依赖面板服务端自己做"智能合并"。

    返回 (匹配到的现有 client 或 None, 该 inbound 全部 clients 列表,
          该 inbound 完整 settings 字典, 错误信息或 None)
    """
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/inbounds/get/{inbound_id}")
    if not res or res.status_code != 200:
        return None, [], {}, f"获取节点信息失败 HTTP {res.status_code if res else 'None'}"
    try:
        data = res.json()
        if not data.get("success"):
            return None, [], {}, "节点获取失败: success=false"
        inbound = data.get("obj", {})
        settings = json.loads(inbound.get("settings", "{}"))
    except Exception as e:
        return None, [], {}, f"解析节点数据异常: {e}"

    clients = settings.get("clients", []) or []
    for c in clients:
        if c.get("id") == client_uuid or c.get("email") == device_id:
            return c, clients, settings, None
    return None, clients, settings, None


def _pick_healthy_template(clients: list[dict], payload_client: dict) -> dict | None:
    """
    从同一 inbound 现有的 client 里，挑一个 flow/encryption 等安全相关字段都不为空的，
    当作新 client 的模板。跳过看起来已经是"裸"/被写坏的 client，避免把坏数据继续传染给
    新用户。找不到就返回 None（上层会拒绝同步或用 XUI_DEFAULT_CLIENT_FIELDS 兜底）。
    """
    for c in clients:
        if c.get("id") == payload_client.get("id"):
            continue
        if all(c.get(f) not in (None, "") for f in _SECURITY_RELEVANT_FIELDS):
            return c
    return None


async def _sync_single_inbound(
    client: httpx.AsyncClient, inbound_id: int, device_id: str, expiry_ms: int
) -> tuple[int, bool, str]:
    """
    单节点同步：使用 3x-ui 的 client 接口（addClient / updateClient）。
    body 里只包含 "settings"，不涉及 streamSettings/sniffing/listen/port 等
    inbound 顶层配置——那些字段完全不在这次请求体里，从结构上没有被改动的机会。
    settings 内部：decryption/fallbacks 等协议级字段用完整读到的原值打底，
    避免被面板服务端用空值覆盖；但 clients 数组只放这一次要处理的单个
    client（不带其它 sibling clients），因为 addClient/updateClient 的
    实际语义是逐个处理数组里的元素，塞入已存在的 client 反而可能导致
    冲突、让真正要新增的那个没生效（已实测验证过这一点）。
    仍然持有该 inbound 的锁，避免两笔订单并发操作同一个 clientId 时互相踩踏。
    """
    async with _get_inbound_lock(inbound_id):
        return await _sync_single_inbound_locked(client, inbound_id, device_id, expiry_ms)


async def _sync_single_inbound_locked(
    client: httpx.AsyncClient, inbound_id: int, device_id: str, expiry_ms: int
) -> tuple[int, bool, str]:
    client_uuid = _derive_client_uuid(device_id)
    sub_id = _derive_sub_id(client_uuid)
    payload_client = _build_client_payload(device_id, client_uuid, sub_id, expiry_ms)

    existing_client, sibling_clients, full_settings, err = await _get_existing_client(
        client, inbound_id, client_uuid, device_id
    )
    if err:
        return inbound_id, False, err

    if existing_client is not None:
        # 续期：以现有 client 的完整字段为底，只覆盖我们主动管理的字段（expiryTime 等），
        # flow/encryption 这些原样保留，然后整个对象发给 updateClient——只动这一个 client。
        merged_client = {**existing_client, **payload_client}
        url = f"{XUI_PATH}/panel/api/inbounds/updateClient/{client_uuid}"
        action = "updated"
    else:
        template = _pick_healthy_template(sibling_clients, payload_client)

        if template is None and not XUI_DEFAULT_CLIENT_FIELDS:
            if sibling_clients:
                logger.warning(
                    f"[危险] inbound={inbound_id} 现有 {len(sibling_clients)} 个 client 但没有一个 "
                    f"flow/encryption 字段完整，怀疑该 inbound 数据已经被写坏。"
                    f"已跳过同步，请人工检查该 inbound 后再重试，或配置 XUI_DEFAULT_CLIENT_FIELDS。"
                )
                return inbound_id, False, "该 inbound 现有 client 安全字段异常，已跳过写入，需人工检查"
            else:
                logger.info(f"inbound={inbound_id} 目前没有任何 client，新建的 client 不会带 flow/encryption")

        inherited = {k: v for k, v in (template or {}).items() if k not in payload_client}
        merged_client = {**inherited, **payload_client, **XUI_DEFAULT_CLIENT_FIELDS}
        url = f"{XUI_PATH}/panel/api/inbounds/addClient"
        action = "added"

    logger.info(
        f"[诊断] inbound={inbound_id} {action} client 安全字段="
        f"{ {k: merged_client.get(k) for k in _SECURITY_RELEVANT_FIELDS} }"
    )

    # 关键修复：settings 的顶层字段（decryption/fallbacks 等）用完整的
    # full_settings 打底，避免被面板服务端用空值刷掉；但 clients 数组
    # 只放这一次要新增/更新的这一个 client——不带上其它 sibling clients。
    # 原因：addClient/updateClient 这两个接口的实际语义是"处理你传的
    # settings.clients 里的每一个元素"，如果把已存在的 client 也塞进去，
    # 服务端可能把它们当成冲突/重复处理掉，导致真正要新增的那个反而没生效
    # （这一点已经过实际测试验证：发全量数组时返回 success，但 client 未真正写入）。
    outgoing_settings = {**full_settings, "clients": [merged_client]}

    logger.info(
        f"[诊断] inbound={inbound_id} 即将写入 settings: "
        f"decryption={outgoing_settings.get('decryption')!r}, "
        f"fallbacks数量={len(outgoing_settings.get('fallbacks') or [])}, "
        f"sibling_clients原有数量={len(sibling_clients)}"
    )

    body = {
        "id": inbound_id,
        "settings": json.dumps(outgoing_settings, ensure_ascii=False),
    }

    res = await _request_with_retry(client, "POST", url, json=body)

    if res and res.status_code == 200:
        try:
            up_data = res.json()
        except Exception:
            # 已知问题：3x-ui 部分版本的 addClient/updateClient 偶发返回空响应体
            # （HTTP 200 但 body 为空字符串），这种不能当成功处理，否则会掩盖真实失败。
            return inbound_id, False, "面板返回空响应（疑似 3x-ui 已知的空响应问题），建议重试或人工核对"
        if up_data.get("success"):
            return inbound_id, True, action
        return inbound_id, False, f"面板返回失败: {up_data.get('msg')}"

    status = res.status_code if res else 'no_response'
    return inbound_id, False, f"写入 client 失败 HTTP {status}"


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