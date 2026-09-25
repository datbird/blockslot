"""webapp - the BlockSlot server's web UI and its JSON API.

One process, the Python standard library's threading HTTP server, the same
choice engine/slotd.py made: nothing to install beyond PyJWT for the
Cloudflare Access check, and nothing that needs the internet at run time.
The page itself is plain HTML, CSS and JS served from static/.

    python3 -m blockslot_server.webapp          (the entrypoint runs this)

Everything a browser can change goes through three gates: a session (every
route except sign-in and the first account), a same-origin check, and the
session's CSRF token in X-CSRF-Token. JSON bodies only: a cross-site form
cannot send application/json without a CORS preflight, which this server
never answers.
"""

import base64
import datetime
import hashlib
import http.server
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse

from . import HERE, asset_path, auth, garage, saves
from .cloudflare import Cloudflare, CloudflareError
from .saves import SavesError, ss

log = logging.getLogger("blockslot.web")

MAX_BODY = 1024 * 1024
USERNAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
KEEPER_EVERY = 24 * 3600
KEEPER_CHECK = 600


# ------------------------------------------------------------------ server.json


class ServerConfig(object):
    """/data/server.json: users, devices made here, sign-in and Cloudflare settings.

    Mode 0600, written atomically. The entrypoint writes the "s3" section
    before the web app starts; after that the web app is its only writer.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def save(self, data):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def update(self, change):
        """Load, let change(data) edit it, save. Returns change's answer."""
        with self.lock:
            data = self.load()
            answer = change(data)
            self.save(data)
            return answer


class HttpError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def setup_settings(endpoint, access_key, secret_key, device, cf=None):
    """The store settings a device needs: what a setup code holds, and what
    a pairing code hands over."""
    body = {"type": "s3", "endpoint": endpoint, "bucket": garage.BUCKET,
            "region": garage.REGION, "access_key": access_key,
            "secret_key": secret_key, "device": device}
    if cf:
        body["cf_client_id"], body["cf_client_secret"] = cf
    return body


def encode_setup(body):
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8")).decode("ascii")


def setup_code(endpoint, access_key, secret_key, device, cf=None):
    """The one line a device pastes into its Store screen."""
    return encode_setup(setup_settings(endpoint, access_key, secret_key, device, cf=cf))


# ------------------------------------------------------------------ pairing codes

PAIR_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"     # no 0, O, 1, I or L
PAIR_LENGTH = 8
PAIR_TTL = 15 * 60
PAIR_INVALID = "That pairing code is not valid. Make a new one on the Devices page."


def normalize_pair_code(text):
    """The code as stored: capitals, with case, spaces and dashes ignored."""
    return re.sub(r"[\s-]+", "", str(text or "")).upper()


def pair_hash(text):
    return hashlib.sha256(normalize_pair_code(text).encode("ascii", "replace")).hexdigest()


def format_pair_code(raw):
    return raw[:4] + "-" + raw[4:]


class Pairings(object):
    """Pending pairing codes, in memory only.

    Each record holds the device's store settings, secret key included,
    because handing those over is the whole point of the code. The record is
    dropped when the code is spent, when it expires, when a newer code
    replaces it and when the device is removed; a restart drops them all. The
    code itself is never kept, only its SHA-256.
    """

    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.pending = {}            # hash -> {"device", "settings", "expires"}

    def _sweep(self, now):
        for key in [k for k, v in self.pending.items() if v["expires"] <= now]:
            del self.pending[key]

    def create(self, device, settings):
        """A new code for this device, replacing any it had. (code, expires)."""
        raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_LENGTH))
        now = self.now()
        with self.lock:
            self._sweep(now)
            for key in [k for k, v in self.pending.items() if v["device"] == device]:
                del self.pending[key]
            self.pending[pair_hash(raw)] = {"device": device, "settings": dict(settings),
                                            "expires": now + PAIR_TTL}
        return format_pair_code(raw), now + PAIR_TTL

    def settings_for(self, device):
        """The settings of this device's pending code, or None."""
        with self.lock:
            self._sweep(self.now())
            for record in self.pending.values():
                if record["device"] == device:
                    return dict(record["settings"])
        return None

    def spend(self, code):
        """(device, settings) for a live code, which is then gone; or None."""
        normal = normalize_pair_code(code)
        if len(normal) != PAIR_LENGTH:
            return None
        with self.lock:
            self._sweep(self.now())
            record = self.pending.pop(pair_hash(normal), None)
        return (record["device"], record["settings"]) if record else None

    def drop(self, device):
        with self.lock:
            for key in [k for k, v in self.pending.items() if v["device"] == device]:
                del self.pending[key]


