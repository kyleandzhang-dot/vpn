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

XUI_HOST = os.getenv("XUI_HOST", "https://api-x7f2.jmsht.one:2053")
XUI_PATH = os.getenv("XUI_PATH", "/voeM3TymjnD2DsYGKn")
XUI_API_TOKEN = os.getenv("XUI_API_TOKEN", "gbLxoecwk2KtxKY2liUjXGcl7RG3mPvWZiZf97XKLQCPR4vz")

XUI_SUB_HOST = os.getenv("XUI_SUB_HOST", "https://api-x7f2.jmsht.one:2096")
XUI_SUB_PATH = os.getenv("XUI_SUB_PATH", "/sub")

XUI_PROXY = os.getenv("XUI_PROXY", None)

# 留空 = 自动获取全部符合协议过滤的 inbound
MANUAL_INBOUND_IDS: list[int] = []

MANAGED_PROTOCOLS: set[str] = {
    p.strip() for p in os.getenv("XUI_MANAGED_PROTOCOLS", "vless").split(",") if p.strip()
}

# 新建 client 时强制附加的字段（flow/encryption 等），优先级最高
try:
    XUI_DEFAULT_CLIENT_FIELDS: dict = json.loads(os.getenv("XUI_DEFAULT_CLIENT_FIELDS", "{}"))
except Exception:
    XUI_DEFAULT_CLIENT_FIELDS = {}

CONCURRENCY = int(os.getenv("XUI_SYNC_CONCURRENCY", "10"))
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
    try:
        uuid.UUID(device_id)
        return device_id
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, device_id))


def _derive_sub_id(client_uuid: str) -> str:
    return f"sub_{client_uuid.replace('-', '')[:16]}"


def _email_for(device_id: str, client_uuid: str, length: int = 12) -> str:
    # web_ 前缀（网站直购）用 uuid 前 length 位当展示名，其它设备保持原样。
    # 之前只取 5 位（16^5 ≈ 100万种组合），几千个 web 用户就有相当高的碰撞概率；
    # 12 位（16^12）碰撞概率可忽略不计，仍然保持足够短、适合展示。
    return client_uuid.replace("-", "")[:length] if device_id.startswith("web_") else device_id


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


def _ok(res) -> tuple[bool, dict | None, str]:
    """统一判断响应是否成功，返回 (是否成功, 解析后的json或None, 错误信息)"""
    if res is None:
        return False, None, "请求超时/连接失败"
    if res.status_code == 404:
        return False, None, "404_NOT_FOUND"
    if res.status_code != 200:
        return False, None, f"HTTP {res.status_code}: {res.text[:300]}"
    try:
        data = res.json()
    except Exception:
        return False, None, f"响应不是合法 JSON: {res.text[:300]}"
    if not data.get("success"):
        return False, data, data.get("msg", "面板返回 success=false")
    return True, data, ""


# ============ 0. 节点展示名（remark）单独管理，跟 email/subId 唯一性逻辑解耦 ============
async def set_inbound_remark(client: httpx.AsyncClient, inbound_id: int, remark: str) -> tuple[bool, str]:
    """
    把 inbound 的展示名（客户端 App 里看到的节点名称）改成指定文字，
    比如 "秒连-高速专线"。这个字段跟 client 的 email/subId 完全无关，
    不会影响你现有的唯一性/查重逻辑。

    /panel/api/inbounds/update/{id} 是整体替换，必须先 GET 完整数据，
    只改 remark 一个字段，其余原样带回去，否则会把 settings/streamSettings
    等字段清空。
    """
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/inbounds/get/{inbound_id}")
    ok, data, err = _ok(res)
    if not ok:
        return False, f"读取 inbound 失败: {err}"

    inbound = data.get("obj") or {}
    inbound["remark"] = remark

    # settings/streamSettings/sniffing 这套面板既接受 dict 也接受字符串两种写法，
    # 这里保持读到的是什么形状就原样传回去，不做转换，避免格式踩坑。
    res2 = await _request_with_retry(
        client, "POST", f"{XUI_PATH}/panel/api/inbounds/update/{inbound_id}", json=inbound
    )
    ok2, data2, err2 = _ok(res2)
    if not ok2:
        return False, f"更新 inbound remark 失败: {err2}"
    return True, "remark_updated"


