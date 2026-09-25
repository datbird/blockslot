"""The whole server with a real Garage 2.1.0: first start, the bucket, a device
key made through the admin API, a device-style upload with that key through
slotstore.S3Store, a restore, a removal, and a restart that adopts the data
the way an existing Garage install is adopted.

Runs only when BLOCKSLOT_TEST_GARAGE names a Garage binary:

    BLOCKSLOT_TEST_GARAGE=/path/to/garage python3 -m unittest discover server/tests
"""

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from helpers import GAME, ss
from test_http import Client

GARAGE = os.environ.get("BLOCKSLOT_TEST_GARAGE")
ENTRYPOINT = os.path.join(helpers.SERVER_DIR, "entrypoint.py")


def free_ports(count):
    socks, ports = [], []
    for _ in range(count):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        socks.append(sock)
        ports.append(sock.getsockname()[1])
    for sock in socks:
        sock.close()
    return ports


@unittest.skipUnless(GARAGE and os.path.isfile(GARAGE), "BLOCKSLOT_TEST_GARAGE is not set")
class RealGarage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blockslot-int-")
        self.data = os.path.join(self.dir, "data")
        self.s3, self.rpc, self.admin, self.web = free_ports(4)
        self.proc = None
        self.log = open(os.path.join(self.dir, "server.log"), "wb")

    def tearDown(self):
        self.stop()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self):
        env = dict(os.environ, BLOCKSLOT_DATA=self.data, BLOCKSLOT_RUN=os.path.join(self.dir, "run"),
                   BLOCKSLOT_GARAGE=GARAGE, BLOCKSLOT_S3_PORT=str(self.s3),
                   BLOCKSLOT_RPC_PORT=str(self.rpc), BLOCKSLOT_ADMIN_PORT=str(self.admin),
                   BLOCKSLOT_WEB_PORT=str(self.web), BLOCKSLOT_WEB_HOST="127.0.0.1",
                   BLOCKSLOT_PUBLIC_S3_PORT=str(self.s3))
        self.proc = subprocess.Popen([sys.executable, ENTRYPOINT], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            self.assertIsNone(self.proc.poll(), "the entrypoint exited; see its log")
            try:
                status, body = Client(self.web).call("GET", "/api/session")
                if status == 200:
                    return body
            except OSError:
                pass
            time.sleep(0.3)
        self.fail("the web UI did not come up within 60 s")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.assertEqual(self.proc.wait(timeout=20), 0)
        self.proc = None

    def device_store(self, code):
        body = json.loads(base64.b64decode(code))
        store = ss.store_from_settings(body)
        return body, store

    def upload(self, store, device, save):
        source = tempfile.mkdtemp(dir=self.dir)
        helpers.write(os.path.join(source, "g", "backup-1", "save.sl2"), save)
        helpers.write(os.path.join(source, "g", "mapping.yaml"), "name: g\n")
        state = ss.LocalState(os.path.join(self.dir, "state-" + device))
        manifest = state.stage(GAME, device, source)
        committed, error = ss.drain(store, state)
        if error:
            raise error
        return manifest

    def test_first_start_device_upload_restore_and_adoption(self):
        session = self.start()
        self.assertTrue(session["first_run"])
        toml = os.path.join(self.data, "garage.toml")
        self.assertEqual(os.stat(toml).st_mode & 0o777, 0o600)
        with open(os.path.join(self.data, "server.json")) as handle:
            self.assertIn("access_key", json.load(handle)["s3"])

        client = Client(self.web)
        status, body = client.call("POST", "/api/setup", {"username": "player",
                                                          "password": "a long password"})
        self.assertEqual(status, 200)
        client.csrf = body["csrf"]

        status, made = client.call("POST", "/api/devices", {"name": "deck"})
        self.assertEqual(status, 200, made)
        code, store = self.device_store(made["setup_code"])
        self.assertEqual((code["bucket"], code["region"], code["device"]),
                         ("blockslot", "garage", "deck"))
        self.assertEqual(code["endpoint"], "http://127.0.0.1:%d" % self.s3)

        first = self.upload(store, "deck", b"first save")
        self.upload(store, "deck", b"second save")
        status, body = client.call("GET", "/api/games?refresh=1")
        self.assertEqual([g["title"] for g in body["games"]], [GAME])
        key = body["games"][0]["key"]
        status, restored = client.call("POST", "/api/games/%s/restore" % key,
                                       {"snapshot": first["id"]})
        self.assertEqual(status, 200, restored)
        view = ss.read_game(store, GAME)                      # as the device sees it
        self.assertEqual(view.heads, [restored["id"]])

        status, storage = client.call("GET", "/api/storage")
        self.assertEqual(storage["games"], 1)
        self.assertGreater(storage["bucket"]["objects"], 0)
        self.assertGreater(storage["bucket"]["bytes"], 0)
        status, clean = client.call("POST", "/api/storage/clean", {"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(clean["skipped"] is False and clean["removed_snapshots"] == 0)

        # Central settings reach the store where a device reads them.
        status, _ = client.call("PUT", "/api/settings/shared", {"libraries": {"Bloodborne": {
            "one_game": "Bloodborne", "extensions": "*"}}})
        self.assertEqual(status, 200)
        shared = json.loads(store.get(ss.PREFIX + "config/shared.json"))
        self.assertEqual(shared["libraries"]["Bloodborne"]["one_game"], "Bloodborne")

        # Removing the device revokes its key at once.
        self.assertEqual(client.call("DELETE", "/api/devices/deck")[0], 200)
        with self.assertRaises(ss.StoreRefused):
            self.upload(store, "deck", b"after removal")

        # Adopt: the toml now names the old container's paths, as in a plain Garage install.
        self.stop()
        with open(toml) as handle:
            text = handle.read()
        adopted = text.replace('metadata_dir = "%s/meta"' % self.data,
                               'metadata_dir = "/var/lib/garage/meta"').replace(
            'data_dir = "%s/data"' % self.data, 'data_dir = "/var/lib/garage/data"')
        self.assertNotEqual(adopted, text)
        with open(toml, "w") as handle:
            handle.write(adopted)
        session = self.start()
        self.assertFalse(session["first_run"])
        with open(toml) as handle:
            self.assertEqual(handle.read(), adopted)              # never edited
        client = Client(self.web)
        status, body = client.call("POST", "/api/login", {"username": "player",
                                                          "password": "a long password"})
        client.csrf = body["csrf"]
        status, body = client.call("GET", "/api/games?refresh=1")
        self.assertEqual([g["title"] for g in body["games"]], [GAME])
        status, made = client.call("POST", "/api/devices", {"name": "laptop"})
        _code, laptop = self.device_store(made["setup_code"])
        self.upload(laptop, "laptop", b"laptop save")
        self.stop()


if __name__ == "__main__":
    unittest.main()
