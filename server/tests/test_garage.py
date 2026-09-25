"""Garage's config and admin API, and Cloudflare's service tokens, with mocked HTTP."""

import base64
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error

import helpers  # noqa: F401  (import path)
from blockslot_server import garage, webapp
from blockslot_server.cloudflare import Cloudflare, CloudflareError


class Response(object):
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status

    def read(self):
        return self.body


class FakeHTTP(object):
    """Answers requests from a table of (method, path prefix) -> body or status.

    Records every call as (method, url, json body) so a test can check the
    exact requests.
    """

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), request.full_url, body,
                           dict(request.header_items())))
        for (method, prefix), answer in self.table.items():
            if request.get_method() == method and prefix in request.full_url:
                if callable(answer):
                    answer = answer(body)
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(request.full_url, answer, "err", {},
                                                 io.BytesIO(b'{"message":"no"}'))
                return Response(answer)
        raise AssertionError("unexpected request %s %s" % (request.get_method(), request.full_url))

    def names(self):
        return [(m, u.split("/v2/")[-1] if "/v2/" in u else u) for m, u, _b, _h in self.calls]


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_new_config(self):
        path = os.path.join(self.dir, "garage.toml")
        garage.write_new_config(path, "/data", s3_port=3900, admin_port=3903)
        parsed = garage.read_config(path)
        self.assertEqual(parsed["metadata_dir"], "/data/meta")
        self.assertEqual(parsed["s3_api"]["s3_region"], "garage")
        self.assertEqual(parsed["admin"]["api_bind_addr"], "127.0.0.1:3903")
        self.assertEqual(len(parsed["rpc_secret"]), 64)
        self.assertGreater(len(parsed["admin"]["admin_token"]), 30)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        other = garage.new_config("/data")
        self.assertNotEqual(garage.tomllib.loads(other)["rpc_secret"], parsed["rpc_secret"])
        with self.assertRaises(FileExistsError):
            garage.write_new_config(path, "/data")          # never overwrites

    def test_adopting_a_plain_garage_layout(self):
        """The existing garage.toml names the old container's paths."""
        os.makedirs(os.path.join(self.dir, "meta"))
        os.makedirs(os.path.join(self.dir, "data"))
        text = "\n".join([
            "# Blockslot save store.",
            'metadata_dir = "/var/lib/garage/meta"',
            'data_dir = "/var/lib/garage/data"',
            'db_engine = "sqlite"',
            "replication_factor = 1",
            'rpc_bind_addr = "[::]:3901"',
            'rpc_secret = "%s"' % ("ab" * 32),
            "[s3_api]",
            's3_region = "garage"',
            'api_bind_addr = "[::]:3900"',
            "[admin]",
            'api_bind_addr = "[::]:3903"',
            'admin_token = "test-token"', ""])
        runtime, port = garage.runtime_config(text, self.dir)
        parsed = garage.tomllib.loads(runtime)
        self.assertEqual(port, 3903)
        self.assertEqual(parsed["metadata_dir"], os.path.join(self.dir, "meta"))
        self.assertEqual(parsed["data_dir"], os.path.join(self.dir, "data"))
        self.assertEqual(parsed["admin"]["api_bind_addr"], "127.0.0.1:3903")
        self.assertEqual(parsed["s3_api"]["api_bind_addr"], "[::]:3900")
        self.assertEqual(parsed["rpc_secret"], "ab" * 32)
        self.assertIn("# Blockslot save store.", runtime)
        self.assertEqual(garage.admin_token(text), "test-token")
        self.assertEqual(garage.s3_port(text), 3900)

    def test_paths_that_exist_are_kept(self):
        text = 'metadata_dir = "%s"\ndata_dir = "%s"\n[admin]\napi_bind_addr = "[::]:4000"\n' % (
            self.dir, self.dir)
        runtime, port = garage.runtime_config(text, "/nowhere")
        self.assertEqual(garage.tomllib.loads(runtime)["metadata_dir"], self.dir)
        self.assertEqual(port, 4000)