class PairLimiter(object):
    """Ten wrong pairing codes from one address in 15 minutes, then a wait."""

    WINDOW = 15 * 60
    LIMIT = 10

    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.failures = {}

    def _recent(self, address, now):
        times = [t for t in self.failures.get(address, []) if now - t < self.WINDOW]
        if times:
            self.failures[address] = times
        else:
            self.failures.pop(address, None)
        return times

    def blocked(self, address):
        now = self.now()
        with self.lock:
            times = self._recent(address, now)
            if len(times) >= self.LIMIT:
                return int(times[-self.LIMIT] + self.WINDOW - now) + 1
            return 0

    def failed(self, address):
        now = self.now()
        with self.lock:
            self.failures[address] = self._recent(address, now) + [now]


# ------------------------------------------------------------------ getting started

ONBOARDING_STEPS = ("account", "address", "emulators", "device", "done")


# ------------------------------------------------------------------ the app


class App(object):
    """Everything the API does, apart from HTTP. The tests drive this directly."""

    def __init__(self, data_dir, store=None, admin=None, s3_url=None,
                 cloudflare_factory=None, verifier=None, now=time.time):
        self.data_dir = data_dir
        self.config = ServerConfig(os.path.join(data_dir, "server.json"))
        self.sessions = auth.Sessions(os.path.join(data_dir, "sessions.json"), now=now)
        self.limiter = auth.LoginLimiter(now=now)
        self.pairings = Pairings(now=now)
        self.pair_limiter = PairLimiter(now=now)
        self.verifier = verifier or auth.AccessVerifier(now=now)
        self.admin = admin
        self.s3_url = s3_url
        self.cloudflare_factory = cloudflare_factory or Cloudflare
        self.now = now
        self._store = store
        self._catalog = None
        self.store_lock = threading.Lock()

    # -- the store

    @property
    def store(self):
        with self.store_lock:
            if self._store is None:
                s3 = self.config.load().get("s3") or {}
                if not (self.s3_url and s3.get("access_key")):
                    raise HttpError(503, "The store is still starting. Try again in a moment.")
                self._store = ss.S3Store(self.s3_url, garage.BUCKET, s3["access_key"],
                                         s3["secret_key"], region=garage.REGION)
            return self._store

    @property
    def catalog(self):
        store = self.store
        with self.store_lock:
            if self._catalog is None:
                self._catalog = saves.Catalog(store, os.path.join(self.data_dir, "cache",
                                                                  "manifests"))
            return self._catalog

    # -- accounts

    def first_run(self):
        return not (self.config.load().get("users") or {})

    def create_first_user(self, username, password):
        def change(data):
            if data.get("users"):
                raise HttpError(409, "An account already exists. Sign in instead.")
            data["users"] = {username: {"hash": auth.hash_password(password),
                                        "created": ss.iso()}}
            # A new install: the guide carries on from the account it just made.
            data["onboarding"] = {"done": False, "step": "address", "updated": ss.iso()}
        self._check_new_user(username, password)
        self.config.update(change)

    def _check_new_user(self, username, password):
        if not USERNAME.match(username or ""):
            raise HttpError(400, "Use a username of letters, digits, dots, dashes or @.")
        problem = auth.password_problem(password)
        if problem:
            raise HttpError(400, problem)

    def login(self, username, password, address):
        wait = self.limiter.blocked(username, address)
        if wait:
            raise HttpError(429, "Too many failed sign-ins. Try again in %d minutes."
                            % max(1, (wait + 59) // 60))
        user = (self.config.load().get("users") or {}).get(username or "")
        # Hash even for an unknown name, so the answer takes the same time.
        stored = user["hash"] if user else auth.hash_password("not a real password")
        if not auth.check_password(password or "", stored) or not user:
            self.limiter.failed(username, address)
            raise HttpError(401, "That username and password do not match.")
        self.limiter.succeeded(username, address)
        return self.sessions.create(username, via="password")

    def cf_login(self, token):
        """A session for a verified, allowed Cloudflare Access email, or None."""
        access = self.config.load().get("access") or {}
        if not (access.get("team_domain") and access.get("aud")):
            return None
        email = self.verifier.verify(token, access["team_domain"], access["aud"])
        if not email:
            return None
        if not auth.email_allowed(email, access.get("emails")):
            log.warning("Cloudflare Access sign-in refused: %s is not on the allowed list", email)
            return None
        return self.sessions.create(email, via="cloudflare")

    def change_password(self, session, current, new):
        if session["via"] != "password":
            raise HttpError(400, "This account signs in through Cloudflare and has no password here.")
        problem = auth.password_problem(new)
        if problem:
            raise HttpError(400, problem)

        def change(data):
            user = (data.get("users") or {}).get(session["user"])
            if not user or not auth.check_password(current or "", user["hash"]):
                raise HttpError(400, "The current password is not right.")
            user["hash"] = auth.hash_password(new)
        self.config.update(change)

    def users(self):
        return [{"username": name, "created": user.get("created")}
                for name, user in sorted((self.config.load().get("users") or {}).items())]

    def add_user(self, username, password):
        self._check_new_user(username, password)

        def change(data):
            users = data.setdefault("users", {})
            if username in users:
                raise HttpError(409, "That username is taken.")
            users[username] = {"hash": auth.hash_password(password), "created": ss.iso()}
        self.config.update(change)

    def set_user_password(self, username, password):
        problem = auth.password_problem(password)
        if problem:
            raise HttpError(400, problem)

        def change(data):
            user = (data.get("users") or {}).get(username)
            if not user:
                raise HttpError(404, "There is no user by that name.")
            user["hash"] = auth.hash_password(password)
        self.config.update(change)
        self.sessions.delete_user(username)

    def remove_user(self, username):
        def change(data):
            users = data.get("users") or {}
            if username not in users:
                raise HttpError(404, "There is no user by that name.")
            if len(users) == 1:
                raise HttpError(400, "This is the last account. Add another before removing it.")
            del users[username]
        self.config.update(change)
        self.sessions.delete_user(username)

    # -- settings that live in server.json

    def access_settings(self):
        access = self.config.load().get("access") or {}
        return {"team_domain": access.get("team_domain") or "", "aud": access.get("aud") or "",
                "emails": access.get("emails") or []}

    def set_access_settings(self, body):
        team = (body.get("team_domain") or "").strip().lower()
        team = re.sub(r"^https?://", "", team).rstrip("/")
        if team and not re.match(r"^[a-z0-9.-]+$", team):
            raise HttpError(400, "The team domain looks like yourteam.cloudflareaccess.com.")
        aud = (body.get("aud") or "").strip()
        emails = body.get("emails") or []
        if isinstance(emails, str):
            emails = re.split(r"[\s,]+", emails)
        emails = sorted({e.strip().lower() for e in emails if e.strip()})
        if any("@" not in e for e in emails):
            raise HttpError(400, "Each allowed email needs an @.")

        def change(data):
            data["access"] = {"team_domain": team, "aud": aud, "emails": emails}
        self.config.update(change)
        return self.access_settings()

    def cloudflare_settings(self):
        cf = self.config.load().get("cloudflare") or {}
        return {"api_token_set": bool(cf.get("api_token")), "account_id": cf.get("account_id") or "",
                "app_id": cf.get("app_id") or ""}

    def set_cloudflare_settings(self, body):
        def change(data):
            cf = data.setdefault("cloudflare", {})
            if body.get("api_token"):
                cf["api_token"] = body["api_token"].strip()
            if body.get("clear_api_token"):
                cf.pop("api_token", None)
            for field in ("account_id", "app_id"):
                if field in body:
                    cf[field] = (body.get(field) or "").strip()
        self.config.update(change)
        return self.cloudflare_settings()

    def _cloudflare(self):
        cf = self.config.load().get("cloudflare") or {}
        if cf.get("api_token") and cf.get("account_id") and cf.get("app_id"):
            return self.cloudflare_factory(cf["api_token"], cf["account_id"], cf["app_id"])
        return None

    def public_endpoint(self, host_header=None):
        found = (self.config.load().get("public_endpoint") or "").strip()
        if found:
            return found
        host = urllib.parse.urlsplit("//" + (host_header or "localhost")).hostname or "localhost"
        if ":" in host:
            host = "[%s]" % host
        return "http://%s:%s" % (host, os.environ.get("BLOCKSLOT_PUBLIC_S3_PORT", "3900"))

    def set_public_endpoint(self, endpoint):
        endpoint = (endpoint or "").strip().rstrip("/")
        if endpoint and not re.match(r"^https?://[^/\s]+(/\S*)?$", endpoint):
            raise HttpError(400, "The endpoint is a URL such as https://saves.example.com.")

        def change(data):
            data["public_endpoint"] = endpoint
        self.config.update(change)

    # -- games

    def games(self, refresh=False):
        return self.catalog.games(refresh=refresh)

    def game(self, game_dir, refresh=False):
        view = self.catalog.view(game_dir, refresh=refresh)
        if view is None:
            raise HttpError(404, "That game is not on the store.")
        head = view.newest_head()
        info = saves.describe_game(game_dir, view.manifests[head])
        info.update({"heads": view.heads, "uploading": sorted(view.pending),
                     "history": saves.history(view)})
        return info

    def restore(self, game_dir, snap_id, settle=False):
        view = self.catalog.view(game_dir, refresh=True)
        try:
            manifest = saves.restore(self.store, view, snap_id, settle=settle)
        except SavesError as exc:
            raise HttpError(400, str(exc))
        except ss.StoreError as exc:
            raise HttpError(502, "The store refused the change: %s" % exc)
        self.catalog.invalidate()
        return {"id": manifest["id"], "restored_from": snap_id}

    def zip_snapshot(self, game_dir, snap_id, out):
        view = self.catalog.view(game_dir)
        if view is None or snap_id not in view.manifests:
            raise HttpError(404, "That save is not on the store.")
        saves.write_zip(self.store, view.manifests[snap_id], out)
        return saves.zip_name(view, snap_id)

    # -- central settings

    def shared_settings(self):
        doc = saves.read_shared(self.store)
        return {"exists": doc is not None,
                "settings": doc or {"version": 1, "libraries": {},
                                    "retention": dict(saves.DEFAULT_RETENTION)}}

    def set_shared_settings(self, doc):
        try:
            return saves.write_shared(self.store, doc)
        except SavesError as exc:
            raise HttpError(400, str(exc))

    def import_trees(self, text):
        try:
            shared, devices = saves.import_trees(self.store, text)
        except SavesError as exc:
            raise HttpError(400, str(exc))
        return {"settings": shared, "devices": devices}

    # -- devices

    def devices(self):
        managed = self.config.load().get("devices") or {}
        docs = saves.device_docs(self.store)
        seen = saves.devices_on_store(self.catalog.listing())
        rows = []
        for device in sorted(set(managed) | set(docs) | set(seen)):
            doc = docs.get(device) or {}
            made = managed.get(device) or {}
            rows.append({"device": device, "name": doc.get("name") or made.get("name") or device,
                         "roots": doc.get("roots") or {}, "set_by": doc.get("set_by"),
                         "updated": doc.get("updated"), "last_save": seen.get(device),
                         "has_key": bool(made.get("key_id")),
                         "cloudflare": bool(made.get("cf_token_id")),
                         "pair_pending": bool(made.get("key_id"))
                         and self.pairings.settings_for(device) is not None,
                         "added": made.get("created")})
        return rows

    def set_device_settings(self, device, body):
        roots = body.get("roots")
        try:
            return saves.write_device(self.store, device, name=body.get("name"), roots=roots)
        except SavesError as exc:
            raise HttpError(400, str(exc))

    def add_device(self, name, host_header=None):
        device = ss.device_key(name or "")
        if not name or not name.strip() or device == saves.SERVER_DEVICE:
            raise HttpError(400, "Give the device a name, such as deck or laptop.")
        if device in (self.config.load().get("devices") or {}):
            raise HttpError(409, "%s already has a key. Remove it first to make a new one." % device)
        if self.admin is None:
            raise HttpError(503, "Garage's admin API is not available.")
        try:
            bucket_id = self.admin.bucket_id(garage.BUCKET)
            key_id, secret = self.admin.create_key("blockslot-%s" % device, bucket_id)
        except garage.GarageError as exc:
            raise HttpError(502, str(exc))
        cf_token = None
        client = self._cloudflare()
        if client is not None:
            try:
                token_id, client_id, client_secret = client.add_device(device)
                cf_token = (token_id, client_id, client_secret)
            except CloudflareError as exc:
                self.admin.delete_key(key_id)
                raise HttpError(502, str(exc))

        def change(data):
            data.setdefault("devices", {})[device] = {
                "key_id": key_id, "name": name.strip(), "created": ss.iso(),
                "cf_token_id": cf_token[0] if cf_token else None}
        self.config.update(change)
        try:
            if saves.read_device(self.store, device) is None:
                saves.write_device(self.store, device, name=name.strip())
        except ss.StoreError as exc:
            log.warning("could not write the device file for %s: %s", device, exc)
        settings = setup_settings(self.public_endpoint(host_header), key_id, secret, device,
                                  cf=(cf_token[1], cf_token[2]) if cf_token else None)
        out = {"device": device, "setup_code": encode_setup(settings), "cloudflare": bool(cf_token)}
        out.update(self._pair_answer(device, settings))
        return out

    def _pair_answer(self, device, settings):
        code, expires = self.pairings.create(device, settings)
        when = datetime.datetime.fromtimestamp(expires, datetime.timezone.utc)
        return {"pair_code": code, "pair_expires": ss.iso(when)}

    def new_pair_code(self, device):
        """A fresh pairing code for a device whose last code is still pending.

        The key's secret is kept only inside a pending pairing, so once that
        is spent or expired there is nothing to hand over; the device is
        removed and added again for a new key.
        """
        if device not in (self.config.load().get("devices") or {}):
            raise HttpError(404, "No key was made here for that device.")
        settings = self.pairings.settings_for(device)
        if settings is None:
            raise HttpError(409, "This server keeps a device's secret key only until its pairing "
                                 "code is used or expires. Remove the device and add it again "
                                 "for a new code.")
        out = {"device": device, "setup_code": encode_setup(settings),
               "cloudflare": bool(settings.get("cf_client_id"))}
        out.update(self._pair_answer(device, settings))
        return out

    def pair(self, code, address):
        """The store settings for a pairing code, which is then spent."""
        wait = self.pair_limiter.blocked(address)
        if wait:
            log.warning("pairing refused for %s: too many wrong codes", address)
            raise HttpError(429, "Too many wrong pairing codes. Try again in %d minutes."
                            % max(1, (wait + 59) // 60))
        found = self.pairings.spend(code)
        if found is None:
            self.pair_limiter.failed(address)
            log.warning("pairing failed from %s: unknown, spent or expired code", address)
            raise HttpError(404, PAIR_INVALID)
        device, settings = found
        log.info("paired %s from %s", device, address)
        return {"setup": settings}

    def remove_device(self, device):
        made = (self.config.load().get("devices") or {}).get(device)
        if not made:
            raise HttpError(404, "No key was made here for that device, so there is nothing to revoke.")
        if made.get("cf_token_id"):
            client = self._cloudflare()
            if client is None:
                raise HttpError(400, "This device has a Cloudflare token, and the Cloudflare "
                                     "settings are not complete. Fill them in first.")
            try:
                client.remove_device(made["cf_token_id"])
            except CloudflareError as exc:
                raise HttpError(502, str(exc))
        try:
            self.admin.delete_key(made["key_id"])
        except garage.GarageError as exc:
            raise HttpError(502, str(exc))

        def change(data):
            (data.get("devices") or {}).pop(device, None)
        self.config.update(change)
        self.pairings.drop(device)
        return {"removed": device}

    # -- getting started

    def _existing_install(self, data):
        """True when this store was in use before the guide existed: devices
        made here, a device file, or any save. None when the store cannot say."""
        if data.get("devices"):
            return True
        try:
            if self.catalog.listing():
                return True
            return bool(saves.device_docs(self.store))
        except (HttpError, ss.StoreError) as exc:
            log.info("could not tell whether this is an existing install: %s", exc)
            return None

    def onboarding(self):
        data = self.config.load()
        state = data.get("onboarding")
        if not isinstance(state, dict):
            if not data.get("users"):
                state = {"done": False, "step": "account"}
            else:
                existing = self._existing_install(data)
                if existing is None:
                    # Unsure: never push a guide on a server that may be in use.
                    state = {"done": True, "step": "done"}
                elif existing:
                    state = {"done": True, "step": "done", "existing": True,
                             "updated": ss.iso()}

                    def change(fresh):
                        fresh.setdefault("onboarding", state)
                    self.config.update(change)
                else:
                    state = {"done": False, "step": "address"}
        step = state.get("step") if state.get("step") in ONBOARDING_STEPS else "address"
        return {"done": bool(state.get("done")), "step": step,
                "existing": bool(state.get("existing")), "steps": list(ONBOARDING_STEPS)}

    def set_onboarding(self, body):
        step = body.get("step")
        done = body.get("done")
        if step is not None and step not in ONBOARDING_STEPS:
            raise HttpError(400, "There is no step called that.")
        if done is not None and not isinstance(done, bool):
            raise HttpError(400, "done is true or false.")
        current = self.onboarding()

        def change(data):
            state = data.get("onboarding") if isinstance(data.get("onboarding"), dict) else {}
            state = {"done": current["done"], "step": current["step"],
                     "existing": bool(state.get("existing"))}
            if step is not None:
                state["step"] = step
            if done is not None:
                state["done"] = done
            if state["step"] == "done":
                state["done"] = True
            if not state["existing"]:
                state.pop("existing")
            state["updated"] = ss.iso()
            data["onboarding"] = state
        self.config.update(change)
        return self.onboarding()

    # -- storage

    def storage(self):
        listing = self.catalog.listing(refresh=True)
        games = set()
        snapshots = 0
        for key, _when in listing:
            parts = key.split("/")
            if len(parts) == 6 and parts[4] == "snapshots":
                games.add(parts[3])
                snapshots += 1
        bucket = None
        if self.admin is not None:
            try:
                info = self.admin.bucket_info(garage.BUCKET)
                bucket = {"bytes": info.get("bytes"), "objects": info.get("objects")}
            except garage.GarageError as exc:
                bucket = {"error": str(exc)}
        data = self.config.load()
        shared = saves.read_shared(self.store) or {}
        return {"bucket": bucket, "games": len(games), "snapshots": snapshots,
                "newest_upload": saves.newest_upload(listing),
                "retention": dict(saves.DEFAULT_RETENTION, **(shared.get("retention") or {})),
                "keeper": bool(data.get("keeper")), "last_clean": data.get("last_clean")}

    def clean(self, dry_run=True):
        retention = (saves.read_shared(self.store) or {}).get("retention")
        try:
            result = saves.run_clean(self.store, retention, dry_run=dry_run)
        except ss.StoreError as exc:
            raise HttpError(502, "The clean-up stopped: %s" % exc)
        if not dry_run and not result.get("skipped"):
            self.config.update(lambda data: data.__setitem__("last_clean", result["when"]))
            self.catalog.invalidate()
        return result

    def set_keeper(self, keeper):
        self.config.update(lambda data: data.__setitem__("keeper", bool(keeper)))
        return {"keeper": bool(keeper)}

    def keeper_tick(self):
        """Clean once a day while this server is the keeper. Returns the result or None."""
        data = self.config.load()
        if not data.get("keeper"):
            return None
        last = ss.parse_iso(data.get("last_clean") or "")
        if last and self.now() - last.timestamp() < KEEPER_EVERY:
            return None
        result = self.clean(dry_run=False)
        log.info("keeper clean-up: %s", {k: v for k, v in result.items() if k != "log"})
        return result


# ------------------------------------------------------------------ HTTP


def _client_address(handler):
    """The address the login limiter counts. Cloudflare's header counts only
    when the request came from a private address, as it does through the
    tunnel; from anywhere else it could be forged to dodge the limit."""
    peer = handler.client_address[0]
    try:
        private = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        private = False
    forwarded = handler.headers.get("Cf-Connecting-Ip")
    return forwarded.strip() if private and forwarded else peer


ROUTES = []


def route(method, pattern, open_route=False, device_route=False):
    """device_route: called by a device app, not a browser page. It skips the
    same-origin check and the session entirely, and never sets a cookie."""
    def wrap(func):
        ROUTES.append((method, re.compile("^%s$" % pattern),
                       "device" if device_route else open_route, func))
        return func
    return wrap


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BlockSlot"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app = None                     # set by make_server

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def log_request(self, code="-", size="-"):
        """Log what changes something or fails. A page load, its API reads and
        the health check every minute would bury those in the container log."""
        status = code.value if hasattr(code, "value") else code
        if self.command in ("GET", "HEAD") and isinstance(status, int) and status < 400:
            return
        http.server.BaseHTTPRequestHandler.log_request(self, code, size)

    # -- plumbing

    def _send(self, status, body=b"", ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                         "form-action 'self'")
        for name, value in (headers or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data, headers=None):
        self._send(status, json.dumps(data).encode("utf-8"), headers=headers)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "That is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(415, "Send JSON.")
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "That is not valid JSON.")
        if not isinstance(data, dict):
            raise HttpError(400, "Send a JSON object.")
        return data

    def _session(self):
        """(session id, record, cookie to set) for this request."""
        session_id = auth.read_cookie(self.headers.get("Cookie"))
        record = self.app.sessions.get(session_id)
        if record:
            return session_id, record, None
        token = self.headers.get("Cf-Access-Jwt-Assertion")
        if token:
            made = self.app.cf_login(token)
            if made:
                return made[0], made[1], auth.cookie_header(made[0], auth.is_https(self.headers))
        return None, None, None

    def _dispatch(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
            return self._static(path)
        for method, pattern, open_route, func in ROUTES:
            if method != self.command:
                continue
            match = pattern.match(path)
            if not match:
                continue
            extra = []
            try:
                if open_route == "device":
                    session_id, session = None, None
                else:
                    if self.command in ("POST", "PUT", "DELETE"):
                        if not auth.same_origin(self.headers):
                            raise HttpError(403, "That request came from another site.")
                    session_id, session, cookie = self._session()
                    if cookie:
                        extra.append(("Set-Cookie", cookie))
                if not open_route:
                    if session is None:
                        raise HttpError(401, "Sign in first.")
                    if self.command in ("POST", "PUT", "DELETE") and not auth.csrf_ok(
                            session, self.headers.get("X-CSRF-Token")):
                        raise HttpError(403, "The page is out of date. Reload it and try again.")
                request = {"session_id": session_id, "session": session,
                           "query": urllib.parse.parse_qs(parsed.query),
                           "args": [urllib.parse.unquote(g) for g in match.groups()],
                           "handler": self}
                if self.command in ("POST", "PUT"):
                    request["body"] = self._body()
                else:
                    request["body"] = {}
                answer = func(self.app, request)
                if answer is None:
                    return
                status, data, headers = answer if len(answer) == 3 else (answer[0], answer[1], [])
                self._json(status, data, headers=extra + list(headers))
            except HttpError as exc:
                self._json(exc.status, {"error": exc.message}, headers=extra)
            except ss.StoreOffline as exc:
                self._json(503, {"error": "The store is not answering: %s" % exc}, headers=extra)
            except ss.StoreError as exc:
                self._json(502, {"error": "The store refused: %s" % exc}, headers=extra)
            except Exception:                       # noqa: BLE001, a bug must not kill the server
                log.exception("request failed: %s %s", self.command, path)
                self._json(500, {"error": "Something went wrong on the server. The log has the details."},
                           headers=extra)
            return
        self._json(404, {"error": "No such page."})

    STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
              "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/static/app.css": ("app.css", "text/css; charset=utf-8"),
              "/static/icon.svg": ("icon.svg", "image/svg+xml"),
              "/favicon.ico": ("icon.svg", "image/svg+xml")}

    def _static(self, path):
        found = self.STATIC.get(path)
        if found is None:
            return self._json(404, {"error": "No such page."})
        name, ctype = found
        full = asset_path(name) if name == "icon.svg" else os.path.join(HERE, "static", name)
        try:
            with open(full, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._json(404, {"error": "No such page."})
        self._send(200, body, ctype)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()


def _arg(request, name, default=None):
    return request["body"].get(name, default)


# -- session


@route("GET", r"/api/session", open_route=True)
def api_session(app, request):
    session = request["session"]
    access = app.access_settings()
    out = {"first_run": app.first_run(), "user": None,
           "cloudflare_access": bool(access["team_domain"] and access["aud"])}
    if session:
        out.update({"user": session["user"], "via": session["via"], "csrf": session["csrf"]})
    return 200, out


@route("POST", r"/api/setup", open_route=True)
def api_setup(app, request):
    username = (_arg(request, "username") or "").strip()
    app.create_first_user(username, _arg(request, "password") or "")
    session_id, record = app.sessions.create(username)
    handler = request["handler"]
    return 200, {"user": username, "via": "password", "csrf": record["csrf"]}, [
        ("Set-Cookie", auth.cookie_header(session_id, auth.is_https(handler.headers)))]


@route("POST", r"/api/login", open_route=True)
def api_login(app, request):
    handler = request["handler"]
    username = (_arg(request, "username") or "").strip()
    session_id, record = app.login(username, _arg(request, "password") or "",
                                   _client_address(handler))
    return 200, {"user": username, "via": "password", "csrf": record["csrf"]}, [
        ("Set-Cookie", auth.cookie_header(session_id, auth.is_https(handler.headers)))]


@route("POST", r"/api/logout")
def api_logout(app, request):
    app.sessions.delete(request["session_id"])
    return 200, {"ok": True}, [("Set-Cookie", auth.cookie_header(
        "", auth.is_https(request["handler"].headers), clear=True))]


# -- games


@route("GET", r"/api/games")
def api_games(app, request):
    return 200, {"games": app.games(refresh="refresh" in request["query"])}


@route("GET", r"/api/games/([^/]+)")
def api_game(app, request):
    return 200, app.game(request["args"][0], refresh="refresh" in request["query"])


@route("POST", r"/api/games/([^/]+)/restore")
def api_restore(app, request):
    return 200, app.restore(request["args"][0], _arg(request, "snapshot") or "")


@route("POST", r"/api/games/([^/]+)/settle")
def api_settle(app, request):
    return 200, app.restore(request["args"][0], _arg(request, "snapshot") or "", settle=True)


@route("GET", r"/api/games/([^/]+)/snapshots/([^/]+)/zip")
def api_zip(app, request):
    game_dir, snap_id = request["args"]
    handler = request["handler"]
    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024,
                                       dir=os.path.join(app.data_dir)) as out:
        name = app.zip_snapshot(game_dir, snap_id, out)
        size = out.tell()
        out.seek(0)
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Length", str(size))
        handler.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        shutil.copyfileobj(out, handler.wfile, 1024 * 1024)
    return None


# -- settings


@route("GET", r"/api/settings/shared")
def api_shared(app, request):
    return 200, app.shared_settings()


@route("PUT", r"/api/settings/shared")
def api_set_shared(app, request):
    return 200, {"settings": app.set_shared_settings(request["body"])}


@route("POST", r"/api/settings/import")
def api_import(app, request):
    return 200, app.import_trees(_arg(request, "text") or "")


# -- devices


@route("GET", r"/api/devices")
def api_devices(app, request):
    return 200, {"devices": app.devices(),
                 "public_endpoint": app.public_endpoint(request["handler"].headers.get("Host")),
                 "public_endpoint_set": bool(app.config.load().get("public_endpoint")),
                 "cloudflare": app.cloudflare_settings()}


@route("POST", r"/api/devices")
def api_add_device(app, request):
    return 200, app.add_device(_arg(request, "name") or "",
                               request["handler"].headers.get("Host"))


@route("POST", r"/api/devices/([^/]+)/pair-code")
def api_new_pair_code(app, request):
    return 200, app.new_pair_code(request["args"][0])


@route("POST", r"/api/pair", device_route=True)
def api_pair(app, request):
    return 200, app.pair(_arg(request, "code") or "", _client_address(request["handler"]))


@route("PUT", r"/api/devices/([^/]+)/settings")
def api_device_settings(app, request):
    return 200, app.set_device_settings(request["args"][0], request["body"])


@route("DELETE", r"/api/devices/([^/]+)")
def api_remove_device(app, request):
    return 200, app.remove_device(request["args"][0])


@route("PUT", r"/api/endpoint")
def api_endpoint(app, request):
    app.set_public_endpoint(_arg(request, "endpoint"))
    return 200, {"public_endpoint": app.public_endpoint(request["handler"].headers.get("Host"))}


@route("GET", r"/api/cloudflare")
def api_cloudflare(app, request):
    return 200, app.cloudflare_settings()


@route("PUT", r"/api/cloudflare")
def api_set_cloudflare(app, request):
    return 200, app.set_cloudflare_settings(request["body"])


# -- getting started


@route("GET", r"/api/onboarding")
def api_onboarding(app, request):
    out = app.onboarding()
    out.update({"public_endpoint": app.public_endpoint(request["handler"].headers.get("Host")),
                "public_endpoint_set": bool(app.config.load().get("public_endpoint"))})
    return 200, out


@route("PUT", r"/api/onboarding")
def api_set_onboarding(app, request):
    return 200, app.set_onboarding(request["body"])


# -- storage


@route("GET", r"/api/storage")
def api_storage(app, request):
    return 200, app.storage()


@route("POST", r"/api/storage/clean")
def api_clean(app, request):
    return 200, app.clean(dry_run=bool(_arg(request, "dry_run", True)))


@route("PUT", r"/api/storage/keeper")
def api_keeper(app, request):
    return 200, app.set_keeper(_arg(request, "keeper"))


# -- account


@route("POST", r"/api/account/password")
def api_password(app, request):
    app.change_password(request["session"], _arg(request, "current"), _arg(request, "new"))
    app.sessions.delete_user(request["session"]["user"], keep=request["session_id"])
    return 200, {"ok": True}


@route("GET", r"/api/users")
def api_users(app, request):
    return 200, {"users": app.users(), "me": request["session"]["user"]}


@route("POST", r"/api/users")
def api_add_user(app, request):
    app.add_user((_arg(request, "username") or "").strip(), _arg(request, "password") or "")
    return 200, {"users": app.users()}


@route("PUT", r"/api/users/([^/]+)/password")
def api_user_password(app, request):
    app.set_user_password(request["args"][0], _arg(request, "password") or "")
    return 200, {"ok": True}


@route("DELETE", r"/api/users/([^/]+)")
def api_remove_user(app, request):
    app.remove_user(request["args"][0])
    return 200, {"users": app.users()}


@route("GET", r"/api/access")
def api_access(app, request):
    return 200, app.access_settings()


@route("PUT", r"/api/access")
def api_set_access(app, request):
    return 200, app.set_access_settings(request["body"])


# ------------------------------------------------------------------ running


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET6

    def server_bind(self):
        # Both IPv4 and IPv6 on one socket, so the container answers on either.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        http.server.HTTPServer.server_bind(self)


def make_server(app, host="::", port=8761):
    handler = type("BoundHandler", (Handler,), {"app": app})
    if ":" in host:
        return Server((host, port), handler)

    class Server4(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    return Server4((host, port), handler)


def keeper_loop(app, stop):
    while not stop.wait(KEEPER_CHECK):
        try:
            app.keeper_tick()
        except Exception:                           # noqa: BLE001, try again next time
            log.exception("keeper clean-up failed")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s web %(levelname)s %(message)s")
    data_dir = os.environ.get("BLOCKSLOT_DATA", "/data")
    admin_url = os.environ.get("BLOCKSLOT_ADMIN_URL")
    admin_token = os.environ.get("BLOCKSLOT_ADMIN_TOKEN")
    admin = garage.GarageAdmin(admin_url, admin_token) if admin_url and admin_token else None
    app = App(data_dir, admin=admin, s3_url=os.environ.get("BLOCKSLOT_S3_URL"))
    host = os.environ.get("BLOCKSLOT_WEB_HOST", "::")
    port = int(os.environ.get("BLOCKSLOT_WEB_PORT", "8761"))
    server = make_server(app, host, port)
    stop = threading.Event()
    threading.Thread(target=keeper_loop, args=(app, stop), daemon=True).start()
    log.info("BlockSlot web UI on port %d", port)
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
