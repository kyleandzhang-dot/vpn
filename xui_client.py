import io
import os
import json
import uuid
import base64
import logging
import asyncio
import httpx
import urllib.parse
import hashlib
from datetime import datetime

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
RESIDENTIAL_XRAY_SYNC_ENABLED = os.getenv(
    "RESIDENTIAL_XRAY_SYNC_ENABLED", "true"
).lower() == "true"
RESIDENTIAL_TAG_PREFIX = "managed-residential-"
RESIDENTIAL_CLIENT_PREFIX = "managed-residential-client-"


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
    # 不要用 "sub_" 了，直接改成你想要的品牌拼音或英文，比如 "MiaoLian-VIP-"
    return f"MiaoLian-VIP-{client_uuid.replace('-', '')[:8]}"


def _email_for(device_id: str, client_uuid: str, length: int = 12) -> str:
    return client_uuid.replace("-", "")[:length] if device_id.startswith("web_") else device_id


def client_email_for_device(device_id: str) -> str:
    """Return the exact Xray client email used by subscription synchronization."""
    return _email_for(device_id, _derive_client_uuid(device_id))


def residential_client_uuid(device_id: str, node_id: str) -> str:
    """Stable per-user/per-node UUID returned to the VPN client and stored in 3x-ui."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"miaolian-residential:{node_id}:{device_id}"))


def residential_client_email(device_id: str, node_id: str) -> str:
    digest = hashlib.sha256(f"{node_id}:{device_id}".encode("utf-8")).hexdigest()[:20]
    return f"{RESIDENTIAL_CLIENT_PREFIX}{digest}"


def _normalized_panel_path(path: str) -> str:
    clean = str(path or "").strip().strip("/")
    return f"/{clean}" if clean else ""


def _build_panel_client(panel: dict) -> httpx.AsyncClient:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {panel['api_token']}",
        "X-Requested-With": "XMLHttpRequest",
    }
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(
        transport=transport,
        base_url=str(panel["base_url"]).rstrip("/"),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        verify=False,
        trust_env=True,
        proxy=XUI_PROXY,
    )


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


async def set_inbound_remark(client: httpx.AsyncClient, inbound_id: int, remark: str) -> tuple[bool, str]:
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/inbounds/get/{inbound_id}")
    ok, data, err = _ok(res)
    if not ok:
        return False, f"读取 inbound 失败: {err}"

    inbound = data.get("obj") or {}
    inbound["remark"] = remark

    res2 = await _request_with_retry(
        client, "POST", f"{XUI_PATH}/panel/api/inbounds/update/{inbound_id}", json=inbound
    )
    ok2, data2, err2 = _ok(res2)
    if not ok2:
        return False, f"更新 inbound remark 失败: {err2}"
    return True, "remark_updated"


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
        "limitIp": 1,
        "totalGB": 0,
        "tgId": 0,
        "reset": 0,
        "comment": "",
        "security": "auto",
    }
    payload.update(XUI_DEFAULT_CLIENT_FIELDS)
    return payload


async def _get_client_by_email(client: httpx.AsyncClient, email: str):
    res = await _request_with_retry(client, "GET", f"{XUI_PATH}/panel/api/clients/get/{email}")

    if res is None:
        return None, [], "请求超时/连接失败"
    if res.status_code == 404:
        return None, [], None
    if res.status_code != 200:
        return None, [], f"HTTP {res.status_code}: {res.text[:300]}"
    try:
        data = res.json()
    except Exception:
        return None, [], f"响应不是合法 JSON: {res.text[:300]}"

    if not data.get("success"):
        msg = (data.get("msg") or "").strip()
        if "record not found" in msg.lower() or "not found" in msg.lower():
            return None, [], None
        return None, [], msg or "面板返回 success=false"

    obj = data.get("obj") or {}
    if isinstance(obj.get("client"), dict):
        client_obj = obj["client"]
    else:
        client_obj = obj
    raw_inbound_ids = obj.get("inboundIds") or obj.get("inbounds") or []
    inbound_ids = []
    for item in raw_inbound_ids:
        if isinstance(item, dict):
            iid = item.get("inboundId") or item.get("id")
            if iid is not None:
                inbound_ids.append(iid)
        else:
            inbound_ids.append(item)

    if not client_obj.get("email") and not client_obj.get("id"):
        logger.warning(f"GET /clients/get 返回结构无法识别，原始 obj keys={list(obj.keys())}，按不存在处理")
        return None, [], None

    return client_obj, inbound_ids, None


async def _create_client(client: httpx.AsyncClient, payload: dict, inbound_ids: list[int]) -> tuple[bool, str]:
    body = {"client": payload, "inboundIds": inbound_ids}
    res = await _request_with_retry(client, "POST", f"{XUI_PATH}/panel/api/clients/add", json=body)
    ok, data, err = _ok(res)
    if not ok:
        return False, f"创建 client 失败: {err}"
    return True, "added"


async def _update_client(client: httpx.AsyncClient, email: str, full_payload: dict) -> tuple[bool, str]:
    res = await _request_with_retry(
        client, "POST", f"{XUI_PATH}/panel/api/clients/update/{email}", json=full_payload
    )
    ok, data, err = _ok(res)
    if not ok:
        return False, f"更新 client 失败: {err}"
    return True, "updated"


async def _attach_client(client: httpx.AsyncClient, email: str, inbound_ids: list[int]) -> tuple[bool, str]:
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

    existing_uuid = existing.get("uuid")
    if existing_uuid and existing_uuid != client_uuid:
        logger.error(
            f"[碰撞] email={email} 已存在但 uuid 不匹配 "
            f"(面板记录 uuid={existing_uuid}, 本次期望 uuid={client_uuid})，"
            f"判定为 email 截断碰撞，拒绝写入以避免误改他人账号"
        )
        return False, f"email 碰撞：{email} 已被其他用户占用，请检查截断位数是否足够"

    _PRESERVE_FIELDS = (
        "flow", "password", "auth", "group", "allowedIPs",
        "keepAlive", "preSharedKey", "privateKey", "publicKey",
    )
    merged = {k: existing[k] for k in _PRESERVE_FIELDS if existing.get(k) not in (None, "")}
    merged.update({
        "id": client_uuid,
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
    已集成规范的 URL 编码与双字段兼容，确保客户端精准识别订阅频道名称。
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
    
    # 核心修改：对中文进行标准 URL 编码，同时携带 title 和 remark 双参数
    encoded_title = urllib.parse.quote("秒连-高速专线")
    sub_link = f"{XUI_SUB_HOST}{XUI_SUB_PATH}/{sub_id}?title={encoded_title}&remark={encoded_title}" if ok else None

    return {
        "success": ok,
        "sub_link": sub_link,
        "synced_nodes": len(inbound_ids) if ok else 0,
        "total_nodes": len(inbound_ids),
        "failed_nodes": [] if ok else [f"all ({msg})"],
    }


def _decode_xray_setting(data: dict) -> tuple[dict, str]:
    """Decode the nested JSON strings returned by 3x-ui's Xray settings API."""
    obj = data.get("obj")
    if isinstance(obj, str):
        obj = json.loads(obj)
    if not isinstance(obj, dict):
        raise RuntimeError("3x-ui 返回的 Xray 配置结构无法识别")

    raw_setting = obj.get("xraySetting", obj)
    if isinstance(raw_setting, str):
        setting = json.loads(raw_setting)
    else:
        setting = raw_setting
    if not isinstance(setting, dict):
        raise RuntimeError("3x-ui 返回的 xraySetting 不是 JSON 对象")
    return setting, str(obj.get("outboundTestUrl") or "")