class Admin(unittest.TestCase):
    def admin(self, table):
        fake = FakeHTTP(table)
        return garage.GarageAdmin("http://127.0.0.1:3903", "tok", opener=fake), fake

    def test_bearer_token(self):
        admin, fake = self.admin({("GET", "GetClusterStatus"): {"nodes": []}})
        admin.status()
        self.assertEqual(fake.calls[0][3]["Authorization"], "Bearer tok")

    def test_layout_assigned_once(self):
        status = {"nodes": [{"id": "node1", "role": None}]}
        admin, fake = self.admin({
            ("GET", "GetClusterStatus"): lambda _b: status,
            ("GET", "GetClusterLayout"): {"version": 0, "roles": [], "stagedRoleChanges": []},
            ("POST", "UpdateClusterLayout"): {},
            ("POST", "ApplyClusterLayout"): {}})
        self.assertTrue(admin.ensure_layout(10 ** 12))
        update = [c for c in fake.calls if c[1].endswith("UpdateClusterLayout")][0][2]
        self.assertEqual(update["roles"][0]["id"], "node1")
        self.assertEqual(update["roles"][0]["capacity"], 10 ** 12)
        apply = [c for c in fake.calls if c[1].endswith("ApplyClusterLayout")][0][2]
        self.assertEqual(apply, {"version": 1})
        status["nodes"][0]["role"] = {"zone": "dc1"}
        fake.calls.clear()
        self.assertFalse(admin.ensure_layout(10 ** 12))
        self.assertEqual(fake.names(), [("GET", "GetClusterStatus")])

    def test_bucket_found_or_made(self):
        admin, fake = self.admin({("GET", "GetBucketInfo"): {"id": "b1", "bytes": 5, "objects": 2}})
        self.assertEqual(admin.ensure_bucket(), ("b1", False))
        self.assertIn("globalAlias=blockslot", fake.calls[0][1])
        admin, fake = self.admin({("GET", "GetBucketInfo"): 404,
                                  ("POST", "CreateBucket"): {"id": "b2"}})
        self.assertEqual(admin.ensure_bucket(), ("b2", True))
        self.assertEqual(fake.calls[1][2], {"globalAlias": "blockslot"})

    def test_create_key_allows_the_bucket(self):
        admin, fake = self.admin({
            ("POST", "CreateKey"): {"accessKeyId": "GKabc", "secretAccessKey": "sec"},
            ("POST", "AllowBucketKey"): {}})
        self.assertEqual(admin.create_key("blockslot-deck", "b1"), ("GKabc", "sec"))
        self.assertEqual(fake.calls[0][2], {"name": "blockslot-deck"})
        self.assertEqual(fake.calls[1][2], {"bucketId": "b1", "accessKeyId": "GKabc",
                                            "permissions": {"read": True, "write": True,
                                                            "owner": False}})

    def test_failed_allow_removes_the_key(self):
        admin, fake = self.admin({
            ("POST", "CreateKey"): {"accessKeyId": "GKabc", "secretAccessKey": "sec"},
            ("POST", "AllowBucketKey"): 500, ("POST", "DeleteKey"): {}})
        with self.assertRaises(garage.GarageError):
            admin.create_key("blockslot-deck", "b1")
        self.assertIn("DeleteKey?id=GKabc", fake.calls[-1][1])

    def test_delete_missing_key_is_fine(self):
        admin, _fake = self.admin({("POST", "DeleteKey"): 404})
        admin.delete_key("GKgone")
        admin, _fake = self.admin({("POST", "DeleteKey"): 500})
        with self.assertRaises(garage.GarageError):
            admin.delete_key("GKx")