# ============ 1. 获取需要同步的全部 inbound id（保留原逻辑，这套接口仍然有效） ============
async def fetch_all_inbound_ids(client: httpx.AsyncClient) -> list[int]:
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/inbounds/list")
    ok, data, err = _ok(res)
    if not ok:
        raise RuntimeError(
            f"获取节点列表失败：{err}。"
            f"请确认 XUI_PATH 是否正确、Bearer token 是否有效（Settings→Security→API Token）。"
        )

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


def _build_client_payload(device_id: str, client_uuid: str, email: str, sub_id: str, expiry_ms: int) -> dict:
    payload = {
        "id": client_uuid,
        "email": email,
        "expiryTime": expiry_ms,
        "enable": True,
        "subId": sub_id,
        "limitIp": 3,
        "totalGB": 0,
        "tgId": 0,          # schema: tgId 是 integer，不是字符串，之前传 "" 类型不匹配
        "reset": 0,
        "comment": "",      # schema 标为必填字段
        "security": "auto", # schema 标为必填字段；对 VLESS 无实际意义，但缺了可能过不了校验
    }
    # 新建时强制附加 flow/encryption 等安全字段（如果配置了）
    payload.update(XUI_DEFAULT_CLIENT_FIELDS)
    return payload


# ============ 2. 新版 Clients 实体 API：查 / 建 / 改 / 挂载 ============

async def _get_client_by_email(client: httpx.AsyncClient, email: str):
    """
    GET /panel/api/clients/get/{email}
    返回 (client_dict_or_None, 已挂载的 inbound_id 列表, 错误信息或 None)
    不存在时返回 (None, [], None) —— 不是错误，是"需要新建"的信号。
    """
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/clients/get/{email}")

    if res is None:
        return None, [], "请求超时/连接失败"
    if res.status_code == 404:
        return None, [], None  # 真正的 HTTP 404，也视为不存在
    if res.status_code != 200:
        return None, [], f"HTTP {res.status_code}: {res.text[:300]}"
    try:
        data = res.json()
    except Exception:
        return None, [], f"响应不是合法 JSON: {res.text[:300]}"

    if not data.get("success"):
        msg = (data.get("msg") or "").strip()
        # 关键：这套面板对"客户不存在"返回的是 HTTP 200 + success:false +
        # msg 里带 "record not found"，不是真正的 404 状态码。之前只认状态码
        # 导致永远判定成"查询出错"而不是"该走新建"，client 一直建不出来。
        if "record not found" in msg.lower() or "not found" in msg.lower():
            return None, [], None
        return None, [], msg or "面板返回 success=false"

    obj = data.get("obj") or {}
    # 兼容两种返回形状：
    # 1) 扁平：obj 本身就是 client，另外带 inboundIds/inbounds 字段
    # 2) 嵌套：obj = {"client": {...真正的client...}, "inboundIds": [...]}（与 /export 同构）
    if isinstance(obj.get("client"), dict):
        client_obj = obj["client"]
    else:
        client_obj = obj
    raw_inbound_ids = obj.get("inboundIds") or obj.get("inbounds") or []
    # ClientInbound 关系表结构是 {clientId, inboundId, flowOverride, createdAt}，
    # 所以这里返回的很可能是一组关系对象而不是纯 int 数组，两种都兼容。
    inbound_ids = []
    for item in raw_inbound_ids:
        if isinstance(item, dict):
            iid = item.get("inboundId") or item.get("id")
            if iid is not None:
                inbound_ids.append(iid)
        else:
            inbound_ids.append(item)

    # 兜底校验：如果解出来的 client_obj 里连 email/id 都没有，说明这次解析大概率还是不对，
    # 当成"没找到"处理，交给上层走"新建"分支，比带着残缺数据去 update 更安全。
    if not client_obj.get("email") and not client_obj.get("id"):
        logger.warning(f"GET /clients/get 返回结构无法识别，原始 obj keys={list(obj.keys())}，按不存在处理")
        return None, [], None

    return client_obj, inbound_ids, None