def _is_api_routing_rule(rule: dict) -> bool:
    """Keep the panel's API routing rule ahead of managed user rules."""
    if rule.get("outboundTag") == "api":
        return True
    inbound_tags = rule.get("inboundTag") or []
    if isinstance(inbound_tags, str):
        inbound_tags = [inbound_tags]
    return "api" in inbound_tags


_residential_sync_lock = asyncio.Lock()


def _resolve_residential_inbound_tag(setting: dict, assignment: dict) -> str:
    """Resolve the selected console node to one concrete Xray inbound tag."""
    inbounds = setting.get("inbounds") or []
    if not isinstance(inbounds, list):
        raise RuntimeError("Xray inbounds 配置不是数组，无法按节点限制住宅出口")

    explicit_tag = str(assignment.get("inbound_tag") or "").strip()
    available_tags = {
        str(item.get("tag") or "").strip()
        for item in inbounds if isinstance(item, dict) and item.get("tag")
    }
    if explicit_tag:
        if available_tags and explicit_tag not in available_tags:
            raise RuntimeError(
                f"节点 {assignment.get('node_id')} 的 Xray 入站标签 {explicit_tag!r} 不存在"
            )
        return explicit_tag

    node_id = str(assignment.get("node_id") or "").strip()
    for candidate in (node_id, f"inbound-{node_id}"):
        if candidate and candidate in available_tags:
            return candidate

    try:
        node_config = json.loads(assignment.get("node_config_json") or "{}")
    except (TypeError, ValueError):
        node_config = {}
    try:
        node_port = int(node_config.get("port"))
    except (TypeError, ValueError):
        node_port = None

    if node_port is not None:
        port_matches = [
            str(item.get("tag") or "").strip()
            for item in inbounds
            if isinstance(item, dict)
            and item.get("tag")
            and str(item.get("port")) == str(node_port)
        ]
        if len(port_matches) == 1:
            return port_matches[0]
        if len(port_matches) > 1:
            raise RuntimeError(
                f"节点 {node_id} 的端口 {node_port} 匹配到多个 Xray 入站，请在节点里填写入站标签"
            )

    raise RuntimeError(
        f"无法找到节点 {node_id} 对应的 Xray inboundTag；请在控制台编辑节点并填写 3x-ui 入站标签"
    )


