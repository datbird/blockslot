"""Pairing with a BlockSlot server: storecheck.pair against a local fake.

The fake answers /api/pair the way the server does: the setup once for a good
code, 404 with a reason for a bad one, 429 when rate limited.
"""

import http.server
import json
import threading
import unittest

from gui.core import storecheck

SETUP = {"type": "s3", "endpoint": "https://saves.example.com", "bucket": "blockslot",
         "region": "garage", "access_key": "GKdemo", "secret_key": "demo-secret",
         "device": "steam-deck"}


class FakeServer(object):
    def __init__(self):
        self.codes = {"K7QX4MPA": dict(SETUP)}
        self.seen = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append((self.path, body, self.headers.get("User-Agent")))
                if body.get("code") == "SLOWDOWN":
                    return self.answer(429, {"error": "Too many tries. Wait 15 minutes."})
                setup = fake.codes.pop(body.get("code"), None)
                if self.path != "/api/pair" or setup is None:
                    return self.answer(404, {"error": "That pairing code is not valid. "
                                                      "Make a new one on the Devices page."})
                self.answer(200, {"setup": setup})

            def answer(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.address = "127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Pairing(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()

    def tearDown(self):
        self.server.close()

    def test_a_good_code_gives_the_store_section_once(self):
        values = storecheck.pair(self.server.address, "k7qx-4mpa")
        self.assertEqual(values, SETUP)
        path, body, agent = self.server.seen[0]
        self.assertEqual((path, body), ("/api/pair", {"code": "K7QX4MPA"}))
        self.assertTrue(agent.startswith("BlockSlot/"))
        with self.assertRaises(ValueError) as caught:
            storecheck.pair(self.server.address, "K7QX 4MPA")
        self.assertIn("not valid", str(caught.exception))

    def test_the_servers_reason_is_shown(self):
        with self.assertRaises(ValueError) as caught:
            storecheck.pair("http://" + self.server.address + "/", "SLOW-DOWN")
        self.assertIn("Too many tries", str(caught.exception))

    def test_a_code_of_the_wrong_length_never_leaves_the_device(self):
        with self.assertRaises(ValueError) as caught:
            storecheck.pair(self.server.address, "K7QX")
        self.assertIn("8 letters", str(caught.exception))
        self.assertEqual(self.server.seen, [])

    def test_nothing_listening(self):
        with self.assertRaises(ValueError) as caught:
            storecheck.pair("127.0.0.1:1", "K7QX-4MPA")
        self.assertIn("Cannot reach http://127.0.0.1:1", str(caught.exception))

    def test_an_answer_without_a_store(self):
        self.server.codes["BADSETUP"] = {"type": "s3", "endpoint": "x"}
        with self.assertRaises(ValueError) as caught:
            storecheck.pair(self.server.address, "BAD-SETUP")
        self.assertIn("has no bucket", str(caught.exception))

    def test_addresses_as_people_type_them(self):
        self.assertEqual(storecheck.server_address(" 192.168.1.20:8761/ "),
                         "http://192.168.1.20:8761")
        self.assertEqual(storecheck.server_address("https://bs.example.com"),
                         "https://bs.example.com")
        with self.assertRaises(ValueError):
            storecheck.server_address("  ")


if __name__ == "__main__":
    unittest.main()