class CloudflareTokens(unittest.TestCase):
    APP = "/accounts/acc/access/apps/app1"

    def policy_app(self, reusable=True):
        return {"success": True, "result": {"id": "app1", "policies": [
            {"id": "p-allow", "decision": "allow", "include": [{"email": {"email": "a@b.c"}}]},
            {"id": "p-svc", "name": "Blockslot devices", "decision": "non_identity",
             "reusable": reusable, "precedence": 2,
             "include": [{"service_token": {"token_id": "old-token"}}]}]}}

    def test_add_device_makes_token_and_admits_it(self):
        fake = FakeHTTP({
            ("POST", "/access/service_tokens"): {"success": True, "result": {
                "id": "tok-9", "client_id": "cid.access", "client_secret": "csecret"}},
            ("GET", self.APP): self.policy_app(),
            ("PUT", "/accounts/acc/access/policies/p-svc"): {"success": True, "result": {}}})
        cf = Cloudflare("api-token", "acc", "app1", opener=fake)
        self.assertEqual(cf.add_device("deck"), ("tok-9", "cid.access", "csecret"))
        method, url, body, headers = fake.calls[0]
        self.assertEqual(body, {"name": "blockslot-deck", "duration": "8760h"})
        self.assertEqual(headers["Authorization"], "Bearer api-token")
        put = fake.calls[-1][2]
        self.assertEqual(put["decision"], "non_identity")
        self.assertEqual(put["include"], [{"service_token": {"token_id": "old-token"}},
                                          {"service_token": {"token_id": "tok-9"}}])

    def test_app_scoped_policy_path(self):
        fake = FakeHTTP({
            ("POST", "/access/service_tokens"): {"success": True, "result": {
                "id": "tok-9", "client_id": "c", "client_secret": "s"}},
            ("PUT", self.APP + "/policies/p-svc"): {"success": True, "result": {}},
            ("GET", self.APP): self.policy_app(reusable=False)})
        Cloudflare("t", "acc", "app1", opener=fake).add_device("deck")
        self.assertTrue(fake.calls[-1][1].endswith(self.APP + "/policies/p-svc"))
        self.assertEqual(fake.calls[-1][2]["precedence"], 2)

    def test_policy_failure_deletes_the_token(self):
        fake = FakeHTTP({
            ("POST", "/access/service_tokens"): {"success": True, "result": {
                "id": "tok-9", "client_id": "c", "client_secret": "s"}},
            ("GET", self.APP): {"success": True, "result": {"policies": []}},
            ("DELETE", "/access/service_tokens/tok-9"): {"success": True, "result": {}}})
        with self.assertRaises(CloudflareError):
            Cloudflare("t", "acc", "app1", opener=fake).add_device("deck")
        self.assertEqual(fake.calls[-1][0], "DELETE")

    def test_remove_device(self):
        app = self.policy_app()
        app["result"]["policies"][1]["include"].append({"service_token": {"token_id": "tok-9"}})
        fake = FakeHTTP({
            ("GET", self.APP): app,
            ("PUT", "/accounts/acc/access/policies/p-svc"): {"success": True, "result": {}},
            ("DELETE", "/access/service_tokens/tok-9"): {"success": True, "result": {}}})
        Cloudflare("t", "acc", "app1", opener=fake).remove_device("tok-9")
        put = [c for c in fake.calls if c[0] == "PUT"][0][2]
        self.assertEqual(put["include"], [{"service_token": {"token_id": "old-token"}}])
        self.assertEqual(fake.calls[-1][0], "DELETE")

    def test_refusal_is_an_error_with_the_reason(self):
        fake = FakeHTTP({("POST", "/access/service_tokens"): {
            "success": False, "errors": [{"message": "Authentication error"}]}})
        with self.assertRaises(CloudflareError) as caught:
            Cloudflare("t", "acc", "app1", opener=fake).add_device("deck")
        self.assertIn("Authentication error", str(caught.exception))


class FakeAdmin(object):
    """GarageAdmin's methods the web app uses, recording what it was asked."""

    def __init__(self):
        self.keys = {}
        self.deleted = []

    def bucket_id(self, alias):
        return "bucket-1"

    def create_key(self, name, bucket_id, owner=False):
        key_id = "GK%04d" % (len(self.keys) + 1)
        self.keys[key_id] = (name, bucket_id)
        return key_id, "secret-for-" + key_id

    def delete_key(self, key_id):
        self.deleted.append(key_id)

    def bucket_info(self, alias):
        return {"bytes": 1234, "objects": 7}


class FakeCloudflare(object):
    made = []
    removed = []

    def __init__(self, token, account, app):
        self.args = (token, account, app)

    def add_device(self, device):
        FakeCloudflare.made.append(device)
        return "tok-" + device, "cid-" + device, "csec-" + device

    def remove_device(self, token_id):
        FakeCloudflare.removed.append(token_id)