async def apply_residential_routing(assignments: list[dict]) -> tuple[bool, str]:
    """Replace only our managed Xray outbounds/rules and ask 3x-ui to reload Xray.

    Each assignment must contain the proxy, device_id, selected node_id and its
    inbound mapping. Existing non-managed outbounds and routing rules are preserved.
    """
    if not RESIDENTIAL_XRAY_SYNC_ENABLED:
        return False, "服务端已关闭 RESIDENTIAL_XRAY_SYNC_ENABLED，未应用住宅出口"

    _require_config()
    async with _residential_sync_lock:
        client = _build_client()
        try:
            response = await _request_with_retry(
                client, "POST", f"{XUI_PATH}/panel/api/xray/"
            )
            ok, data, err = _ok(response)
            if not ok or not data:
                return False, f"读取 3x-ui Xray 配置失败: {err}"

            try:
                setting, outbound_test_url = _decode_xray_setting(data)
            except (ValueError, TypeError, RuntimeError) as exc:
                return False, str(exc)

            outbounds = setting.setdefault("outbounds", [])
            if not isinstance(outbounds, list):
                return False, "Xray outbounds 配置不是数组，拒绝覆盖"
            setting["outbounds"] = [
                item for item in outbounds
                if not str(item.get("tag", "")).startswith(RESIDENTIAL_TAG_PREFIX)
            ]

            routing = setting.setdefault("routing", {})
            if not isinstance(routing, dict):
                return False, "Xray routing 配置不是对象，拒绝覆盖"
            rules = routing.setdefault("rules", [])
            if not isinstance(rules, list):
                return False, "Xray routing.rules 配置不是数组，拒绝覆盖"
            rules = [
                rule for rule in rules
                if not str(rule.get("outboundTag", "")).startswith(RESIDENTIAL_TAG_PREFIX)
            ]

            managed_rules = []
            for assignment in assignments:
                tag = f"{RESIDENTIAL_TAG_PREFIX}{assignment['id']}"
                inbound_tag = _resolve_residential_inbound_tag(setting, assignment)
                protocol = assignment["protocol"]
                settings = {
                    "address": assignment["host"],
                    "port": int(assignment["port"]),
                    "user": assignment["username"],
                    "pass": assignment["password"],
                }
                setting["outbounds"].append({
                    "tag": tag,
                    "protocol": protocol,
                    "settings": settings,
                })
                # Provider UDP support varies. Route TCP only so an unavailable
                # UDP association cannot break the user's entire VPN connection.
                managed_rules.append({
                    "type": "field",
                    "user": [client_email_for_device(assignment["device_id"])],
                    "inboundTag": [inbound_tag],
                    "network": "tcp",
                    "outboundTag": tag,
                })

            api_rule_count = 0
            while api_rule_count < len(rules) and _is_api_routing_rule(rules[api_rule_count]):
                api_rule_count += 1
            routing["rules"] = (
                rules[:api_rule_count] + managed_rules + rules[api_rule_count:]
            )

            form = {"xraySetting": json.dumps(setting, ensure_ascii=False)}
            if outbound_test_url:
                form["outboundTestUrl"] = outbound_test_url
            update_response = await _request_with_retry(
                client,
                "POST",
                f"{XUI_PATH}/panel/api/xray/update",
                data=form,
            )
            update_ok, _, update_err = _ok(update_response)
            if not update_ok:
                return False, f"保存并重载 3x-ui Xray 配置失败: {update_err}"
            return True, f"已应用 {len(assignments)} 条住宅出口用户路由"
        except Exception as exc:
            logger.exception("同步住宅出口路由失败")
            return False, f"同步住宅出口路由失败: {exc}"
        finally:
            await client.aclose()


