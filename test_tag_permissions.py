import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
TEMP_DIR = Path(tempfile.mkdtemp())
os.chdir(TEMP_DIR)
sys.path.insert(0, str(PROJECT_DIR))

import main  # noqa: E402  (切换到临时数据库目录后再导入)


def run_endpoint(coroutine):
    """这些接口内部没有异步 I/O，可直接推进协程，避免测试环境创建网络事件循环。"""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("接口意外产生了异步挂起")


class TagPermissionTests(unittest.TestCase):
    def setUp(self):
        main.DB_FILE = str(TEMP_DIR / f"test_{self._testMethodName}.db")
        main.init_db()
        expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(main.DB_FILE) as conn:
            conn.execute(
                "INSERT INTO users (device_id, expire_time, uid) VALUES (?, ?, ?)",
                ("kyle", expiry, "100001")
            )

        for node_id, tags in (("public", []), ("node_a", ["a"]), ("node_b", ["b"])):
            response = run_endpoint(main.add_node(main.AddNodeRequest(
                node_id=node_id,
                remark=node_id,
                config_json='{"server":"127.0.0.1"}',
                tags=tags,
            )))
            self.assertEqual(response["code"], 200)

    def node_ids(self, device_id=None):
        response = run_endpoint(main.list_available_nodes(device_id=device_id, x_device_id=None))
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
        denied = run_endpoint(main.get_node(main.NodeRequest(device_id="kyle", node_id="node_b")))
        self.assertEqual(denied["code"], 404)

        allowed = run_endpoint(main.get_node(main.NodeRequest(device_id="kyle", node_id="node_a")))
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


def tearDownModule():
    os.chdir(PROJECT_DIR)
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