async def _create_client(client: httpx.AsyncClient, payload: dict, inbound_ids: list[int]) -> tuple[bool, str]:
    """
    POST /panel/api/clients/add
    请求体与 /export、/bulkCreate 的元素同构：{"client": {...}, "inboundIds": [...]}
    一次调用把该用户挂到全部目标节点上，不再需要逐个 inbound 循环写 settings。
    """
    body = {"client": payload, "inboundIds": inbound_ids}
    res = await _request_with_retry(client, "POST", f"{XUI_PATH}/panel/api/clients/add", json=body)
    ok, data, err = _ok(res)
    if not ok:
        return False, f"创建 client 失败: {err}"
    return True, "added"


async def _update_client(client: httpx.AsyncClient, email: str, full_payload: dict) -> tuple[bool, str]:
    """
    POST /panel/api/clients/update/{email}
    整行替换，必须把要保留的字段（flow/encryption/id 等）一起带上，不能只传变更字段。
    """
    res = await _request_with_retry(
        client, "POST", f"{XUI_PATH}/panel/api/clients/update/{email}", json=full_payload
    )
    ok, data, err = _ok(res)
    if not ok:
        return False, f"更新 client 失败: {err}"
    return True, "updated"


async def _attach_client(client: httpx.AsyncClient, email: str, inbound_ids: list[int]) -> tuple[bool, str]:
    """POST /panel/api/clients/{email}/attach —— 把已存在的 client 补挂到新增的 inbound 上"""
    if not inbound_ids:
        return True, "no_attach_needed"
    body = {"inboundIds": inbound_ids}
    res = await _request_with_retry(
        client, "POST", f"{XUI_PATH}/panel/api/clients/{email}/attach", json=body
    )
    ok, data, err = _ok(res)
    if not ok:
        return False, f"挂载新节点失败: {err}"
    return True, "attached"


# 按 email 加锁，防止同一个用户的并发请求（新建+续期）互相踩踏
_client_locks: dict[str, asyncio.Lock] = {}


def _get_client_lock(email: str) -> asyncio.Lock:
    lock = _client_locks.get(email)
    if lock is None:
        lock = asyncio.Lock()
        _client_locks[email] = lock
    return lock