def _decode_inbound_settings(inbound: dict) -> tuple[dict, bool]:
    raw = inbound.get("settings", {})
    was_string = isinstance(raw, str)
    if was_string:
        raw = json.loads(raw or "{}")
    if not isinstance(raw, dict):
        raise RuntimeError("3x-ui inbound settings 不是 JSON 对象")
    return raw, was_string


async def _read_panel_inbound(client: httpx.AsyncClient, path: str, inbound_id: int) -> dict:
    response = await _request_with_retry(
        client, "GET", f"{path}/panel/api/inbounds/get/{inbound_id}"
    )
    ok, data, error = _ok(response)
    if not ok or not data:
        raise RuntimeError(f"读取 3x-ui 入站 {inbound_id} 失败: {error}")
    inbound = data.get("obj")
    if not isinstance(inbound, dict):
        raise RuntimeError(f"3x-ui 入站 {inbound_id} 返回结构无法识别")
    return inbound


async def _write_panel_inbound(
    client: httpx.AsyncClient, path: str, inbound_id: int, inbound: dict
) -> None:
    response = await _request_with_retry(
        client, "POST", f"{path}/panel/api/inbounds/update/{inbound_id}", json=inbound
    )
    ok, _, error = _ok(response)
    if not ok:
        raise RuntimeError(f"更新 3x-ui 入站 {inbound_id} 客户端失败: {error}")


