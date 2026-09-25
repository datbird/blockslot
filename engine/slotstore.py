"""slotstore - where Blockslot keeps saves off the device, and how it gets them there.

The design is docs/superpowers/specs/2026-09-24-store-and-daemon-design.md.
This file is the library both the daemon and the picker use: the store
transports, the snapshot format, the commit order, the queue and the lineage
decision. It imports nothing outside the standard library, and nothing from
savepick, so it can be copied next to savepick.py and imported from there.

EVERYTHING ON THE STORE IS WRITTEN ONCE

A snapshot is a manifest plus the blobs it names. A blob is named by the
SHA-256 of its bytes, so two writes of one name always carry the same bytes.
A manifest's name carries its time, its device and its own hash. So no object
is ever changed after it is written, two devices can never overwrite each
other, and the store needs no locking and no compare-and-swap.

Python 3.9 compatible, because macOS ships 3.9. Decky's frozen python has no
xml.etree, so the S3 listing is read without it.
"""

import datetime
import errno
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

FORMAT = "blockslot/v1"
PREFIX = FORMAT + "/"

# A blob above this goes up in parts. Cloudflare caps a proxied request body
# at 100 MB on its free plan, and parts of this size stay well under it.
PART_SIZE = 32 * 1024 * 1024
MULTIPART_OVER = 64 * 1024 * 1024

# ludusavi metadata that belongs to the backup format, not to the save.
METADATA_NAMES = ("mapping.yaml", "registry.yaml")

REQUEST_TIMEOUT = 30

# Cloudflare's bot rules refuse urllib's default user agent (error 1010)
# before Access even reads the service token. Name ourselves instead.
USER_AGENT = "Blockslot/1 (save sync)"


# ------------------------------------------------------------------ errors


class StoreError(Exception):
    """Something went wrong talking to the store."""


class StoreOffline(StoreError):
    """The store could not be reached at all. Try again later, quietly."""


class StoreRefused(StoreError):
    """The store answered and said no. Retrying will not help until a person
    fixes something: an expired token, a wrong key, a missing bucket."""


class NotFound(StoreError):
    """The object is not on the store."""


# ------------------------------------------------------------------ names


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(when=None):
    """20260924T060429Z, the form every name and record uses."""
    return (when or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def iso(when=None):
    return (when or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def game_key(name):
    """A game name no filesystem or bucket will argue with.

    The same filter savepick uses for its vault, plus no spaces, because a
    key with spaces needs quoting in every tool a person might look with.
    """
    kept = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    kept = re.sub(r"_+", "_", kept).strip("._")
    return kept or "game"


def device_key(name):
    kept = "".join(c.lower() if c.isalnum() or c == "-" else "-" for c in name)
    return kept.strip("-") or "device"


def blob_key(sha):
    return "%sblobs/%s/%s" % (PREFIX, sha[:2], sha)


def game_prefix(game):
    return "%sgames/%s/" % (PREFIX, game_key(game))


def manifest_key(game, snap_id):
    return "%ssnapshots/%s.json" % (game_prefix(game), snap_id)


def pending_key(game, snap_id):
    return "%spending/%s.json" % (game_prefix(game), snap_id)


def snap_id_for(created, device, manifest_hash):
    return "%s_%s_%s" % (stamp(created), device_key(device), manifest_hash[:8])


def snap_time(snap_id):
    """The time in a snapshot id, or None for a name that is not one."""
    try:
        return datetime.datetime.strptime(snap_id[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def snap_device(snap_id):
    parts = snap_id.split("_")
    return parts[1] if len(parts) >= 3 else ""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ manifests


def scan_dir(root):
    """Every file under root, as manifest file records, sorted by path.

    Paths are relative and always use forward slashes, so a manifest made on
    Windows reads the same on the Deck.
    """
    records = []
    root = os.path.abspath(root)
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            stat = os.stat(full)
            records.append({
                "path": rel,
                "sha256": sha256_file(full),
                "size": stat.st_size,
                "mtime": iso(datetime.datetime.fromtimestamp(
                    stat.st_mtime, datetime.timezone.utc)),
            })
    records.sort(key=lambda record: record["path"])
    return records


def make_manifest(game, device, files, parents, played=None, mode="game",
                  created=None):
    """A manifest dict, with its id worked out from its own content."""
    created = created or utc_now()
    body = {
        "format": FORMAT,
        "game": game,
        "mode": mode,
        "device": device_key(device),
        "created": iso(created),
        "parents": sorted(set(parents or [])),
        "played": played or {},
        "files": files,
    }
    digest = sha256_bytes(canonical_json(body))
    body["id"] = snap_id_for(created, device, digest)
    return body


def canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_bytes(manifest):
    return json.dumps(manifest, sort_keys=True, indent=1).encode("utf-8")


def save_hashes(manifest):
    """The hashes of the save files themselves, without ludusavi's metadata.

    This is what "the same save" means: the same bytes in the save files. The
    backup name and the mapping differ on every backup and mean nothing.
    """
    out = set()
    for record in manifest.get("files") or []:
        if record["path"].rsplit("/", 1)[-1] in METADATA_NAMES:
            continue
        out.add(record["sha256"])
    return out


# ------------------------------------------------------------------ stores


class Store(object):
    """Where snapshots live. Every write is atomic and returns only once stored."""

    label = "store"

    def put(self, key, data):
        """Store bytes, or a file path, under key."""
        raise NotImplementedError

    def get(self, key):
        """The bytes under key. Raises NotFound."""
        raise NotImplementedError

    def get_to(self, key, path):
        data = self.get(key)
        with open(path, "wb") as handle:
            handle.write(data)

    def list(self, prefix):
        """Every key that starts with prefix."""
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def list_times(self, prefix):
        """[(key, when written or None)]. Clean-up keeps what it cannot date."""
        return [(key, None) for key in self.list(prefix)]


def _read_input(data):
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    with open(data, "rb") as handle:
        return handle.read()


class LocalStore(Store):
    """A directory. A mounted share, a USB disk, and every test.

    A write goes to a temporary name first and is renamed into place, which
    is atomic on every filesystem this runs on.
    """

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.label = self.root

    def _path(self, key):
        if ".." in key.split("/"):
            raise StoreRefused("a key may not climb out of the store: %s" % key)
        return os.path.join(self.root, *key.split("/"))

    def put(self, key, data):
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".put-", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "wb") as handle:
                    if isinstance(data, (bytes, bytearray)):
                        handle.write(data)
                    else:
                        with open(data, "rb") as source:
                            shutil.copyfileobj(source, handle, 1024 * 1024)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise _local_error(exc)

    def get(self, key):
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise NotFound(key)
        except OSError as exc:
            raise _local_error(exc)

    def list(self, prefix):
        base = self._path(prefix.rstrip("/")) if prefix.endswith("/") else None
        start = base if base and os.path.isdir(base) else self.root
        out = []
        if not os.path.isdir(start):
            if not os.path.isdir(self.root):
                raise StoreOffline("the store folder is not there: %s" % self.root)
            return out
        for folder, _dirs, files in os.walk(start):
            for name in files:
                if name.startswith(".put-"):
                    continue
                rel = os.path.relpath(os.path.join(folder, name), self.root)
                key = rel.replace(os.sep, "/")
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)

    def exists(self, key):
        return os.path.isfile(self._path(key))

    def list_times(self, prefix):
        out = []
        for key in self.list(prefix):
            try:
                when = datetime.datetime.fromtimestamp(
                    os.path.getmtime(self._path(key)), datetime.timezone.utc)
            except OSError:
                when = None
            out.append((key, when))
        return out

    def delete(self, key):
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _local_error(exc)


def _local_error(exc):
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return StoreRefused(str(exc))
    return StoreOffline(str(exc))


# ------------------------------------------------------------------ S3

# Where operating systems keep their certificate authorities, for a Python
# that ships without a list of its own. Decky's frozen Python is one: every
# HTTPS call from the Deck plugin failed CERTIFICATE_VERIFY_FAILED while the
# Deck's system Python, with the same settings, worked.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",      # SteamOS, Arch, Debian
    "/etc/pki/tls/certs/ca-bundle.crt",        # Fedora
    "/etc/ssl/cert.pem",                       # macOS, Alpine
)


def tls_context(bundles=CA_BUNDLES):
    """A verifying TLS context that actually has authorities to verify with."""
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) or sys.platform == "win32":
        # Windows loads its store lazily, so an empty count there means
        # nothing; its default context verifies correctly.
        return context
    for path in bundles:
        if os.path.isfile(path):
            try:
                context.load_verify_locations(cafile=path)
                return context
            except (OSError, ssl.SSLError):
                continue
    return context


