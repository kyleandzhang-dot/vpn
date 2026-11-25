import os
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from pathlib import Path
from cryptography.fernet import Fernet

PROJECT_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path(tempfile.mkdtemp())
os.chdir(TEMP_DIR)
sys.path.insert(0, str(PROJECT_DIR))

import main  # noqa: E402  (切换到临时数据库目录后再导入)
import xui_client  # noqa: E402


def run_endpoint(coroutine):
    """这些接口内部没有异步 I/O，可直接推进协程，避免测试环境创建网络事件循环。"""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("接口意外产生了异步挂起")


class TagPermissionTests(unittest.TestCase):
    def setUp(self):
        os.environ["RESIDENTIAL_PROXY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        main.DB_FILE = str(TEMP_DIR / f"test_{self._testMethodName}.db")
        main.init_db()
        expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(main.DB_FILE) as conn:
            conn.execute(
                "INSERT INTO users (device_id, expire_time, uid) VALUES (?, ?, ?)",
                ("kyle", expiry, "100001")
            )
            self.device_token = main.issue_device_token(conn.cursor(), "kyle")
        self.authorization = f"Bearer {self.device_token}"

        for node_id, tags in (("public", []), ("node_a", ["a"]), ("node_b", ["b"])):
            response = run_endpoint(main.add_node(main.AddNodeRequest(
                node_id=node_id,
                remark=node_id,
                config_json='{"server":"127.0.0.1"}',
                tags=tags,
            )))
            self.assertEqual(response["code"], 200)

    def node_ids(self, device_id=None):
        response = run_endpoint(main.list_available_nodes(
            device_id=device_id,
            x_device_id=None,
            authorization=self.authorization,
        ))
        self.assertEqual(response["code"], 200)
        return {item["node_id"] for item in response["data"]}

    def test_public_nodes_are_visible_without_identity_or_tags(self):
        self.assertEqual(self.node_ids(), {"public"})
        self.assertEqual(self.node_ids("kyle"), {"public"})

    def test_user_can_use_union_of_matching_tags(self):
        result = run_endpoint(main.set_user_tags(main.SetUserTagsRequest(
            device_id="kyle", tags=["a", "b"]
        )))
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.node_ids("kyle"), {"public", "node_a", "node_b"})

    def test_manual_node_selection_cannot_bypass_tags(self):
        run_endpoint(main.set_user_tags(main.SetUserTagsRequest(device_id="kyle", tags=["a"])))
        denied = run_endpoint(main.get_node(
            main.NodeRequest(device_id="kyle", node_id="node_b"),
            authorization=self.authorization,
        ))
        self.assertEqual(denied["code"], 404)

        allowed = run_endpoint(main.get_node(
            main.NodeRequest(device_id="kyle", node_id="node_a"),
            authorization=self.authorization,
        ))
        self.assertEqual(allowed["code"], 200)

    def test_last_node_tag_removal_cleans_users(self):
        run_endpoint(main.set_user_tags(main.SetUserTagsRequest(device_id="kyle", tags=["a", "b"])))
        result = run_endpoint(main.update_node(main.UpdateNodeRequest(
            node_id="node_a",
            remark="node_a",
            config_json='{"server":"127.0.0.1"}',
            tags=[],
        )))
        self.assertEqual(result["code"], 200)

        users = run_endpoint(main.get_all_users())["data"]
        kyle = next(user for user in users if user["device_id"] == "kyle")
        self.assertEqual(kyle["tags"], ["b"])

        run_endpoint(main.delete_node(main.DeleteNodeRequest(node_id="node_b")))
        users = run_endpoint(main.get_all_users())["data"]
        kyle = next(user for user in users if user["device_id"] == "kyle")
        self.assertEqual(kyle["tags"], [])

    def test_unknown_tag_cannot_be_assigned_to_user(self):
        result = run_endpoint(main.set_user_tags(main.SetUserTagsRequest(
            device_id="kyle", tags=["not-exists"]
        )))
        self.assertEqual(result["code"], 400)

    def test_admin_can_search_user_by_partial_uid(self):
        matched = run_endpoint(main.get_all_users(uid="000"))["data"]
        self.assertEqual([user["device_id"] for user in matched], ["kyle"])

        missing = run_endpoint(main.get_all_users(uid="999"))["data"]
        self.assertEqual(missing, [])

    def test_node_list_rejects_missing_token(self):
        with self.assertRaises(main.HTTPException) as raised:
            run_endpoint(main.list_available_nodes(
                device_id=None, x_device_id=None, authorization=None
            ))
        self.assertEqual(raised.exception.status_code, 401)

    def test_token_rejects_spoofed_device_id(self):
        with self.assertRaises(main.HTTPException) as raised:
            run_endpoint(main.list_available_nodes(
                device_id="someone-else",
                x_device_id=None,
                authorization=self.authorization,
            ))
        self.assertEqual(raised.exception.status_code, 403)

    def test_admin_claim_code_issues_and_rotates_token(self):
        claim = run_endpoint(main.create_device_claim_code(
            main.CreateDeviceClaimCodeRequest(uid="100001", valid_minutes=15)
        ))
        self.assertEqual(claim["code"], 200)

        claimed = run_endpoint(main.claim_existing_device(main.ClaimDeviceRequest(
            device_id="kyle", claim_code=claim["data"]["claim_code"]
        )))
        self.assertEqual(claimed["code"], 200)
        new_token = claimed["data"]["access_token"]
        self.assertNotEqual(new_token, self.device_token)

        with sqlite3.connect(main.DB_FILE) as conn:
            self.assertIsNone(main.authenticate_device_token(conn.cursor(), self.authorization))
            identity = main.authenticate_device_token(conn.cursor(), f"Bearer {new_token}")
        self.assertEqual(identity["device_id"], "kyle")

    def test_new_device_registration_issues_token_once(self):
        registered = run_endpoint(main.register_device(
            main.RegisterDeviceRequest(device_id="new-installation")
        ))
        self.assertEqual(registered["code"], 200)
        token = registered["data"]["access_token"]
        self.assertTrue(token.startswith("vpn_"))

        duplicate = run_endpoint(main.register_device(
            main.RegisterDeviceRequest(device_id="new-installation")
        ))
        self.assertEqual(duplicate["code"], 409)

    def test_admin_api_fails_closed_without_configured_key(self):
        request = main.Request({
            "type": "http",
            "method": "GET",
            "path": "/api/admin/users",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 12345),
        })
        original_key = main.ADMIN_API_KEY
        original_enabled = main.ADMIN_AUTH_ENABLED
        main.ADMIN_API_KEY = ""
        main.ADMIN_AUTH_ENABLED = True
        try:
            response = run_endpoint(main.protect_admin_api(request, None))
        finally:
            main.ADMIN_API_KEY = original_key
            main.ADMIN_AUTH_ENABLED = original_enabled
        self.assertEqual(response.status_code, 503)

    def test_internal_admin_api_can_disable_key_authentication(self):
        request = main.Request({
            "type": "http", "method": "GET", "path": "/api/admin/users",
            "headers": [], "query_string": b"", "scheme": "http",
            "server": ("internal", 80), "client": ("10.0.0.2", 12345),
        })

        async def next_handler(_request):
            return "passed"

        original_enabled = main.ADMIN_AUTH_ENABLED
        main.ADMIN_AUTH_ENABLED = False
        try:
            response = run_endpoint(main.protect_admin_api(request, next_handler))
        finally:
            main.ADMIN_AUTH_ENABLED = original_enabled
        self.assertEqual(response, "passed")

    def test_admin_can_set_and_clear_user_remark(self):
        saved = run_endpoint(main.set_user_remark(main.SetUserRemarkRequest(
            device_id="kyle", remark="测试客户"
        )))
        self.assertEqual(saved["code"], 200)
        user = run_endpoint(main.get_all_users(uid="100001"))["data"][0]
        self.assertEqual(user["remark"], "测试客户")

        run_endpoint(main.set_user_remark(main.SetUserRemarkRequest(
            device_id="kyle", remark=""
        )))
        user = run_endpoint(main.get_all_users(uid="100001"))["data"][0]
        self.assertEqual(user["remark"], "")

    def create_residential_egress(self):
        response = run_endpoint(main.create_residential_egress(main.ResidentialEgressRequest(
            name="日本住宅 01",
            protocol="socks",
            host="isp.example.test",
            port=8001,
            username="proxy-user",
            password="proxy-secret",
            country="日本",
        )))
        self.assertEqual(response["code"], 200)
        return response["data"]["id"]

    def test_residential_password_is_encrypted_and_never_returned(self):
        egress_id = self.create_residential_egress()
        with sqlite3.connect(main.DB_FILE) as conn:
            ciphertext = conn.execute(
                "SELECT password_ciphertext FROM residential_egresses WHERE id = ?",
                (egress_id,)
            ).fetchone()[0]
        self.assertNotEqual(ciphertext, "proxy-secret")
        self.assertEqual(main.decrypt_proxy_password(ciphertext), "proxy-secret")

        listed = run_endpoint(main.get_residential_egresses())["data"][0]
        self.assertNotIn("password", listed)
        self.assertNotIn("password_ciphertext", listed)
        self.assertTrue(listed["password_set"])

    def test_residential_binding_syncs_xray_and_is_visible_on_user(self):
        egress_id = self.create_residential_egress()
        with sqlite3.connect(main.DB_FILE) as conn:
            conn.execute(
                "UPDATE residential_egresses SET status='available' WHERE id=?", (egress_id,)
            )

        sync_mock = AsyncMock(return_value=(True, "applied"))
        with patch("main.apply_residential_routing", sync_mock):
            response = run_endpoint(main.bind_residential_egress(
                main.BindResidentialEgressRequest(
                    device_id="kyle", residential_egress_id=egress_id
                )
            ))
        self.assertEqual(response["code"], 200)
        assignments = sync_mock.await_args.args[0]
        self.assertEqual(assignments[0]["device_id"], "kyle")
        self.assertEqual(assignments[0]["password"], "proxy-secret")

        user = run_endpoint(main.get_all_users(uid="100001"))["data"][0]
        self.assertEqual(user["residential_egress_id"], egress_id)
        self.assertEqual(user["residential_egress_name"], "日本住宅 01")

    def test_residential_binding_rolls_back_when_xray_update_fails(self):
        egress_id = self.create_residential_egress()
        with sqlite3.connect(main.DB_FILE) as conn:
            conn.execute(
                "UPDATE residential_egresses SET status='available' WHERE id=?", (egress_id,)
            )

        with patch(
            "main.apply_residential_routing",
            AsyncMock(return_value=(False, "xray rejected config")),
        ):
            response = run_endpoint(main.bind_residential_egress(
                main.BindResidentialEgressRequest(
                    device_id="kyle", residential_egress_id=egress_id
                )
            ))
        self.assertEqual(response.status_code, 502)
        with sqlite3.connect(main.DB_FILE) as conn:
            assigned = conn.execute(
                "SELECT residential_egress_id FROM users WHERE device_id='kyle'"
            ).fetchone()[0]
        self.assertIsNone(assigned)

    def test_xray_sync_preserves_existing_config_and_inserts_user_rule(self):
        original_setting = {
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "managed-residential-old", "protocol": "http"},
            ],
            "routing": {"rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                {"type": "field", "user": ["old"], "outboundTag": "managed-residential-old"},
            ]},
        }

        def response(payload):
            result = MagicMock(status_code=200, text="")
            result.json.return_value = payload
            return result

        request_mock = AsyncMock(side_effect=[
            response({
                "success": True,
                "obj": json.dumps({"xraySetting": original_setting}),
            }),
            response({"success": True, "obj": None}),
        ])
        fake_client = MagicMock()
        fake_client.aclose = AsyncMock()
        assignment = {
            "id": "new01", "protocol": "socks", "host": "isp.example.test",
            "port": 8001, "username": "proxy-user", "password": "proxy-secret",
            "device_id": "kyle",
        }
        with patch("xui_client._build_client", return_value=fake_client), patch(
            "xui_client._request_with_retry", request_mock
        ):
            result = run_endpoint(xui_client.apply_residential_routing([assignment]))
        self.assertTrue(result[0])

        saved = json.loads(request_mock.await_args_list[1].kwargs["data"]["xraySetting"])
        self.assertEqual(
            [item["tag"] for item in saved["outbounds"]],
            ["direct", "managed-residential-new01"],
        )
        rules = saved["routing"]["rules"]
        self.assertEqual(rules[0]["outboundTag"], "api")
        self.assertEqual(rules[1]["user"], ["kyle"])
        self.assertEqual(rules[1]["network"], "tcp")
        self.assertEqual(rules[2]["outboundTag"], "direct")


def tearDownModule():
    os.chdir(PROJECT_DIR)
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
