# 客户端设备令牌接入

节点权限不再信任客户端提交的 `device_id`。客户端必须在安全存储中保存设备令牌，并通过 `Authorization` 请求头发送。

## 全新设备注册

```http
POST /api/v1/auth/register
Content-Type: application/json

{"device_id":"客户端安装ID"}
```

响应中的 `data.access_token` 只应保存到 Android Keystore、iOS Keychain 或 Windows 凭据保险库，不要写入普通日志、URL、明文配置文件或分析事件。

## 老用户迁移

1. 管理员在总控制台用户列表点击“领取码”。
2. 用户在客户端输入一次性领取码。
3. 客户端调用：

```http
POST /api/v1/auth/claim
Content-Type: application/json

{"device_id":"原设备ID","claim_code":"ABCD-EFGH-JKLM"}
```

成功后保存 `data.access_token`。领取码默认15分钟过期且只能使用一次。

## 获取节点

```http
GET /api/v1/nodes
Authorization: Bearer vpn_xxxxxxxxx
```

```http
POST /api/v1/get_node
Authorization: Bearer vpn_xxxxxxxxx
Content-Type: application/json

{"node_id":"US-01"}
```

`device_id` 不再用于决定权限。若请求同时携带 `device_id`，服务端只把它用于迁移期一致性校验；与令牌身份不一致时返回 `403`。

下列用户接口同样必须携带相同的 `Authorization` 请求头：

- `/api/v1/recharge`
- `/api/v1/recharge_by_uid`
- `/api/v1/check_status`
- `/api/v1/client/subscription/recharge`
- `/api/v1/bind_invite`
- `/api/v1/invite_info`
- `/api/v1/checkin`
- `/api/v1/checkin_status`

## 令牌轮换

```http
POST /api/v1/auth/rotate
Authorization: Bearer vpn_xxxxxxxxx
```

保存响应中的新令牌后立即删除旧令牌，旧令牌已经失效。

## 部署配置

复制 `.env.example` 为 `.env`，设置高强度 `ADMIN_API_KEY`。控制台第一次请求时会提示输入该密钥，并仅保存于当前浏览器会话。

安全模式默认启用：

```env
ALLOW_LEGACY_DEVICE_AUTH=false
```

只有在旧客户端短期迁移时才可临时设为 `true`；该模式仍允许通过 `device_id` 访问，不能视为安全状态。

原生内购接口当前缺少 Apple/Google 服务端收据校验，因此安全默认关闭：

```env
ENABLE_UNVERIFIED_NATIVE_RECHARGE=false
```

不要为了恢复功能直接开启；应先完成商店收据或服务器通知校验，再由服务端根据已验证的商品决定充值天数。