def _default_opener(request, timeout=None):
    return urllib.request.urlopen(request, timeout=timeout, context=_TLS[0])


_TLS = [None]


class S3Store(Store):
    """An S3-compatible bucket: R2, B2, AWS, MinIO, Garage.

    Path-style URLs (https://host/bucket/key), because MinIO and Garage serve
    those by default and every hosted provider accepts them too.

    `cf_token` is (client_id, client_secret) for a Cloudflare Access service
    token. It rides along as two headers on every request, so the edge lets
    the request through and the origin never sees a request without it.
    """

    def __init__(self, endpoint, bucket, access_key, secret_key,
                 region="us-east-1", prefix="", cf_token=None,
                 timeout=REQUEST_TIMEOUT, opener=None):
        self.endpoint = endpoint.rstrip("/")
        parsed = urllib.parse.urlsplit(self.endpoint)
        self.origin = "%s://%s" % (parsed.scheme or "https", parsed.netloc)
        self.host = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.key_prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.cf_token = cf_token
        self.timeout = timeout
        if opener is None:
            if _TLS[0] is None:
                _TLS[0] = tls_context()
            opener = _default_opener
        self.opener = opener
        self.label = "%s/%s" % (self.host, bucket)

    # -- signing

    def _uri(self, key):
        uri = "%s/%s" % (self.base_path, urllib.parse.quote(self.bucket, safe=""))
        if key is not None:
            uri += "/" + urllib.parse.quote(self.key_prefix + key, safe="/-_.~")
        return uri

    def _request(self, method, key=None, query=None, body=b"", headers=None,
                 payload_hash=None):
        query = query or {}
        now = utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        uri = self._uri(key)
        canonical_query = "&".join(
            "%s=%s" % (urllib.parse.quote(str(k), safe="-_.~"),
                       urllib.parse.quote(str(v), safe="-_.~"))
            for k, v in sorted(query.items()))
        if payload_hash is None:
            payload_hash = sha256_bytes(body if isinstance(body, bytes) else b"")
        send = {"host": self.host, "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash}
        for name, value in (headers or {}).items():
            send[name.lower()] = value
        signed = sorted(send)
        canonical = "\n".join([
            method, uri, canonical_query,
            "".join("%s:%s\n" % (name, str(send[name]).strip()) for name in signed),
            ";".join(signed), payload_hash])
        scope = "%s/%s/s3/aws4_request" % (day, self.region)
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                             sha256_bytes(canonical.encode("utf-8"))])
        key_bytes = ("AWS4" + self.secret_key).encode("utf-8")
        for part in (day, self.region, "s3", "aws4_request"):
            key_bytes = hmac.new(key_bytes, part.encode("utf-8"),
                                 hashlib.sha256).digest()
        signature = hmac.new(key_bytes, to_sign.encode("utf-8"),
                             hashlib.sha256).hexdigest()
        send["authorization"] = (
            "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (self.access_key, scope, ";".join(signed), signature))
        if self.cf_token:
            # Not signed: Cloudflare strips nothing, but the origin never
            # needs to see these, and signing them would tie the S3 key to
            # the token for no gain.
            send["CF-Access-Client-Id"] = self.cf_token[0]
            send["CF-Access-Client-Secret"] = self.cf_token[1]
        url = self.origin + uri
        if canonical_query:
            url += "?" + canonical_query
        send.pop("host")
        send["User-Agent"] = USER_AGENT
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         method=method, headers=send)
        try:
            response = self.opener(request, timeout=self.timeout)
            data = response.read()
            return response.status if hasattr(response, "status") else 200, data, response
        except urllib.error.HTTPError as exc:
            data = exc.read() or b""
            if exc.code == 404:
                raise NotFound(key or "")
            raise _s3_error(exc.code, data, exc.headers)
        except (urllib.error.URLError, OSError) as exc:
            raise StoreOffline("cannot reach %s: %s" % (self.host, _reason(exc)))

    # -- operations

    def put(self, key, data):
        if not isinstance(data, (bytes, bytearray)):
            size = os.path.getsize(data)
            if size > MULTIPART_OVER:
                return self._put_multipart(key, data, size)
            data = _read_input(data)
        body = bytes(data)
        self._request("PUT", key, body=body,
                      headers={"content-length": str(len(body))})

    def _put_multipart(self, key, path, size):
        _status, data, _resp = self._request("POST", key, query={"uploads": ""})
        upload_id = _xml_value(data, "UploadId")
        if not upload_id:
            raise StoreRefused("the store did not start a multipart upload")
        etags = []
        try:
            with open(path, "rb") as handle:
                number = 1
                while True:
                    chunk = handle.read(PART_SIZE)
                    if not chunk:
                        break
                    _s, _d, response = self._request(
                        "PUT", key, query={"partNumber": number, "uploadId": upload_id},
                        body=chunk, headers={"content-length": str(len(chunk))})
                    etags.append((number, response.headers.get("ETag", "")))
                    number += 1
            body = ("<CompleteMultipartUpload>%s</CompleteMultipartUpload>" % "".join(
                "<Part><PartNumber>%d</PartNumber><ETag>%s</ETag></Part>" % (n, _xml_escape(e))
                for n, e in etags)).encode("utf-8")
            self._request("POST", key, query={"uploadId": upload_id}, body=body,
                          headers={"content-length": str(len(body))})
        except BaseException:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id})
            except StoreError:
                pass
            raise

    def get(self, key):
        _status, data, _resp = self._request("GET", key)
        return data

    def list(self, prefix):
        return [key for key, _when in self.list_times(prefix)]

    def list_times(self, prefix):
        out = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": self.key_prefix + prefix}
            if token:
                query["continuation-token"] = token
            _status, data, _resp = self._request("GET", None, query=query)
            for block in _xml_values(data, "Contents"):
                found = _xml_value(block, "Key")
                if not found or not found.startswith(self.key_prefix):
                    continue
                stamp_text = (_xml_value(block, "LastModified") or "")[:19]
                try:
                    when = datetime.datetime.strptime(stamp_text, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc)
                except ValueError:
                    when = None
                out.append((found[len(self.key_prefix):], when))
            if _xml_value(data, "IsTruncated") != "true":
                break
            token = _xml_value(data, "NextContinuationToken")
            if not token:
                break
        return sorted(out)

    def exists(self, key):
        try:
            self._request("HEAD", key)
            return True
        except NotFound:
            return False

    def delete(self, key):
        try:
            self._request("DELETE", key)
        except NotFound:
            pass