async def _sync_client_locked(
    client: httpx.AsyncClient, device_id: str, target_inbound_ids: list[int], expiry_ms: int
) -> tuple[bool, str]:
    client_uuid = _derive_client_uuid(device_id)
    sub_id = _derive_sub_id(client_uuid)
    email = _email_for(device_id, client_uuid)

    existing, attached_ids, err = await _get_client_by_email(client, email)
    if err:
        return False, f"查询 client 失败: {err}"

    if existing is None:
        payload = _build_client_payload(device_id, client_uuid, email, sub_id, expiry_ms)
        logger.info(f"[新建] email={email} 目标节点={target_inbound_ids}")
        return await _create_client(client, payload, target_inbound_ids)

    # 关键安全校验：查到了 email 对应的记录，不代表这条记录真的是这个 device_id 的。
    # email 是从 uuid 截断出来的展示名，理论上存在（哪怕概率很低的）碰撞可能——
    # 如果碰撞发生但不校验，会把 A 用户的到期时间/流量额度改成 B 用户的参数，
    # 甚至让两个用户共用同一条 uuid 订阅链接（等于免费蹭号，是安全问题）。
    # 面板真实返回的字段名是 "uuid"（不是 "id"，"id" 是内部数据库数字主键）。
    existing_uuid = existing.get("uuid")
    if existing_uuid and existing_uuid != client_uuid:
        logger.error(
            f"[碰撞] email={email} 已存在但 uuid 不匹配 "
            f"(面板记录 uuid={existing_uuid}, 本次期望 uuid={client_uuid})，"
            f"判定为 email 截断碰撞，拒绝写入以避免误改他人账号"
        )
        return False, f"email 碰撞：{email} 已被其他用户占用，请检查截断位数是否足够"

    # 不能直接 **existing 整体展开——这套面板的 GET /clients/get/{email} 返回的
    # "id" 字段实际是内部数据库数字主键（和 ClientInbound.clientId 是 int 对得上），
    # 跟我们创建时传的字符串 UUID 字段名冲突但类型不同，整体回传会导致
    # "cannot unmarshal number into Go struct field Client.id of type string"。
    # 只挑我们不主动管理、且确定是我们要保留的协议相关字段，其它字段的类型全部
    # 由我们自己显式控制，不依赖 GET 返回值的类型。
    _PRESERVE_FIELDS = (
        "flow", "password", "auth", "group", "allowedIPs",
        "keepAlive", "preSharedKey", "privateKey", "publicKey",
    )
    merged = {k: existing[k] for k in _PRESERVE_FIELDS if existing.get(k) not in (None, "")}
    merged.update({
        "id": client_uuid,          # 强制用我们自己派生的字符串 UUID，不信任 GET 回传的 id
        "email": email,
        "expiryTime": expiry_ms,
        "enable": True,
        "subId": existing.get("subId") or sub_id,
        "limitIp": existing.get("limitIp") if isinstance(existing.get("limitIp"), int) else 3,
        "totalGB": existing.get("totalGB") if isinstance(existing.get("totalGB"), int) else 0,
        "tgId": existing.get("tgId") if isinstance(existing.get("tgId"), int) else 0,
        "reset": existing.get("reset") if isinstance(existing.get("reset"), int) else 0,
        "comment": existing.get("comment") if isinstance(existing.get("comment"), str) else "",
        "security": existing.get("security") if isinstance(existing.get("security"), str) and existing.get("security") else "auto",
    })
    ok, msg = await _update_client(client, email, merged)
    if not ok:
        return False, msg

    missing = [iid for iid in target_inbound_ids if iid not in attached_ids]
    if missing:
        logger.info(f"[补挂] email={email} 缺失节点={missing}")
        ok2, msg2 = await _attach_client(client, email, missing)
        if not ok2:
            return False, f"续期成功但补挂节点失败: {msg2}"

    return True, "updated"


async def sync_client(client: httpx.AsyncClient, device_id: str, inbound_ids: list[int], expiry_ms: int) -> tuple[str, bool, str]:
    lock = _get_client_lock(device_id)
    async with lock:
        ok, msg = await _sync_client_locked(client, device_id, inbound_ids, expiry_ms)
        return device_id, ok, msg


async def create_or_renew_subscription(device_id: str, expiry_ms: int) -> dict:
    """
    同步用户到所有目标节点，返回统一订阅链接。
    新版 Clients API 下，一个用户只需 1~2 次请求（add 或 update+attach），
    不再需要对每个 inbound 单独发一次 addClient/updateClient。
    """
    _require_config()

    client = _build_client()

    try:
        if MANUAL_INBOUND_IDS:
            inbound_ids = MANUAL_INBOUND_IDS
        else:
            inbound_ids = await fetch_all_inbound_ids(client)

        if not inbound_ids:
            return {
                "success": False,
                "msg": "面板当前没有任何节点，或者获取节点列表失败，请检查 XUI_HOST/XUI_PATH/XUI_API_TOKEN 配置",
                "sub_link": None,
                "synced_nodes": 0,
                "total_nodes": 0,
                "failed_nodes": [],
            }

        device_id, ok, msg = await sync_client(client, device_id, inbound_ids, expiry_ms)
    finally:
        await client.aclose()

    if not ok:
        logger.warning(f"用户 {device_id} 同步失败: {msg}")
    else:
        logger.info(f"用户 {device_id} 同步完成: {msg}")

    client_uuid = _derive_client_uuid(device_id)
    sub_id = _derive_sub_id(client_uuid)
    sub_link = f"{XUI_SUB_HOST}{XUI_SUB_PATH}/{sub_id}"

    return {
        "success": ok,
        "sub_link": sub_link if ok else None,
        "synced_nodes": len(inbound_ids) if ok else 0,
        "total_nodes": len(inbound_ids),
        "failed_nodes": [] if ok else [f"all ({msg})"],
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