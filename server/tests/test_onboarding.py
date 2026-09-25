"""The getting-started guide's state, and pairing codes: the short code a
device types in place of the long setup code."""

import base64
import json
import os
import threading

import helpers
from helpers import ss
from blockslot_server import webapp
from test_garage import FakeAdmin, FakeCloudflare
from test_http import Client


class Clock(object):
    def __init__(self, start=1_800_000_000.0):
        self.t = start

    def __call__(self):
        return self.t


class Onboarding(helpers.StoreCase):
    def app(self):
        return webapp.App(self.dir, store=self.store, admin=FakeAdmin())

    def server_json(self):
        with open(os.path.join(self.dir, "server.json")) as handle:
            return json.load(handle)

    def test_defaults_on_a_new_install(self):
        app = self.app()
        state = app.onboarding()
        self.assertEqual((state["done"], state["step"]), (False, "account"))
        self.assertEqual(state["steps"], ["account", "address", "emulators", "device", "done"])
        app.create_first_user("player", "a long password")
        self.assertEqual(self.server_json()["onboarding"]["step"], "address")
        state = app.onboarding()
        self.assertEqual((state["done"], state["step"], state["existing"]),
                         (False, "address", False))

    def test_account_but_nothing_else_still_gets_the_guide(self):
        # Made the account on an older version, then nothing: still a new install.
        app = self.app()
        app.config.update(lambda d: d.__setitem__("users", {"player": {"hash": "x"}}))
        state = app.onboarding()
        self.assertEqual((state["done"], state["step"]), (False, "address"))
        self.assertNotIn("onboarding", self.server_json())

    def test_existing_install_with_saves_is_done(self):
        self.play("deck", b"a save")
        app = self.app()
        app.config.update(lambda d: d.__setitem__("users", {"player": {"hash": "x"}}))
        state = app.onboarding()
        self.assertEqual((state["done"], state["existing"]), (True, True))
        self.assertTrue(self.server_json()["onboarding"]["done"])     # decided once, kept

    def test_existing_install_with_devices_is_done(self):
        app = self.app()
        app.config.update(lambda d: d.update({"users": {"player": {"hash": "x"}},
                                              "devices": {"deck": {"key_id": "GK1"}}}))
        self.assertTrue(app.onboarding()["done"])

    def test_store_down_never_pushes_the_guide(self):
        app = webapp.App(self.dir)          # no store, no S3 settings: 503
        app.config.update(lambda d: d.__setitem__("users", {"player": {"hash": "x"}}))
        self.assertTrue(app.onboarding()["done"])
        self.assertNotIn("onboarding", self.server_json())            # asked again later

    def test_reopening_and_persistence(self):
        self.play("deck", b"a save")
        app = self.app()
        app.config.update(lambda d: d.__setitem__("users", {"player": {"hash": "x"}}))
        self.assertTrue(app.onboarding()["done"])
        state = app.set_onboarding({"step": "device"})
        self.assertEqual((state["done"], state["step"]), (True, "device"))
        state = app.set_onboarding({"done": False})
        self.assertEqual((state["done"], state["step"]), (False, "device"))
        again = webapp.App(self.dir, store=self.store).onboarding()
        self.assertEqual((again["done"], again["step"]), (False, "device"))
        self.assertTrue(app.set_onboarding({"step": "done"})["done"])
        with self.assertRaises(webapp.HttpError):
            app.set_onboarding({"step": "nowhere"})
        with self.assertRaises(webapp.HttpError):
            app.set_onboarding({"done": "yes"})


class Pairing(helpers.StoreCase):
    def setUp(self):
        helpers.StoreCase.setUp(self)
        self.clock = Clock()
        self.app = webapp.App(self.dir, store=self.store, admin=FakeAdmin(),
                              cloudflare_factory=FakeCloudflare, now=self.clock)

    def test_code_format_and_response_shape(self):
        got = self.app.add_device("Steam Deck", host_header="nas.lan:8761")
        code = got["pair_code"]
        self.assertRegex(code, r"^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")
        for bad in "01OIL":
            self.assertNotIn(bad, code)
        self.assertEqual(ss.parse_iso(got["pair_expires"]).timestamp(), self.clock.t + 15 * 60)
        answer = self.app.pair(code.lower().replace("-", " "), "10.0.0.5")
        self.assertEqual(answer["setup"], json.loads(base64.b64decode(got["setup_code"])))
        self.assertEqual(sorted(answer["setup"]), ["access_key", "bucket", "device", "endpoint",
                                                   "region", "secret_key", "type"])

    def test_cloudflare_fields_come_along(self):
        self.app.set_cloudflare_settings({"api_token": "t", "account_id": "a", "app_id": "p"})
        got = self.app.add_device("laptop")
        setup = self.app.pair(got["pair_code"], "10.0.0.5")["setup"]
        self.assertEqual((setup["cf_client_id"], setup["cf_client_secret"]),
                         ("cid-laptop", "csec-laptop"))

    def test_single_use(self):
        code = self.app.add_device("deck")["pair_code"]
        self.app.pair(code, "10.0.0.5")
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.pair(code, "10.0.0.5")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.message, webapp.PAIR_INVALID)

    def test_expiry(self):
        code = self.app.add_device("deck")["pair_code"]
        self.clock.t += 15 * 60 + 1
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.pair(code, "10.0.0.5")
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(self.app.pairings.pending)          # the secret is gone too

    def test_only_a_hash_is_kept(self):
        got = self.app.add_device("deck")
        raw = webapp.normalize_pair_code(got["pair_code"])
        self.assertEqual(list(self.app.pairings.pending), [webapp.pair_hash(raw)])
        self.assertNotIn(raw, repr(self.app.pairings.pending))
        with open(os.path.join(self.dir, "server.json")) as handle:
            text = handle.read()
        self.assertNotIn(raw, text)
        self.assertNotIn("secret-for-", text)

    def test_new_code_replaces_the_old_and_needs_the_secret(self):
        first = self.app.add_device("deck")["pair_code"]
        again = self.app.new_pair_code("deck")
        self.assertEqual(len(self.app.pairings.pending), 1)
        with self.assertRaises(webapp.HttpError):
            self.app.pair(first, "10.0.0.5")
        self.assertEqual(self.app.pair(again["pair_code"], "10.0.0.5")["setup"]["device"], "deck")
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.new_pair_code("deck")                   # spent: the secret is gone
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.new_pair_code("nobody")
        self.assertEqual(caught.exception.status, 404)

    def test_removing_the_device_drops_its_code(self):
        code = self.app.add_device("deck")["pair_code"]
        self.app.remove_device("deck")
        with self.assertRaises(webapp.HttpError):
            self.app.pair(code, "10.0.0.5")

    def test_rate_limit(self):
        code = self.app.add_device("deck")["pair_code"]
        for _ in range(10):
            with self.assertRaises(webapp.HttpError):
                self.app.pair("AAAA-AAAA", "10.0.0.9")
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.pair(code, "10.0.0.9")                  # even the right one, for now
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.app.pair(code, "10.0.0.10")["setup"]["device"], "deck")
        self.clock.t += 15 * 60 + 1
        with self.assertRaises(webapp.HttpError) as caught:
            self.app.pair("AAAA-AAAA", "10.0.0.9")
        self.assertEqual(caught.exception.status, 404)       # the wait is over