def _reason(exc):
    return str(getattr(exc, "reason", None) or exc)


def _s3_error(code, data, headers):
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    s3_code = _xml_value(data, "Code")
    s3_message = _xml_value(data, "Message")
    if s3_code:
        message = "%s: %s" % (s3_code, s3_message or "")
    elif "error code: 1010" in text:
        message = ("Cloudflare's bot rules refused this client (error 1010). "
                   "The Blockslot user agent should have been sent.")
    elif "cloudflare" in text.lower() or (headers and headers.get("cf-ray")):
        message = ("Cloudflare refused the request (HTTP %d). The service token "
                   "is missing, wrong or expired." % code)
    else:
        message = "HTTP %d" % code
    if code in (500, 502, 503, 504, 520, 521, 522, 523, 524, 530):
        return StoreOffline("the store is not answering: %s" % message)
    return StoreRefused(message)


_XML_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"),
                 ("&amp;", "&"))


def _xml_unescape(text):
    for entity, char in _XML_ENTITIES:
        text = text.replace(entity, char)
    return text


def _xml_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _xml_values(data, tag):
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return [_xml_unescape(found) for found in
            re.findall(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S)]


def _xml_value(data, tag):
    found = _xml_values(data, tag)
    return found[0] if found else None


# ------------------------------------------------------------------ SSH


