"""garage - the Garage config file and Garage's admin API.

Garage is the S3 store inside the BlockSlot container. This module writes its
config on a first start, adapts an existing config it adopts, and talks to
the admin API (v2) for the layout, the bucket and the per-device keys.

ADOPTING AN EXISTING INSTALL

A Garage run from the official image usually keeps garage.toml, meta/ and data/
in one folder, but its toml names /var/lib/garage/meta and /var/lib/garage/data,
the paths inside the old container. The adopted file is never edited: the old
container may still need it for a rollback. Instead Garage is started with a
runtime copy (see runtime_config) in which a directory that does not exist in
this container, and whose last name matches a folder in /data, is pointed at
that folder. The same copy pins the admin API to 127.0.0.1, because it holds
the keys to every save and only the web app in this container needs it.
"""

import json
import os
import re
import secrets
import tomllib
import urllib.error
import urllib.parse
import urllib.request

BUCKET = "blockslot"
REGION = "garage"
SERVER_KEY_NAME = "blockslot-server"


# ------------------------------------------------------------------ config


def new_config(data_dir, s3_port=3900, rpc_port=3901, admin_port=3903):
    """A fresh single-node garage.toml, with new secrets, as text."""
    return "\n".join([
        "# BlockSlot save store. Single node, written by the BlockSlot server on",
        "# its first start. The admin API answers inside the container only.",
        'metadata_dir = "%s"' % os.path.join(data_dir, "meta"),
        'data_dir = "%s"' % os.path.join(data_dir, "data"),
        'db_engine = "sqlite"',
        "replication_factor = 1",
        'rpc_bind_addr = "[::]:%d"' % rpc_port,
        'rpc_public_addr = "127.0.0.1:%d"' % rpc_port,
        'rpc_secret = "%s"' % secrets.token_hex(32),
        "",
        "[s3_api]",
        's3_region = "%s"' % REGION,
        'api_bind_addr = "[::]:%d"' % s3_port,
        'root_domain = ".s3.garage.localhost"',
        "",
        "[admin]",
        'api_bind_addr = "127.0.0.1:%d"' % admin_port,
        'admin_token = "%s"' % secrets.token_urlsafe(32),
        "",
    ])


def write_new_config(path, data_dir, **ports):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(new_config(data_dir, **ports))


def read_config(path):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _local_dir(named, data_dir):
    """Where a directory the toml names really is in this container."""
    if os.path.isdir(named):
        return named
    candidate = os.path.join(data_dir, os.path.basename(named.rstrip("/")))
    if os.path.isdir(candidate):
        return candidate
    return named


def runtime_config(text, data_dir, admin_port=None):
    """The config Garage runs with, from the adopted file's text.

    Only three values change: metadata_dir and data_dir when they name paths
    this container does not have (see the module docstring), and the admin
    API's bind address, pinned to 127.0.0.1. Every other line, secrets and
    comments included, is kept as it is. Returns (text, admin port).
    """
    parsed = tomllib.loads(text)
    admin_bind = (parsed.get("admin") or {}).get("api_bind_addr") or "127.0.0.1:3903"
    port = admin_port or int(admin_bind.rsplit(":", 1)[1])
    out = []
    section = ""
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            section = header.group(1).strip()
        key = re.match(r"^\s*([A-Za-z_]+)\s*=", line)
        name = key.group(1) if key else ""
        if section == "" and name in ("metadata_dir", "data_dir"):
            line = '%s = "%s"' % (name, _local_dir(parsed[name], data_dir))
        elif section == "admin" and name == "api_bind_addr":
            line = 'api_bind_addr = "127.0.0.1:%d"' % port
        out.append(line)
    return "\n".join(out) + "\n", port


def admin_token(text):
    return ((tomllib.loads(text).get("admin") or {}).get("admin_token")) or ""


def s3_port(text):
    bind = (tomllib.loads(text).get("s3_api") or {}).get("api_bind_addr") or "[::]:3900"
    return int(bind.rsplit(":", 1)[1])


# ------------------------------------------------------------------ admin API


class GarageError(Exception):
    """Garage's admin API said no, or did not answer."""


def _default_opener(request, timeout=None):
    return urllib.request.urlopen(request, timeout=timeout)


class GarageAdmin(object):
    """Garage's admin API v2: one POST or GET per call, JSON both ways.

    The opener is urllib's by default and is replaced in the tests, which
    check the exact calls without a running Garage.
    """

    def __init__(self, url, token, opener=None, timeout=15):
        self.url = url.rstrip("/")
        self.token = token
        self.opener = opener or _default_opener
        self.timeout = timeout

    def call(self, name, body=None, query=None, method=None):
        url = "%s/v2/%s" % (self.url, name)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method or ("POST" if body is not None else "GET"),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            response = self.opener(request, timeout=self.timeout)
            raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = (exc.read() or b"").decode("utf-8", "replace")[:300]
            raise GarageError("Garage refused %s (HTTP %d): %s" % (name, exc.code, detail))
        except (urllib.error.URLError, OSError) as exc:
            raise GarageError("Garage is not answering: %s" % getattr(exc, "reason", exc))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise GarageError("Garage answered %s with something that is not JSON" % name)

    # -- cluster

    def status(self):
        return self.call("GetClusterStatus")

    def health(self):
        return self.call("GetClusterHealth")

    def ensure_layout(self, capacity):
        """Give this one node a role, once. Returns True when it changed.

        A node without a role stores nothing, and every S3 write fails with
        an error about the layout, so a first start does this before
        anything else.
        """
        status = self.status()
        nodes = status.get("nodes") or []
        if not nodes:
            raise GarageError("Garage reports no node")
        if any(node.get("role") for node in nodes):
            return False
        node_id = nodes[0]["id"]
        layout = self.call("GetClusterLayout")
        staged = [r for r in layout.get("stagedRoleChanges") or [] if r.get("id") == node_id]
        if not staged:
            self.call("UpdateClusterLayout", {"roles": [
                {"id": node_id, "zone": "dc1", "capacity": int(capacity), "tags": ["blockslot"]}]})
        self.call("ApplyClusterLayout", {"version": int(layout.get("version") or 0) + 1})
        return True

    # -- buckets

    def bucket_id(self, alias=BUCKET):
        try:
            return self.call("GetBucketInfo", query={"globalAlias": alias})["id"]
        except GarageError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def ensure_bucket(self, alias=BUCKET):
        found = self.bucket_id(alias)
        if found:
            return found, False
        return self.call("CreateBucket", {"globalAlias": alias})["id"], True

    def bucket_info(self, alias=BUCKET):
        return self.call("GetBucketInfo", query={"globalAlias": alias})

    # -- keys

    def create_key(self, name, bucket_id, owner=False):
        """A new key allowed to read and write the bucket: (id, secret)."""
        made = self.call("CreateKey", {"name": name})
        key_id = made["accessKeyId"]
        try:
            self.call("AllowBucketKey", {
                "bucketId": bucket_id, "accessKeyId": key_id,
                "permissions": {"read": True, "write": True, "owner": bool(owner)}})
        except GarageError:
            # A key that exists but can do nothing is only clutter; remove it
            # so a retry starts clean.
            self.delete_key(key_id)
            raise
        return key_id, made["secretAccessKey"]

    def delete_key(self, key_id):
        try:
            self.call("DeleteKey", {}, query={"id": key_id})
        except GarageError as exc:
            if "HTTP 404" not in str(exc):
                raise

    def list_keys(self):
        return self.call("ListKeys") or []
