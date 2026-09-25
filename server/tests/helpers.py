"""Shared test helpers: the import path, a store with real snapshots, a fake
certs endpoint and an RSA key for Cloudflare Access tokens."""

import http.server
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import blockslot_server  # noqa: E402,F401  (puts engine/ on the path)
import slotstore as ss  # noqa: E402

# The log lines a bad token or a failed request writes are expected here.
logging.getLogger("blockslot").setLevel(logging.CRITICAL)

GAME = "Dark Souls II: Scholar of the First Sin"


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())


class StoreCase(unittest.TestCase):
    """A LocalStore and a way to play sessions on devices against it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blockslot-server-")
        self.store = ss.LocalStore(os.path.join(self.dir, "store"))
        os.makedirs(self.store.root)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, device):
        return ss.LocalState(os.path.join(self.dir, "state-" + device))

    def play(self, device, save, game=GAME, unit=None, played=None, created=None):
        """One session on a device: stage its save and upload it."""
        source = tempfile.mkdtemp(prefix="src-", dir=self.dir)
        write(os.path.join(source, "g", "backup-1", "drive-C", "save.sl2"), save)
        write(os.path.join(source, "g", "mapping.yaml"), "name: g\n")
        state = self.state(device)
        manifest = state.stage(game, device, source, unit=unit, played=played,
                               created=created)
        committed, error = ss.drain(self.store, state)
        self.assertIsNone(error)
        return manifest


# ------------------------------------------------------------------ Cloudflare Access fakes


def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_for(private_key, kid):
    import jwt
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    data.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return data


def make_token(private_key, kid, team, aud, email="player@example.com", exp_in=300,
               alg="RS256", **extra):
    import jwt
    now = int(time.time())
    claims = {"aud": [aud], "email": email, "iss": team, "iat": now, "nbf": now - 5,
              "exp": now + exp_in, "sub": "user-1", "type": "app"}
    claims.update(extra)
    return jwt.encode(claims, private_key, algorithm=alg, headers={"kid": kid})


class CertsServer(object):
    """A local stand-in for https://<team>/cdn-cgi/access/certs."""

    def __init__(self, keys):
        self.keys = keys
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/cdn-cgi/access/certs":
                    self.send_response(404)
                    self.end_headers()
                    return
                outer.hits += 1
                body = json.dumps({"keys": outer.keys}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.team = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