class SSHStore(Store):
    """A directory on a server, reached with the system ssh.

    Commands, not SFTP: ssh ships with Windows 10 and later and with SteamOS,
    and a POSIX shell on the far side does the rest. A write goes to a
    temporary name and is renamed, so it is atomic like the other stores.

    `cf_token` sends the connection through `cloudflared access ssh` with a
    service token. The token travels in the environment, not on a command
    line another user could read.
    """

    def __init__(self, host, root, user=None, port=None, identity=None,
                 cf_token=None, ssh_binary=None, cloudflared=None,
                 timeout=REQUEST_TIMEOUT, runner=None):
        self.host = host
        self.root = root.rstrip("/")
        self.user = user
        self.port = port
        self.identity = identity
        self.cf_token = cf_token
        self.ssh_binary = ssh_binary or "ssh"
        self.cloudflared = cloudflared or "cloudflared"
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.label = "%s:%s" % (host, self.root)

    def _argv(self, command):
        argv = [self.ssh_binary, "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=%d" % min(self.timeout, 15),
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
        if self.port:
            argv += ["-p", str(self.port)]
        if self.identity:
            argv += ["-i", self.identity, "-o", "IdentitiesOnly=yes"]
        if self.cf_token:
            argv += ["-o", "ProxyCommand=%s access ssh --hostname %%h" % self.cloudflared]
        target = "%s@%s" % (self.user, self.host) if self.user else self.host
        return argv + [target, command]

    def _env(self):
        env = dict(os.environ)
        if self.cf_token:
            env["TUNNEL_SERVICE_TOKEN_ID"] = self.cf_token[0]
            env["TUNNEL_SERVICE_TOKEN_SECRET"] = self.cf_token[1]
        return env

    def _run(self, command, stdin=None, timeout=None):
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            done = self.runner(self._argv(command), input=stdin,
                               capture_output=True, env=self._env(),
                               timeout=timeout or max(self.timeout, 60), **kwargs)
        except subprocess.TimeoutExpired:
            raise StoreOffline("ssh to %s timed out" % self.host)
        except OSError as exc:
            raise StoreRefused("cannot run ssh: %s" % exc)
        if done.returncode == 255:
            raise _ssh_error(done.stderr, self.host)
        return done

    def _path(self, key):
        if ".." in key.split("/"):
            raise StoreRefused("a key may not climb out of the store: %s" % key)
        return "%s/%s" % (self.root, key)

    def put(self, key, data):
        body = _read_input(data)
        path = self._path(key)
        tmp = "%s/.put-%s" % (path.rsplit("/", 1)[0], os.urandom(6).hex())
        command = "mkdir -p %s && cat > %s && mv -f %s %s" % (
            shlex.quote(path.rsplit("/", 1)[0]), shlex.quote(tmp),
            shlex.quote(tmp), shlex.quote(path))
        done = self._run(command, stdin=body,
                         timeout=max(60, len(body) // (256 * 1024)))
        if done.returncode != 0:
            raise StoreRefused("the server would not store %s: %s"
                               % (key, _text(done.stderr)))

    def get(self, key):
        path = self._path(key)
        done = self._run("test -f %s && cat %s" % (shlex.quote(path), shlex.quote(path)))
        if done.returncode != 0:
            raise NotFound(key)
        return done.stdout

    def list(self, prefix):
        return [key for key, _when in self.list_times(prefix)]

    def list_times(self, prefix):
        folder = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        path = "%s/%s" % (self.root, folder) if folder else self.root
        done = self._run("test -d %s || exit 0; find %s -type f ! -name '.put-*' "
                         "-printf '%%T@ %%p\\n'"
                         % (shlex.quote(path), shlex.quote(path)))
        if done.returncode != 0:
            raise StoreRefused("cannot list %s: %s" % (path, _text(done.stderr)))
        out = []
        head = self.root + "/"
        for line in _text(done.stdout).splitlines():
            stamp_text, _sep, name = line.partition(" ")
            if not name.startswith(head):
                continue
            key = name[len(head):]
            if not key.startswith(prefix):
                continue
            try:
                when = datetime.datetime.fromtimestamp(float(stamp_text),
                                                       datetime.timezone.utc)
            except ValueError:
                when = None
            out.append((key, when))
        return sorted(out)

    def exists(self, key):
        return self._run("test -f %s" % shlex.quote(self._path(key))).returncode == 0

    def delete(self, key):
        self._run("rm -f %s" % shlex.quote(self._path(key)))


def _text(data):
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


def _ssh_error(stderr, host):
    text = _text(stderr).strip()
    lower = text.lower()
    refused = ("permission denied", "host key verification failed",
               "no such identity", "access denied", "forbidden", "403")
    if any(word in lower for word in refused):
        return StoreRefused("ssh to %s was refused: %s" % (host, text[:200]))
    return StoreOffline("cannot reach %s: %s" % (host, text[:200] or "ssh failed"))


# ------------------------------------------------------------------ settings


def store_from_settings(settings):
    """Build a store from the "store" section of the settings, or None.

    {"type": "s3", "endpoint", "bucket", "access_key", "secret_key", "region",
     "prefix", "cf_client_id", "cf_client_secret"}
    {"type": "ssh", "host", "root", "user", "port", "identity",
     "cf_client_id", "cf_client_secret"}
    {"type": "local", "root"}
    """
    if not settings:
        return None
    kind = (settings.get("type") or "").lower()
    token = None
    if settings.get("cf_client_id") and settings.get("cf_client_secret"):
        token = (settings["cf_client_id"], settings["cf_client_secret"])
    if kind == "s3":
        return S3Store(settings["endpoint"], settings["bucket"],
                       settings["access_key"], settings["secret_key"],
                       region=settings.get("region") or "us-east-1",
                       prefix=settings.get("prefix") or "", cf_token=token)
    if kind == "ssh":
        return SSHStore(settings["host"], settings["root"],
                        user=settings.get("user"), port=settings.get("port"),
                        identity=settings.get("identity"), cf_token=token,
                        ssh_binary=settings.get("ssh"),
                        cloudflared=settings.get("cloudflared"))
    if kind == "local":
        return LocalStore(settings["root"])
    raise StoreRefused("unknown store type %r" % kind)


def store_name(settings):
    """What to call the store in a sentence: its own label, or its host."""
    if not settings:
        return "the store"
    if settings.get("name"):
        return settings["name"]
    for field in ("host", "endpoint", "root"):
        if settings.get(field):
            return urllib.parse.urlsplit(settings[field]).netloc or settings[field]
    return "the store"


# ------------------------------------------------------------------ commit


def put_blob(store, path, sha):
    """Upload one file as a gzip blob, unless the store already has it."""
    key = blob_key(sha)
    if store.exists(key):
        return False
    buffer = io.BytesIO()
    with open(path, "rb") as source, gzip.GzipFile(
            fileobj=buffer, mode="wb", mtime=0) as packed:
        shutil.copyfileobj(source, packed, 1024 * 1024)
    store.put(key, buffer.getvalue())
    return True


def commit(store, manifest, data_dir, progress=None, known=None):
    """Put one snapshot on the store, in the order that survives interruption.

    1. intent marker   2. blobs   3. manifest   4. clear the intent

    Each step can be cut off, by a crash or a lid closing, and the next call
    carries on: blobs already there are skipped, and a manifest written twice
    is written with the same bytes. Returns the number of blobs uploaded.

    `known` is a set of blob hashes this device has already seen on the store.
    A retro frontend's tree holds thousands of files that almost never change,
    and asking the store about each one on every exit costs minutes over a
    tunnel. A known blob is not asked about; every blob sent is added.
    """
    game = manifest["game"]
    snap_id = manifest["id"]
    intent = {"id": snap_id, "device": manifest["device"],
              "created": manifest["created"], "parents": manifest["parents"],
              "files": len(manifest["files"]),
              "bytes": sum(r["size"] for r in manifest["files"])}
    store.put(pending_key(game, snap_id), manifest_bytes(intent))

    total = sum(record["size"] for record in manifest["files"]) or 1
    done_bytes = 0
    uploaded = 0
    if manifest.get("merge_only"):
        # Closing a fork names blobs the store already holds. Prove it, so a
        # clean-up that removed one can never leave a manifest pointing at
        # nothing.
        for record in manifest["files"]:
            if not store.exists(blob_key(record["sha256"])):
                raise StoreRefused("the store no longer has %s; this choice "
                                   "cannot be recorded" % record["path"])
        total = 0
    for record in ([] if manifest.get("merge_only") else manifest["files"]):
        path = os.path.join(data_dir, *record["path"].split("/"))
        if sha256_file(path) != record["sha256"]:
            raise StoreError("the staged copy of %s changed after it was staged"
                             % record["path"])
        if known is not None and record["sha256"] in known:
            pass
        elif put_blob(store, path, record["sha256"]):
            uploaded += 1
        if known is not None:
            known.add(record["sha256"])
        done_bytes += record["size"]
        if progress:
            progress(done_bytes, total)

    store.put(manifest_key(game, snap_id), manifest_bytes(manifest))
    store.delete(pending_key(game, snap_id))
    return uploaded


def fetch(store, manifest, into):
    """Lay a snapshot out in `into`, and prove every file by its hash."""
    for record in manifest["files"]:
        target = os.path.join(into, *record["path"].split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        packed = store.get(blob_key(record["sha256"]))
        data = gzip.decompress(packed)
        if sha256_bytes(data) != record["sha256"]:
            raise StoreError("blob %s does not match its name" % record["sha256"][:12])
        with open(target, "wb") as handle:
            handle.write(data)
        stamp_time = parse_iso(record.get("mtime"))
        if stamp_time:
            ts = stamp_time.timestamp()
            os.utime(target, (ts, ts))
    return into


# ------------------------------------------------------------------ lineage


class GameView(object):
    """What the store holds for one game."""

    def __init__(self, game, manifests, pending):
        self.game = game
        self.manifests = manifests          # {id: manifest}
        self.pending = pending              # {id: intent}, not yet committed

    @property
    def heads(self):
        named = set()
        for manifest in self.manifests.values():
            named.update(manifest.get("parents") or [])
        return sorted((sid for sid in self.manifests if sid not in named),
                      key=lambda sid: self.sort_time(sid))

    def sort_time(self, snap_id):
        manifest = self.manifests.get(snap_id) or {}
        played = parse_iso((manifest.get("played") or {}).get("end"))
        return (played or snap_time(snap_id) or utc_now()).timestamp()

    def ancestors(self, snap_id):
        seen = set()
        todo = [snap_id]
        while todo:
            current = todo.pop()
            for parent in (self.manifests.get(current) or {}).get("parents") or []:
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        return seen

    def descends_from(self, snap_id, ancestor):
        return snap_id == ancestor or ancestor in self.ancestors(snap_id)

    def newest_head(self):
        heads = self.heads
        return heads[-1] if heads else None


def read_game(store, game, cache_dir=None):
    """List a game's snapshots and pending markers. Manifests are cached,
    because a manifest never changes once written."""
    keys = store.list(game_prefix(game))
    manifests = {}
    pending = {}
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        snap_id = name[:-5]
        if "/snapshots/" in key:
            manifests[snap_id] = _cached_manifest(store, key, snap_id, cache_dir)
        elif "/pending/" in key:
            pending[snap_id] = None
    for snap_id in list(pending):
        if snap_id in manifests:
            # Committed, and the marker was not cleared yet. Not pending.
            del pending[snap_id]
            continue
        try:
            pending[snap_id] = json.loads(store.get(pending_key(game, snap_id)))
        except (NotFound, ValueError):
            del pending[snap_id]
    return GameView(game, manifests, pending)


def _cached_manifest(store, key, snap_id, cache_dir):
    path = os.path.join(cache_dir, snap_id + ".json") if cache_dir else None
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as handle:
                return json.loads(handle.read())
        except (OSError, ValueError):
            pass
    data = store.get(key)
    manifest = json.loads(data)
    if path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError:
            pass
    return manifest


# What the picker does at launch.
LAUNCH = "launch"            # play on what is here
RESTORE = "restore"          # fetch this head and restore it first
ASK = "ask"                  # two real saves; a person decides
WAIT = "wait"                # another device is still uploading a newer save
UNKNOWN = "unknown"          # the store could not be read


def decide(view, base, local_hashes, device):
    """The launch decision, from lineage rather than clocks.

    `local_hashes` are the hashes of the save files on this device now (see
    save_hashes). Returns (action, detail): the head to restore, the heads to
    choose between, or the pending markers to wait for. LAUNCH with a detail
    means "the save here already is that snapshot; adopt it as the base".
    """
    if view is None:
        return UNKNOWN, None
    heads = view.heads
    newest_known = max([view.sort_time(h) for h in heads] or [0])
    others_pending = {sid: intent for sid, intent in view.pending.items()
                      if snap_device(sid) != device_key(device)
                      and (snap_time(sid) or utc_now()).timestamp() > newest_known}
    if others_pending:
        return WAIT, others_pending
    if not heads:
        return LAUNCH, None
    local_hashes = set(local_hashes or [])
    if len(heads) > 1:
        # A fork. If the save here is one of the heads, that head is still a
        # choice; the player picks between it and the others.
        return ASK, heads
    head = heads[0]
    head_hashes = save_hashes(view.manifests[head])
    if local_hashes and local_hashes == head_hashes:
        return (LAUNCH, None) if base == head else (LAUNCH, head)
    if base == head:
        # Played here since the last sync. The newer save is this one, and it
        # goes up at exit.
        return LAUNCH, None
    if base is None and local_hashes:
        # No base yet: the first launch after an import, or a new device.
        # When the save here is one the head already descends from, this
        # device simply fell behind, and restoring is not a choice between
        # two plays. Only a save the history has never seen is asked about.
        for ancestor in view.ancestors(head):
            if save_hashes(view.manifests.get(ancestor) or {}) == local_hashes:
                return RESTORE, head
    if base is not None and not view.descends_from(head, base):
        # The store moved on along a branch this device never had.
        return ASK, [head]
    base_hashes = save_hashes(view.manifests[base]) if base in view.manifests else set()
    changed = bool(local_hashes) and local_hashes != base_hashes
    if changed:
        return ASK, [head]
    return RESTORE, head


# ------------------------------------------------------------------ local state


class LocalState(object):
    """What this device remembers: its base per game, and its queue.

    bases.json: {game_key: {"base": snap_id, "merge": [snap_ids]}}
    queue/<seq>_<snap_id>/manifest.json + data/...

    The sequence number keeps staging order. Two snapshots staged in the
    same second have ids that sort by their hash, and a chain uploaded out
    of order would name a parent the store does not have yet.
    """

    def __init__(self, root):
        self.root = root
        self.queue_dir = os.path.join(root, "queue")
        self.cache_dir = os.path.join(root, "manifests")
        self._lock = threading.Lock()

    # -- bases

    def _bases_path(self):
        return os.path.join(self.root, "bases.json")

    def bases(self):
        try:
            with open(self._bases_path(), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def base(self, game):
        return (self.bases().get(game_key(game)) or {}).get("base")

    def merge(self, game):
        return list((self.bases().get(game_key(game)) or {}).get("merge") or [])

    def set_base(self, game, snap_id, merge=None):
        with self._lock:
            data = self.bases()
            data[game_key(game)] = {"base": snap_id, "merge": sorted(set(merge or []))}
            _write_json_atomic(self._bases_path(), data)

    # -- queue

    def _entries(self):
        """(seq, snap_id, dir name) for every finished queue entry, in order."""
        if not os.path.isdir(self.queue_dir):
            return []
        out = []
        for name in os.listdir(self.queue_dir):
            seq, _sep, snap_id = name.partition("_")
            if not seq.isdigit() or not snap_id:
                continue
            if not os.path.isfile(os.path.join(self.queue_dir, name, "manifest.json")):
                # A stage that never finished. Not a snapshot.
                continue
            out.append((int(seq), snap_id, name))
        return sorted(out)

    def _dir(self, snap_id):
        for _seq, sid, name in self._entries():
            if sid == snap_id:
                return os.path.join(self.queue_dir, name)
        return None

    def queued(self, game=None):
        """Queued snapshot ids, in the order they were staged."""
        out = []
        for _seq, snap_id, _name in self._entries():
            if game is not None:
                manifest = self.queued_manifest(snap_id)
                if manifest is None or game_key(manifest["game"]) != game_key(game):
                    continue
            out.append(snap_id)
        return out

    def queued_manifest(self, snap_id):
        folder = self._dir(snap_id)
        if folder is None:
            return None
        try:
            with open(os.path.join(folder, "manifest.json"), "r",
                      encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def queued_data(self, snap_id):
        folder = self._dir(snap_id)
        return os.path.join(folder, "data") if folder else None

    def _next_name(self, snap_id):
        entries = self._entries()
        seq = (entries[-1][0] + 1) if entries else 1
        return "%012d_%s" % (seq, snap_id)

    def parents_for(self, game):
        """What a new snapshot of this game names as its parents.

        The newest queued snapshot of this game if there is one, so a week of
        offline sessions forms a chain. Otherwise this device's base, plus any
        heads the player chose against, which is what closes a fork.
        """
        queued = self.queued(game)
        if queued:
            return [queued[-1]]
        base = self.base(game)
        parents = [base] if base else []
        return parents + self.merge(game)

    def stage(self, game, device, source_dir, played=None, mode="game",
              parents=None, created=None, unit=None):
        """Copy a backup into the queue and fsync it. Returns the manifest.

        The copy is what gets uploaded, so a game started again before the
        upload finishes cannot change what goes up.
        """
        with self._lock:
            if parents is None:
                parents = self.parents_for(game)
            os.makedirs(self.queue_dir, exist_ok=True)
            work = tempfile.mkdtemp(prefix=".stage-", dir=self.queue_dir)
            try:
                data = os.path.join(work, "data")
                shutil.copytree(source_dir, data)
                files = scan_dir(data)
                manifest = make_manifest(game, device, files, parents,
                                         played=played, mode=mode, created=created)
                if unit:
                    # Which library game this is, for display: not part of
                    # the id, so it never changes what "the same save" means.
                    manifest["unit"] = unit
                _fsync_tree(data)
                _write_json_atomic(os.path.join(work, "manifest.json"), manifest)
                final = os.path.join(self.queue_dir, self._next_name(manifest["id"]))
                os.replace(work, final)
            except BaseException:
                shutil.rmtree(work, ignore_errors=True)
                raise
            return manifest

    def stage_merge(self, game, device, chosen, heads):
        """Close a fork with no upload: a new snapshot holding the chosen
        head's files, naming every head as a parent. The blobs are already
        on the store, so this costs one small manifest."""
        with self._lock:
            files = list(chosen["files"])
            manifest = make_manifest(game, device, files, heads,
                                     played=chosen.get("played"),
                                     mode=chosen.get("mode") or "game")
            os.makedirs(self.queue_dir, exist_ok=True)
            work = tempfile.mkdtemp(prefix=".stage-", dir=self.queue_dir)
            os.makedirs(os.path.join(work, "data"))
            manifest["merge_only"] = True
            _write_json_atomic(os.path.join(work, "manifest.json"), manifest)
            os.replace(work, os.path.join(self.queue_dir,
                                          self._next_name(manifest["id"])))
            return manifest

    def restore_pending(self, game):
        try:
            with open(os.path.join(self.root, "restore_pending.json"), "r",
                      encoding="utf-8") as handle:
                return json.load(handle).get(game_key(game))
        except (OSError, ValueError):
            return None

    def set_restore_pending(self, game, snap_id):
        path = os.path.join(self.root, "restore_pending.json")
        with self._lock:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            if snap_id:
                data[game_key(game)] = snap_id
            else:
                data.pop(game_key(game), None)
            _write_json_atomic(path, data)

    def known_blobs(self):
        try:
            with open(os.path.join(self.root, "blobs.known"), "r") as handle:
                return set(line.strip() for line in handle if len(line.strip()) == 64)
        except OSError:
            return set()

    def save_known_blobs(self, known):
        path = os.path.join(self.root, "blobs.known")
        os.makedirs(self.root, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as handle:
            handle.write("\n".join(sorted(known)))
        os.replace(tmp, path)

    def done(self, snap_id):
        folder = self._dir(snap_id)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".json-", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _fsync_tree(root):
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                fd = os.open(os.path.join(folder, name), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass


# ------------------------------------------------------------------ uploading


class QueueLock(object):
    """One uploader per device, across processes.

    The picker and the daemon can both upload. A lock file created with
    O_EXCL decides which one does; a lock older than `stale` seconds belongs
    to a process that died and is taken over.
    """

    def __init__(self, path, stale=600):
        self.path = path
        self.stale = stale
        self.held = False

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    continue
                if age > self.stale:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        return False
                    continue
                return False
        return False

    def touch(self):
        if self.held:
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def release(self):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.held = False


def drain(store, state, progress=None, only=None):
    """Upload every queued snapshot, oldest first. Stops at the first failure.

    Returns (committed ids, error or None). An offline store is not an error
    worth shouting about: the queue is on disk and the next drain carries on.
    `only` limits the drain to one snapshot and the ones queued before it.
    """
    lock = QueueLock(os.path.join(state.root, "upload.lock"))
    if not lock.acquire():
        return [], None
    committed = []
    try:
        for snap_id in state.queued():
            manifest = state.queued_manifest(snap_id)
            if manifest is None:
                continue
            lock.touch()
            known = state.known_blobs()
            try:
                commit(store, manifest, state.queued_data(snap_id),
                       progress=(lambda done, total, sid=snap_id:
                                 progress(sid, done, total)) if progress else None,
                       known=known)
            except StoreError as exc:
                state.save_known_blobs(known)
                return committed, exc
            state.save_known_blobs(known)
            game = manifest["game"]
            # The base moves only when nothing newer of this game is queued
            # behind it, so a chain lands with the base on its last link.
            chain = state.queued(game)
            later = chain[chain.index(snap_id) + 1:] if snap_id in chain else []
            if not later:
                state.set_base(game, snap_id)
            state.done(snap_id)
            committed.append(snap_id)
            if only is not None and snap_id == only:
                break
        return committed, None
    finally:
        lock.release()


# ------------------------------------------------------------------ import


def _mapping_name(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                found = re.match(r'^name:\s*"?(.*?)"?\s*$', line)
                if found:
                    return found.group(1).replace('\\"', '"')
    except OSError:
        pass
    return None


def filter_mapping(text, backup_name):
    """A mapping.yaml that lists one backup only.

    ludusavi restores the newest backup its mapping lists. A snapshot holds
    one backup folder, so its mapping must name that one and no other, or a
    restore would look for a folder the snapshot does not have. Everything
    before `backups:` is kept; of the list, only the entry for backup_name.
    """
    out = []
    lines = text.splitlines(True)
    index = 0
    while index < len(lines):
        out.append(lines[index])
        if lines[index].rstrip() == "backups:":
            index += 1
            break
        index += 1
    keep = False
    for line in lines[index:]:
        entry = re.match(r"^  - name:\s*\"?([^\"\s]+)\"?\s*$", line)
        if entry:
            keep = entry.group(1) == backup_name
        elif not line.startswith("  ") and line.strip():
            # A new top-level key after the list. Keep it.
            keep = True
        if keep:
            out.append(line)
    return "".join(out)


def find_ludusavi_backups(root):
    """Every ludusavi backup under a Syncthing gamesaves folder.

    Yields (device, game, game_dir, backup_name, when), oldest first per
    device and game. The layout is <root>/<device>/<game dir>/mapping.yaml
    plus one backup-<stamp> folder per backup.
    """
    found = []
    for device in sorted(os.listdir(root)):
        device_dir = os.path.join(root, device)
        if not os.path.isdir(device_dir) or device.startswith("."):
            continue
        for game_dir in sorted(os.listdir(device_dir)):
            folder = os.path.join(device_dir, game_dir)
            mapping = os.path.join(folder, "mapping.yaml")
            if not os.path.isfile(mapping):
                continue
            game = _mapping_name(mapping) or game_dir
            for name in sorted(os.listdir(folder)):
                match = re.match(r"^backup-(\d{8}T\d{6})Z$", name)
                if not match or not os.path.isdir(os.path.join(folder, name)):
                    continue
                when = datetime.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
                    tzinfo=datetime.timezone.utc)
                found.append((device, game, folder, name, when))
    return found


def newest_save_time(manifest):
    """The newest save file's time in a manifest, ludusavi metadata left out."""
    times = [record.get("mtime") or "" for record in manifest.get("files") or []
             if record["path"].rsplit("/", 1)[-1] not in METADATA_NAMES]
    return max(times) if times else ""


def import_backups(store, root, dry_run=False, progress=None, known=None):
    """Put every ludusavi backup in a Syncthing folder on the store.

    Each backup becomes a snapshot. Backups from one device chain in time
    order. Where more than one device has backups of a game, a merge snapshot
    of the newest one names every device's last snapshot as a parent, so the
    game starts with one head instead of a fork nobody made.

    Safe to run twice: ids come from content and time, and blobs already on
    the store are skipped. Returns {game: head id}.
    """
    backups = find_ludusavi_backups(root)
    last = {}           # (game, device) -> manifest
    heads = {}
    for index, (device, game, folder, name, when) in enumerate(backups):
        work = tempfile.mkdtemp(prefix="blockslot-import-")
        try:
            game_copy = os.path.join(work, os.path.basename(folder))
            os.makedirs(game_copy)
            with open(os.path.join(folder, "mapping.yaml"), "r", encoding="utf-8") as handle:
                mapping = filter_mapping(handle.read(), name)
            with open(os.path.join(game_copy, "mapping.yaml"), "w", encoding="utf-8",
                      newline="") as handle:
                handle.write(mapping)
            # Keep the original's time. The manifest records every file's
            # time, so a fresh one would give a re-run new ids and a second
            # copy of every chain beside the first.
            original = os.stat(os.path.join(folder, "mapping.yaml"))
            os.utime(os.path.join(game_copy, "mapping.yaml"),
                     (original.st_atime, original.st_mtime))
            shutil.copytree(os.path.join(folder, name), os.path.join(game_copy, name))
            previous = last.get((game, device))
            manifest = make_manifest(
                game, device, scan_dir(work),
                [previous["id"]] if previous else [],
                played={"end": iso(when)}, created=when)
            manifest["imported"] = True
            if not dry_run:
                commit(store, manifest, work, known=known)
            last[(game, device)] = manifest
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if progress:
            progress(index + 1, len(backups), game, device)

    games = {}
    for (game, _device), manifest in last.items():
        games.setdefault(game, []).append(manifest)
    for game, tips in games.items():
        # Newest SAVE wins, as it did under Syncthing, not the newest backup.
        # The first backups on every device were one bulk run minutes apart,
        # so the backup time says nothing about which save is newer.
        tips.sort(key=lambda m: (newest_save_time(m), m["created"]))
        if len(tips) == 1:
            heads[game] = tips[0]["id"]
            continue
        newest = tips[-1]
        merge = make_manifest(game, newest["device"], list(newest["files"]),
                              [m["id"] for m in tips], played=newest.get("played"),
                              created=parse_iso(newest["created"]))
        merge["merge_only"] = True
        merge["imported"] = True
        if not dry_run:
            commit(store, merge, "")
        heads[game] = merge["id"]
    return heads


# ------------------------------------------------------------------ at a glance


def newest_on_store(store):
    """{game_key: (iso time, device)} of the newest snapshot of every game.

    One listing, no manifest reads: a snapshot's id carries its time and its
    device, which is all a games list shows. Merge snapshots count, because
    one is what a device restores after a fork is closed.
    """
    newest = {}
    for key in store.list(PREFIX + "games/"):
        parts = key.split("/")
        if len(parts) < 3 or parts[-2] != "snapshots" or not parts[-1].endswith(".json"):
            continue
        snap_id = parts[-1][:-5]
        when = snap_time(snap_id)
        if when is None:
            continue
        game = parts[-3]
        current = newest.get(game)
        if current is None or snap_id > current[2]:
            newest[game] = (iso(when), snap_device(snap_id), snap_id)
    return {game: (when, device) for game, (when, device, _sid) in newest.items()}


# ------------------------------------------------------------------ trees


def newest_per_device(view):
    """{device: manifest} of the newest snapshot each device has of a game.

    A save set (a retro frontend's whole saves folder) is merged file by file,
    newest copy of each file wins, exactly as it was under Syncthing. So what
    matters is each device's latest tree, not a single head.
    """
    by_device = {}
    for snap_id, manifest in view.manifests.items():
        if manifest.get("merge_only"):
            continue
        by_device.setdefault(manifest.get("device") or snap_device(snap_id), []).append(snap_id)
    best = {}
    for device, sids in by_device.items():
        # Follow the chain, not the id: two snapshots made in the same second
        # have ids that sort by hash. A snapshot another one of this device's
        # descends from is older, whatever its name says.
        older = set()
        for sid in sids:
            older.update(view.ancestors(sid))
        tips = sorted(sid for sid in sids if sid not in older)
        best[device] = view.manifests[tips[-1]]
    return best


def fetch_blobs(store, items):
    """Write single files from blobs. items: [{sha256, path, mtime}].

    Returns (written, failed). A tree merge copies a handful of files out of
    thousands, so it fetches only those.
    """
    written, failed = 0, []
    for item in items:
        try:
            data = gzip.decompress(store.get(blob_key(item["sha256"])))
            if sha256_bytes(data) != item["sha256"]:
                raise StoreError("blob does not match its name")
            target = item["path"]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".blockslot-tmp"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
            when = parse_iso(item.get("mtime"))
            if when:
                os.utime(target, (when.timestamp(), when.timestamp()))
            written += 1
        except (StoreError, OSError) as exc:
            failed.append((item.get("path"), str(exc)))
    return written, failed


# ------------------------------------------------------------------ clean-up

KEEP_PER_DEVICE = 10
KEEP_DAYS = 30
BLOB_GRACE_DAYS = 7
PENDING_QUIET_HOURS = 1


def clean(store, now=None, dry_run=False, log=None):
    """Remove old snapshots and the blobs nothing names any more.

    Kept: every head, the newest KEEP_PER_DEVICE snapshots of each game from
    each device, every snapshot younger than KEEP_DAYS, and every parent a
    kept merge snapshot names. A blob goes only when no kept manifest and no
    pending marker names it AND it is older than BLOB_GRACE_DAYS, because
    another device may have uploaded it for a manifest it has not written
    yet. Nothing runs while any upload started within the last hour.
    """
    say = log or (lambda _msg: None)
    now = now or utc_now()
    keys = store.list(PREFIX + "games/")
    games = sorted(set(key.split("/")[3] for key in keys if key.count("/") >= 4))
    for key in keys:
        if "/pending/" in key:
            when = snap_time(key.rsplit("/", 1)[-1][:-5])
            if when and (now - when).total_seconds() < PENDING_QUIET_HOURS * 3600:
                say("clean: an upload is in progress; not cleaning now")
                return {"removed_snapshots": 0, "removed_blobs": 0, "skipped": True}
    committed = set(key.rsplit("/", 1)[-1] for key in keys if "/snapshots/" in key)
    for key in keys:
        if "/pending/" not in key or key.rsplit("/", 1)[-1] in committed:
            continue
        when = snap_time(key.rsplit("/", 1)[-1][:-5])
        if when and (now - when).days >= BLOB_GRACE_DAYS:
            # An upload that started a week ago and never finished: the
            # device that began it was reset or gave up. Nobody waits on it.
            say("clean: removing abandoned upload marker %s" % key)
            if not dry_run:
                store.delete(key)
    removed = 0
    referenced = set()
    for game_dir in games:
        prefix = "%sgames/%s/" % (PREFIX, game_dir)
        snaps = {}
        for key in keys:
            if key.startswith(prefix + "snapshots/") and key.endswith(".json"):
                snaps[key.rsplit("/", 1)[-1][:-5]] = key
            elif key.startswith(prefix + "pending/") and key.endswith(".json"):
                try:
                    intent = json.loads(store.get(key))
                    referenced.update(intent.get("blobs") or [])
                except (StoreError, ValueError):
                    pass
        manifests = {sid: json.loads(store.get(key)) for sid, key in snaps.items()}
        view = GameView(game_dir, manifests, {})
        keep = set(view.heads)
        by_device = {}
        for sid in manifests:
            by_device.setdefault(snap_device(sid), []).append(sid)
        for sids in by_device.values():
            keep.update(sorted(sids)[-KEEP_PER_DEVICE:])
        for sid in manifests:
            when = snap_time(sid)
            if when and (now - when).days < KEEP_DAYS:
                keep.add(sid)
        for sid in list(keep):
            if manifests.get(sid, {}).get("merge_only"):
                keep.update(manifests[sid].get("parents") or [])
        for sid, manifest in manifests.items():
            if sid in keep:
                referenced.update(r["sha256"] for r in manifest.get("files") or [])
                continue
            say("clean: removing snapshot %s" % sid)
            removed += 1
            if not dry_run:
                store.delete(snaps[sid])
    blobs_removed = 0
    for key, when in store.list_times(PREFIX + "blobs/"):
        sha = key.rsplit("/", 1)[-1]
        if sha in referenced:
            continue
        if when is None or (now - when).days < BLOB_GRACE_DAYS:
            continue
        blobs_removed += 1
        if not dry_run:
            store.delete(key)
    say("clean: %d snapshot(s) and %d blob(s) %s"
        % (removed, blobs_removed, "would go" if dry_run else "removed"))
    return {"removed_snapshots": removed, "removed_blobs": blobs_removed, "skipped": False}


# ------------------------------------------------------------------ libraries
#
# An emulator's saves folder holds many games. Each game in it is a unit,
# with its own snapshots and history, stored like any game under a name that
# says which library it came from. See saveunits.py for the split itself.

LIBRARY_PREFIX = "lib-"


def library_unit_name(library, unit_id):
    """The store name of one game in a library.

    game_key() is lossy: "Mortal Kombat II (U) [!]" and "Mortal Kombat II (U)"
    make the same key. A hash of the exact unit id keeps them apart, and the
    whole name is already a key, so game_key() leaves it as it is.
    """
    digest = hashlib.sha1(unit_id.encode("utf-8")).hexdigest()[:8]
    return "%s%s--%s-%s" % (LIBRARY_PREFIX, game_key(library), game_key(unit_id)[:80], digest)


def library_prefix(library):
    return "%s%s--" % (LIBRARY_PREFIX, game_key(library))


def library_views(store, library, cache_dir=None, workers=16):
    """{unit name: GameView} for every unit of a library on the store.

    One listing of the store, then only the manifests not already cached,
    read in parallel: a library holds well over a thousand games.
    """
    import concurrent.futures
    prefix = PREFIX + "games/" + library_prefix(library)
    by_unit = {}
    for key in store.list(prefix):
        parts = key.split("/")
        if len(parts) < 3 or not parts[-1].endswith(".json"):
            continue
        unit = parts[-3]
        entry = by_unit.setdefault(unit, {"snapshots": {}, "pending": set()})
        if parts[-2] == "snapshots":
            entry["snapshots"][parts[-1][:-5]] = key
        elif parts[-2] == "pending":
            entry["pending"].add(parts[-1][:-5])
    jobs = [(unit, sid, key) for unit, entry in by_unit.items()
            for sid, key in entry["snapshots"].items()]
    manifests = {}

    def one(job):
        unit, sid, key = job
        return unit, sid, _cached_manifest(store, key, sid, cache_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for unit, sid, manifest in pool.map(one, jobs):
            manifests.setdefault(unit, {})[sid] = manifest
    views = {}
    for unit, entry in by_unit.items():
        pending = {sid: None for sid in entry["pending"]
                   if sid not in entry["snapshots"]}
        views[unit] = GameView(unit, manifests.get(unit, {}), pending)
    return views