class OverHttp(helpers.StoreCase):
    def setUp(self):
        helpers.StoreCase.setUp(self)
        self.app = webapp.App(self.dir, store=self.store, admin=FakeAdmin())
        self.server = webapp.make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        helpers.StoreCase.tearDown(self)

    def signed_in(self):
        client = Client(self.port)
        status, body = client.call("POST", "/api/setup", {"username": "player",
                                                          "password": "a long password"})
        self.assertEqual(status, 200)
        client.csrf = body["csrf"]
        return client

    def test_onboarding_needs_a_session_and_the_token(self):
        stranger = Client(self.port)
        self.assertEqual(stranger.call("GET", "/api/onboarding")[0], 401)
        self.assertEqual(stranger.call("PUT", "/api/onboarding", {"step": "done"})[0], 401)
        client = self.signed_in()
        status, body = client.call("GET", "/api/onboarding")
        self.assertEqual((status, body["done"], body["step"]), (200, False, "address"))
        self.assertEqual(body["public_endpoint"], "http://127.0.0.1:3900")
        self.assertFalse(body["public_endpoint_set"])
        token, client.csrf = client.csrf, None
        self.assertEqual(client.call("PUT", "/api/onboarding", {"step": "device"})[0], 403)
        client.csrf = token
        self.assertEqual(client.call("PUT", "/api/onboarding", {"step": "device"},
                                     headers={"Origin": "https://evil.example"})[0], 403)
        status, body = client.call("PUT", "/api/onboarding", {"step": "device"})
        self.assertEqual((status, body["step"]), (200, "device"))
        self.assertEqual(client.call("GET", "/api/onboarding")[1]["step"], "device")
        self.assertEqual(client.call("PUT", "/api/onboarding", {"step": "moon"})[0], 400)

    def test_pair_skips_origin_and_sets_no_cookie(self):
        client = self.signed_in()
        status, made = client.call("POST", "/api/devices", {"name": "Steam Deck"})
        self.assertEqual(status, 200)
        self.assertIn("pair_code", made)
        self.assertIn("pair_expires", made)
        rows = {r["device"]: r for r in client.call("GET", "/api/devices")[1]["devices"]}
        self.assertTrue(rows["steam-deck"]["pair_pending"])
        device = Client(self.port)
        status, body = device.call("POST", "/api/pair", {"code": "zzzz-zzzz"},
                                   headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], webapp.PAIR_INVALID)
        response, payload = device.call("POST", "/api/pair", {"code": made["pair_code"]},
                                        headers={"Origin": "https://elsewhere.example"}, raw=True)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertEqual(json.loads(payload)["setup"],
                         json.loads(base64.b64decode(made["setup_code"])))
        # A browser session's cookie is not even read.
        status, _ = client.call("POST", "/api/pair", {"code": made["pair_code"]})
        self.assertEqual(status, 404)
        status, _ = client.call("POST", "/api/devices/steam-deck/pair-code", {})
        self.assertEqual(status, 409)

    def test_pair_code_button_route_needs_a_session(self):
        self.assertEqual(Client(self.port).call("POST", "/api/devices/deck/pair-code", {})[0], 401)

    def test_pair_rate_limit_over_http(self):
        device = Client(self.port)
        for _ in range(10):
            self.assertEqual(device.call("POST", "/api/pair", {"code": "AAAA-AAAA"})[0], 404)
        status, body = device.call("POST", "/api/pair", {"code": "AAAA-AAAA"})
        self.assertEqual(status, 429)
        self.assertIn("Too many", body["error"])


if __name__ == "__main__":
    import unittest
    unittest.main()