async def apply_node_residential_routing(
    panel: dict, assignments: list[dict]
) -> tuple[bool, str]:
    """Reconcile one VPN node's dedicated 3x-ui panel.

    A managed client is created for every residential assignment. Managed clients,
    outbounds and rules that are no longer desired are removed, while all ordinary
    3x-ui users and non-managed Xray configuration are preserved.
    """
    if not RESIDENTIAL_XRAY_SYNC_ENABLED:
        return False, "服务端已关闭 RESIDENTIAL_XRAY_SYNC_ENABLED，未应用住宅出口"

    required = ("base_url", "api_token", "inbound_id")
    missing = [key for key in required if not panel.get(key)]
    if missing:
        return False, f"节点 3x-ui 配置不完整，缺少: {', '.join(missing)}"

    path = _normalized_panel_path(panel.get("path", ""))
    node_id = str(panel.get("node_id") or "")
    async with _residential_sync_lock:
        client = _build_panel_client(panel)
        try:
            inbound_id = int(panel["inbound_id"])
            inbound = await _read_panel_inbound(client, path, inbound_id)
            settings, settings_was_string = _decode_inbound_settings(inbound)
            clients = settings.setdefault("clients", [])
            if not isinstance(clients, list):
                return False, "3x-ui inbound clients 不是数组，拒绝覆盖"

            desired_by_email = {
                residential_client_email(item["device_id"], node_id): item
                for item in assignments
            }
            preserved = [
                item for item in clients
                if not str(item.get("email") or "").startswith(RESIDENTIAL_CLIENT_PREFIX)
            ]
            for email, assignment in desired_by_email.items():
                expiry = assignment.get("expire_time")
                if isinstance(expiry, str):
                    try:
                        expiry_ms = int(datetime.strptime(
                            expiry, "%Y-%m-%d %H:%M:%S"
                        ).timestamp() * 1000)
                    except ValueError:
                        expiry_ms = 0
                else:
                    expiry_ms = int(expiry or 0)
                preserved.append({
                    "id": residential_client_uuid(assignment["device_id"], node_id),
                    "email": email,
                    "expiryTime": expiry_ms,
                    "enable": True,
                    "limitIp": 1,
                    "totalGB": 0,
                    "tgId": 0,
                    "reset": 0,
                    "subId": "",
                    "comment": "秒连控制台住宅出口托管，请勿手动修改",
                    "security": "auto",
                    **XUI_DEFAULT_CLIENT_FIELDS,
                })
            settings["clients"] = preserved
            inbound["settings"] = (
                json.dumps(settings, ensure_ascii=False)
                if settings_was_string else settings
            )
            await _write_panel_inbound(client, path, inbound_id, inbound)

            response = await _request_with_retry(client, "POST", f"{path}/panel/api/xray/")
            ok, data, error = _ok(response)
            if not ok or not data:
                return False, f"读取节点 3x-ui Xray 配置失败: {error}"
            setting, outbound_test_url = _decode_xray_setting(data)

            outbounds = setting.setdefault("outbounds", [])
            if not isinstance(outbounds, list):
                return False, "Xray outbounds 配置不是数组，拒绝覆盖"
            setting["outbounds"] = [
                item for item in outbounds
                if not str(item.get("tag", "")).startswith(RESIDENTIAL_TAG_PREFIX)
            ]

            routing = setting.setdefault("routing", {})
            if not isinstance(routing, dict):
                return False, "Xray routing 配置不是对象，拒绝覆盖"
            rules = routing.setdefault("rules", [])
            if not isinstance(rules, list):
                return False, "Xray routing.rules 配置不是数组，拒绝覆盖"
            rules = [
                rule for rule in rules
                if not str(rule.get("outboundTag", "")).startswith(RESIDENTIAL_TAG_PREFIX)
            ]

            managed_rules = []
            for assignment in assignments:
                tag = f"{RESIDENTIAL_TAG_PREFIX}{assignment['id']}"
                server = {
                    "address": assignment["host"],
                    "port": int(assignment["port"]),
                    "users": [{
                        "user": assignment["username"],
                        "pass": assignment["password"],
                    }],
                }
                setting["outbounds"].append({
                    "tag": tag,
                    "protocol": assignment["protocol"],
                    "settings": {"servers": [server]},
                })
                managed_rules.append({
                    "type": "field",
                    "user": [residential_client_email(assignment["device_id"], node_id)],
                    "network": "tcp",
                    "outboundTag": tag,
                })

            api_rule_count = 0
            while api_rule_count < len(rules) and _is_api_routing_rule(rules[api_rule_count]):
                api_rule_count += 1
            routing["rules"] = rules[:api_rule_count] + managed_rules + rules[api_rule_count:]

            form = {"xraySetting": json.dumps(setting, ensure_ascii=False)}
            if outbound_test_url:
                form["outboundTestUrl"] = outbound_test_url
            response = await _request_with_retry(
                client, "POST", f"{path}/panel/api/xray/update", data=form
            )
            ok, _, error = _ok(response)
            if not ok:
                return False, f"保存并重载节点 3x-ui Xray 配置失败: {error}"
            return True, f"节点 {node_id} 已应用 {len(assignments)} 个住宅专属用户"
        except Exception as exc:
            logger.exception("同步节点住宅出口失败")
            return False, f"节点 {node_id or '-'} 住宅出口同步失败: {exc}"
        finally:
            await client.aclose()


def make_qrcode_base64(data: str) -> str:
    import qrcode

    qr = qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffered.getvalue()).decode()}"