class Devices(helpers.StoreCase):
    def app(self):
        return webapp.App(self.dir, store=self.store, admin=FakeAdmin(),
                          cloudflare_factory=FakeCloudflare)

    def test_setup_code(self):
        code = webapp.setup_code("http://nas:3900", "GK1", "sec", "deck")
        body = json.loads(base64.b64decode(code))
        self.assertEqual(body, {"type": "s3", "endpoint": "http://nas:3900", "bucket": "blockslot",
                                "region": "garage", "access_key": "GK1", "secret_key": "sec",
                                "device": "deck"})
        self.assertNotIn("\n", code)
        with_cf = json.loads(base64.b64decode(webapp.setup_code("e", "a", "s", "d", cf=("i", "c"))))
        self.assertEqual((with_cf["cf_client_id"], with_cf["cf_client_secret"]), ("i", "c"))

    def test_add_and_remove_device(self):
        app = self.app()
        got = app.add_device("Steam Deck", host_header="nas.lan:8761")
        body = json.loads(base64.b64decode(got["setup_code"]))
        self.assertEqual(body["device"], "steam-deck")
        self.assertEqual(body["endpoint"], "http://nas.lan:3900")
        self.assertEqual(body["access_key"], "GK0001")
        self.assertNotIn("cf_client_id", body)
        self.assertEqual(app.admin.keys["GK0001"], ("blockslot-steam-deck", "bucket-1"))
        doc = json.loads(self.store.get(helpers.ss.PREFIX + "config/devices/steam-deck.json"))
        self.assertEqual(doc["name"], "Steam Deck")
        rows = {row["device"]: row for row in app.devices()}
        self.assertTrue(rows["steam-deck"]["has_key"])
        with self.assertRaises(webapp.HttpError):
            app.add_device("steam deck")                     # same id, still has a key
        app.remove_device("steam-deck")
        self.assertEqual(app.admin.deleted, ["GK0001"])
        self.assertFalse({row["device"]: row for row in app.devices()}["steam-deck"]["has_key"])
        with self.assertRaises(webapp.HttpError):
            app.remove_device("steam-deck")

    def test_public_endpoint_setting(self):
        app = self.app()
        app.set_public_endpoint("https://saves.example.com/")
        body = json.loads(base64.b64decode(app.add_device("deck")["setup_code"]))
        self.assertEqual(body["endpoint"], "https://saves.example.com")
        with self.assertRaises(webapp.HttpError):
            app.set_public_endpoint("not a url")

    def test_cloudflare_device(self):
        FakeCloudflare.made, FakeCloudflare.removed = [], []
        app = self.app()
        app.set_cloudflare_settings({"api_token": "t", "account_id": "acc", "app_id": "app1"})
        self.assertEqual(app.cloudflare_settings(), {"api_token_set": True, "account_id": "acc",
                                                     "app_id": "app1"})
        body = json.loads(base64.b64decode(app.add_device("laptop")["setup_code"]))
        self.assertEqual((body["cf_client_id"], body["cf_client_secret"]),
                         ("cid-laptop", "csec-laptop"))
        app.remove_device("laptop")
        self.assertEqual(FakeCloudflare.removed, ["tok-laptop"])

    def test_storage_numbers(self):
        self.play("deck", b"one")
        numbers = self.app().storage()
        self.assertEqual(numbers["bucket"], {"bytes": 1234, "objects": 7})
        self.assertEqual((numbers["games"], numbers["snapshots"]), (1, 1))
        self.assertIsNotNone(numbers["newest_upload"])
        self.assertEqual(numbers["retention"], {"per_device": 10, "days": 30})

    def test_keeper_cleans_once_a_day(self):
        app = self.app()
        self.assertIsNone(app.keeper_tick())                 # not the keeper
        app.set_keeper(True)
        self.assertIsNotNone(app.keeper_tick())
        self.assertIsNone(app.keeper_tick())                 # already today


if __name__ == "__main__":
    unittest.main()
