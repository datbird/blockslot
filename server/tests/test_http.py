"""The web app over real HTTP: first run, sign-in, the three gates on every
change (session, same origin, CSRF token), the limiter, Cloudflare Access
sign-in, and the zip download."""

import http.client
import io
import json
import threading
import zipfile

import helpers
from helpers import GAME, ss
from blockslot_server import auth, webapp


class Client(object):
    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def call(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        send = {"Host": "127.0.0.1:%d" % self.port}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            send["Content-Type"] = "application/json"
        if self.cookie:
            send["Cookie"] = "%s=%s" % (auth.SESSION_COOKIE, self.cookie)
        if self.csrf and method != "GET":
            send["X-CSRF-Token"] = self.csrf
        send.update(headers or {})
        conn.request(method, path, body=data, headers=send)
        response = conn.getresponse()
        payload = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie and cookie.startswith(auth.SESSION_COOKIE + "="):
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        conn.close()
        if raw:
            return response, payload
        return response.status, (json.loads(payload) if payload else None)


class Http(helpers.StoreCase):
    def setUp(self):
        helpers.StoreCase.setUp(self)
        self.app = webapp.App(self.dir, store=self.store)
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

    def test_page_and_first_run(self):
        client = Client(self.port)
        response, page = client.call("GET", "/", raw=True)
        self.assertEqual(response.status, 200)
        self.assertIn(b"BlockSlot", page)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(client.call("GET", "/static/app.js", raw=True)[0].status, 200)
        self.assertEqual(client.call("GET", "/static/icon.svg", raw=True)[0].status, 200)
        self.assertEqual(client.call("GET", "/api/session")[1]["first_run"], True)
        status, _ = client.call("POST", "/api/setup", {"username": "player", "password": "short"})
        self.assertEqual(status, 400)
        status, body = client.call("POST", "/api/setup", {"username": "player",
                                                          "password": "a long password"})
        self.assertEqual((status, body["user"]), (200, "player"))
        self.assertIsNotNone(client.cookie)
        # Only once: the next person signs in.
        status, _ = Client(self.port).call("POST", "/api/setup", {"username": "mallory",
                                                                  "password": "a long password"})
        self.assertEqual(status, 409)
        session = client.call("GET", "/api/session")[1]
        self.assertEqual((session["user"], session["first_run"]), ("player", False))

    def test_every_api_route_needs_a_session(self):
        client = Client(self.port)
        for method, path in (("GET", "/api/games"), ("GET", "/api/devices"),
                             ("GET", "/api/storage"), ("GET", "/api/users"),
                             ("PUT", "/api/settings/shared"), ("POST", "/api/devices"),
                             ("GET", "/api/games/x/snapshots/y/zip")):
            with self.subTest(path=path):
                status, _ = client.call(method, path, {} if method != "GET" else None)
                self.assertEqual(status, 401)

    def test_csrf_token_and_origin(self):
        client = self.signed_in()
        token = client.csrf
        client.csrf = None
        status, _ = client.call("PUT", "/api/storage/keeper", {"keeper": True})
        self.assertEqual(status, 403)
        client.csrf = "wrong"
        self.assertEqual(client.call("PUT", "/api/storage/keeper", {"keeper": True})[0], 403)
        client.csrf = token
        status, _ = client.call("PUT", "/api/storage/keeper", {"keeper": True},
                                headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, body = client.call("PUT", "/api/storage/keeper", {"keeper": True},
                                   headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual((status, body), (200, {"keeper": True}))

    def test_json_only(self):
        client = self.signed_in()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", body="username=player&password=x",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 415)
        conn.close()
        self.assertIsNotNone(client)

    def test_login_logout_and_lockout(self):
        self.signed_in()
        client = Client(self.port)
        status, _ = client.call("POST", "/api/login", {"username": "player", "password": "wrong!!!"})
        self.assertEqual(status, 401)
        status, body = client.call("POST", "/api/login", {"username": "player",
                                                          "password": "a long password"})
        self.assertEqual(status, 200)
        client.csrf = body["csrf"]
        self.assertEqual(client.call("GET", "/api/games")[0], 200)
        self.assertEqual(client.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(client.call("GET", "/api/games")[0], 401)
        attacker = Client(self.port)
        for _ in range(auth.LoginLimiter.PER_PAIR):
            attacker.call("POST", "/api/login", {"username": "player", "password": "guess guess"})
        status, body = attacker.call("POST", "/api/login", {"username": "player",
                                                            "password": "a long password"})
        self.assertEqual(status, 429)
        self.assertIn("Too many", body["error"])

    def test_forwarded_address_is_not_trusted_from_outside(self):
        class FakeHandler(object):
            client_address = ("93.184.216.34", 5555)
            headers = {"Cf-Connecting-Ip": "198.51.100.1"}
        self.assertEqual(webapp._client_address(FakeHandler), "93.184.216.34")
        FakeHandler.client_address = ("192.168.1.5", 5555)
        self.assertEqual(webapp._client_address(FakeHandler), "198.51.100.1")

    def test_cloudflare_access_sign_in(self):
        self.signed_in()
        key = helpers.rsa_key()
        certs = helpers.CertsServer([helpers.jwk_for(key, "k1")])
        try:
            # Set directly: the UI only accepts real team domains, and the
            # fake certs endpoint is plain http on 127.0.0.1.
            self.app.config.update(lambda d: d.__setitem__("access", {
                "team_domain": certs.team, "aud": "aud1", "emails": ["player@example.com"]}))
            good = helpers.make_token(key, "k1", certs.team, "aud1", email="player@example.com")
            client = Client(self.port)
            status, body = client.call("GET", "/api/session",
                                       headers={"Cf-Access-Jwt-Assertion": good})
            self.assertEqual((body["user"], body["via"]), ("player@example.com", "cloudflare"))
            self.assertIsNotNone(client.cookie)
            self.assertEqual(client.call("GET", "/api/games")[0], 200)
            # Not on the list, or a forged token: not signed in, and nothing breaks.
            for token in (helpers.make_token(key, "k1", certs.team, "aud1", email="kid@example.com"),
                          helpers.make_token(helpers.rsa_key(), "k1", certs.team, "aud1",
                                             email="player@example.com"),
                          "garbage"):
                stranger = Client(self.port)
                status, body = stranger.call("GET", "/api/games",
                                             headers={"Cf-Access-Jwt-Assertion": token})
                self.assertEqual(status, 401)
                self.assertIsNone(stranger.cookie)
        finally:
            certs.close()

    def test_games_restore_and_zip(self):
        old = self.play("deck", b"old save")
        self.play("deck", b"new save")
        client = self.signed_in()
        status, body = client.call("GET", "/api/games")
        self.assertEqual(status, 200)
        key = body["games"][0]["key"]
        self.assertEqual(body["games"][0]["title"], GAME)
        status, game = client.call("GET", "/api/games/%s" % key)
        self.assertEqual(len(game["history"]), 2)
        status, made = client.call("POST", "/api/games/%s/restore" % key, {"snapshot": old["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(ss.read_game(self.store, GAME).heads, [made["id"]])
        status, body = client.call("POST", "/api/games/%s/settle" % key, {"snapshot": made["id"]})
        self.assertEqual(status, 400)
        response, payload = client.call("GET", "/api/games/%s/snapshots/%s/zip" % (key, old["id"]),
                                        raw=True)
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("g/backup-1/drive-C/save.sl2"), b"old save")
        self.assertEqual(client.call("GET", "/api/games/nothing-here")[0], 404)

    def test_settings_over_http(self):
        client = self.signed_in()
        status, body = client.call("GET", "/api/settings/shared")
        self.assertEqual((status, body["exists"]), (200, False))
        status, body = client.call("POST", "/api/settings/import", {"text": json.dumps(
            {"trees": {"Bloodborne": {"one_game": "Bloodborne", "roots": {"deck": "/b"}}}})})
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], ["deck"])
        status, body = client.call("PUT", "/api/settings/shared",
                                   {"libraries": {}, "retention": {"days": 99999}})
        self.assertEqual(status, 400)
        status, body = client.call("PUT", "/api/devices/deck/settings",
                                   {"name": "Deck", "roots": {"Bloodborne": "/c"}})
        self.assertEqual((status, body["roots"]), (200, {"Bloodborne": "/c"}))

    def test_users(self):
        client = self.signed_in()
        self.assertEqual(client.call("DELETE", "/api/users/player")[0], 400)   # the last one
        status, body = client.call("POST", "/api/users", {"username": "kid", "password": "another password"})
        self.assertEqual([u["username"] for u in body["users"]], ["kid", "player"])
        self.assertEqual(client.call("POST", "/api/account/password",
                                     {"current": "wrong", "new": "brand new password"})[0], 400)
        self.assertEqual(client.call("POST", "/api/account/password",
                                     {"current": "a long password", "new": "brand new password"})[0], 200)
        self.assertEqual(client.call("GET", "/api/games")[0], 200)              # still signed in
        self.assertEqual(client.call("DELETE", "/api/users/kid")[0], 200)
        with open(self.dir + "/server.json") as handle:
            self.assertNotIn("brand new password", handle.read())


if __name__ == "__main__":
    import unittest
    unittest.main()
