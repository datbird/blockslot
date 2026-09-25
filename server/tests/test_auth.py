"""Passwords, sessions, CSRF, the login limiter and the Cloudflare Access check."""

import base64
import json
import os
import shutil
import tempfile
import time
import unittest

import helpers  # noqa: F401  (import path)
from blockslot_server import auth


class Passwords(unittest.TestCase):
    def test_hash_checks_and_salts(self):
        one = auth.hash_password("correct horse")
        two = auth.hash_password("correct horse")
        self.assertNotEqual(one, two)
        self.assertTrue(one.startswith("scrypt$16384$8$1$"))
        self.assertTrue(auth.check_password("correct horse", one))
        self.assertFalse(auth.check_password("correct horsE", one))

    def test_garbage_hash_is_a_no(self):
        for stored in ("", "plain", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$zz$yy", None):
            self.assertFalse(auth.check_password("anything", stored))

    def test_password_rules(self):
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNone(auth.password_problem("long enough"))


class Clock(object):
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sessions.json")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_create_get_delete(self):
        sessions = auth.Sessions(self.path, now=self.clock)
        sid, record = sessions.create("player")
        self.assertGreaterEqual(len(sid), 43)            # 32 random bytes, base64
        self.assertEqual(sessions.get(sid)["user"], "player")
        self.assertIsNone(sessions.get(sid + "x"))
        sessions.delete(sid)
        self.assertIsNone(sessions.get(sid))

    def test_file_holds_no_cookie_values(self):
        sessions = auth.Sessions(self.path, now=self.clock)
        sid, _ = sessions.create("player")
        with open(self.path) as handle:
            self.assertNotIn(sid, handle.read())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        again = auth.Sessions(self.path, now=self.clock)
        self.assertEqual(again.get(sid)["user"], "player")

    def test_idle_and_absolute_expiry(self):
        sessions = auth.Sessions(self.path, now=self.clock)
        sid, _ = sessions.create("player")
        self.clock.now += auth.SESSION_IDLE + 1
        self.assertIsNone(sessions.get(sid))
        sid, _ = sessions.create("player")
        for _ in range(10):
            self.clock.now += auth.SESSION_IDLE - 10
            sessions.get(sid)
        self.assertIsNone(sessions.get(sid))              # past SESSION_MAX

    def test_delete_user_keeps_the_asker(self):
        sessions = auth.Sessions(self.path, now=self.clock)
        keep, _ = sessions.create("player")
        other, _ = sessions.create("player")
        third, _ = sessions.create("kid")
        sessions.delete_user("player", keep=keep)
        self.assertIsNotNone(sessions.get(keep))
        self.assertIsNone(sessions.get(other))
        self.assertIsNotNone(sessions.get(third))

    def test_cookie_flags(self):
        plain = auth.cookie_header("abc", secure=False)
        self.assertIn("HttpOnly", plain)
        self.assertIn("SameSite=Lax", plain)
        self.assertNotIn("Secure", plain)
        self.assertIn("Secure", auth.cookie_header("abc", secure=True))
        self.assertIn("Max-Age=0", auth.cookie_header("", secure=False, clear=True))
        self.assertEqual(auth.read_cookie("a=1; blockslot_session=xyz; b=2"), "xyz")

    def test_https_detection(self):
        self.assertTrue(auth.is_https({"X-Forwarded-Proto": "https"}))
        self.assertTrue(auth.is_https({"Cf-Visitor": '{"scheme":"https"}'}))
        self.assertFalse(auth.is_https({"Cf-Visitor": '{"scheme":"http"}'}))
        self.assertFalse(auth.is_https({}))


class Csrf(unittest.TestCase):
    def test_same_origin(self):
        self.assertTrue(auth.same_origin({"Host": "nas:8761", "Origin": "http://nas:8761"}))
        self.assertFalse(auth.same_origin({"Host": "nas:8761", "Origin": "https://evil.example"}))
        self.assertFalse(auth.same_origin({"Host": "nas:8761", "Referer": "https://evil.example/x"}))
        self.assertTrue(auth.same_origin({"Host": "127.0.0.1:8761",
                                          "X-Forwarded-Host": "saves-admin.example.com",
                                          "Origin": "https://saves-admin.example.com"}))
        self.assertTrue(auth.same_origin({"Host": "nas:8761"}))

    def test_token(self):
        session = {"csrf": "tok"}
        self.assertTrue(auth.csrf_ok(session, "tok"))
        self.assertFalse(auth.csrf_ok(session, "other"))
        self.assertFalse(auth.csrf_ok(session, None))
        self.assertFalse(auth.csrf_ok(None, "tok"))


class Limiter(unittest.TestCase):
    def test_pair_lockout_and_release(self):
        clock = Clock()
        limiter = auth.LoginLimiter(now=clock)
        for _ in range(auth.LoginLimiter.PER_PAIR):
            self.assertEqual(limiter.blocked("player", "10.0.0.2"), 0)
            limiter.failed("player", "10.0.0.2")
        self.assertGreater(limiter.blocked("player", "10.0.0.2"), 0)
        self.assertEqual(limiter.blocked("player", "10.0.0.3"), 0)   # another address
        clock.now += auth.LoginLimiter.WINDOW + 1
        self.assertEqual(limiter.blocked("player", "10.0.0.2"), 0)

    def test_user_lockout_across_addresses(self):
        limiter = auth.LoginLimiter(now=Clock())
        for n in range(auth.LoginLimiter.PER_USER):
            limiter.failed("Player", "10.0.1.%d" % n)
        self.assertGreater(limiter.blocked("player", "10.9.9.9"), 0)

    def test_success_clears_the_pair(self):
        limiter = auth.LoginLimiter(now=Clock())
        for _ in range(auth.LoginLimiter.PER_PAIR - 1):
            limiter.failed("player", "a")
        limiter.succeeded("player", "a")
        limiter.failed("player", "a")
        self.assertEqual(limiter.blocked("player", "a"), 0)


try:
    import jwt  # noqa: F401  (signs the test tokens: an independent implementation)
    import cryptography  # noqa: F401
    SIGNER = True
except ImportError:
    SIGNER = False


@unittest.skipUnless(SIGNER, "PyJWT and cryptography sign the test tokens (server/requirements-test.txt)")
class CloudflareAccess(unittest.TestCase):
    AUD = "aud-tag-123"

    def setUp(self):
        self.key = helpers.rsa_key()
        self.certs = helpers.CertsServer([helpers.jwk_for(self.key, "kid-1")])
        self.verifier = auth.AccessVerifier()

    def tearDown(self):
        self.certs.close()

    def verify(self, token):
        return self.verifier.verify(token, self.certs.team, self.AUD)

    def test_good_token(self):
        token = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD,
                                   email="Player@Example.com")
        self.assertEqual(self.verify(token), "player@example.com")
        self.verify(token)
        self.assertEqual(self.certs.hits, 1)                # keys cached

    def test_bad_tokens_are_ignored(self):
        other = helpers.rsa_key()
        cases = {
            "wrong audience": helpers.make_token(self.key, "kid-1", self.certs.team, "other-aud"),
            "expired": helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD, exp_in=-60),
            "wrong issuer": helpers.make_token(self.key, "kid-1", "https://evil.example", self.AUD),
            "forged signature": helpers.make_token(other, "kid-1", self.certs.team, self.AUD),
            "garbage": "not.a.token",
        }
        for name, token in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.verify(token))

    def test_hs256_is_refused(self):
        import jwt
        token = jwt.encode({"aud": self.AUD, "iss": self.certs.team, "email": "a@b.c",
                            "exp": 9999999999}, "secret", algorithm="HS256",
                           headers={"kid": "kid-1"})
        self.assertIsNone(self.verify(token))

    def test_none_and_ps256_are_refused(self):
        body = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD).split(".")
        none = base64.urlsafe_b64encode(b'{"alg":"none","kid":"kid-1"}').rstrip(b"=").decode()
        self.assertIsNone(self.verify("%s.%s." % (none, body[1])))
        ps = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD, alg="PS256")
        self.assertIsNone(self.verify(ps))

    def test_a_changed_claim_breaks_the_signature(self):
        head, body, sig = helpers.make_token(self.key, "kid-1", self.certs.team,
                                             self.AUD).split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        claims["email"] = "someone.else@example.com"
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        self.assertIsNone(self.verify("%s.%s.%s" % (head, forged, sig)))

    def test_not_yet_valid_is_refused(self):
        token = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD,
                                   nbf=int(time.time()) + 3600)
        self.assertIsNone(self.verify(token))

    def test_a_short_key_is_never_loaded(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        with self.assertRaises(auth.TokenError):
            auth.rsa_public_key(helpers.jwk_for(small, "k"))

    def test_unknown_kid_refetches_once(self):
        token = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD)
        self.assertIsNotNone(self.verify(token))
        rotated = helpers.rsa_key()
        self.certs.keys = [helpers.jwk_for(rotated, "kid-2")]
        # The refetch gap holds back a second fetch right away...
        new = helpers.make_token(rotated, "kid-2", self.certs.team, self.AUD)
        self.assertIsNone(self.verify(new))
        # ...and once it has passed, an unknown kid makes one fetch.
        self.verifier.fetched = {}
        self.assertEqual(self.verify(new), "player@example.com")
        self.assertEqual(self.certs.hits, 2)

    def test_not_configured_means_nobody(self):
        token = helpers.make_token(self.key, "kid-1", self.certs.team, self.AUD)
        self.assertIsNone(self.verifier.verify(token, "", self.AUD))
        self.assertIsNone(self.verifier.verify(token, self.certs.team, ""))

    def test_allowed_emails(self):
        self.assertTrue(auth.email_allowed("A@b.com", ["a@B.com "]))
        self.assertFalse(auth.email_allowed("c@b.com", ["a@b.com"]))
        self.assertFalse(auth.email_allowed("", [""]))


if __name__ == "__main__":
    unittest.main()
