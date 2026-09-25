"""auth - who may use the BlockSlot web UI.

Two ways in, and either is enough (the server design, "Login"):

1. Local accounts: a username and a password, kept as scrypt hashes.
2. Cloudflare Access: a verified Cf-Access-Jwt-Assertion for an allowed email.

Either way the browser then holds one session cookie. Sessions live on the
server, keyed by the SHA-256 of the cookie value, so the sessions file on
disk cannot be replayed as cookies by someone who reads it.

A Cloudflare header that fails verification is ignored and logged. It is
never trusted: this port may be reachable on the LAN without Cloudflare in
front of it, and anyone there can send any header.

Standard library only. The Cloudflare check is RS256 by hand (see verify_rs256),
so the image carries no third-party package to keep patched.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.request

log = logging.getLogger("blockslot.auth")

# ------------------------------------------------------------------ passwords

# 16 MiB of memory per check: slow enough that a stolen server.json is
# expensive to crack, fast enough on a NAS CPU (about 50 ms).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD = 8


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex())


def check_password(password, stored):
    """True when password matches. The parameters come from the stored hash,
    so hashes made with other costs keep working."""
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(digest) // 2)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def password_problem(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return "Use at least %d characters." % MIN_PASSWORD
    return None


# ------------------------------------------------------------------ sessions

SESSION_COOKIE = "blockslot_session"
SESSION_IDLE = 14 * 24 * 3600
SESSION_MAX = 60 * 24 * 3600


def _digest(session_id):
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class Sessions(object):
    """The session store: {sha256(cookie): {user, via, csrf, created, seen}}.

    Kept in a file so a restart of the web app does not sign everyone out.
    `seen` is written back at most once a minute per session, so reading a
    page does not rewrite the file on every request.
    """

    def __init__(self, path=None, now=time.time):
        self.path = path
        self.now = now
        self.lock = threading.Lock()
        self.items = {}
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self.items = json.load(handle)
            except (OSError, ValueError):
                self.items = {}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.items, handle)
        os.replace(tmp, self.path)

    def create(self, user, via="password"):
        """A new session. Returns (cookie value, record)."""
        session_id = secrets.token_urlsafe(32)
        now = self.now()
        record = {"user": user, "via": via, "csrf": secrets.token_urlsafe(24),
                  "created": now, "seen": now}
        with self.lock:
            self._expire(now)
            self.items[_digest(session_id)] = record
            self._save()
        return session_id, dict(record)

    def get(self, session_id):
        if not session_id:
            return None
        now = self.now()
        with self.lock:
            record = self.items.get(_digest(session_id))
            if record is None:
                return None
            if now - record["seen"] > SESSION_IDLE or now - record["created"] > SESSION_MAX:
                del self.items[_digest(session_id)]
                self._save()
                return None
            if now - record["seen"] > 60:
                record["seen"] = now
                self._save()
            return dict(record)

    def delete(self, session_id):
        with self.lock:
            if self.items.pop(_digest(session_id or ""), None) is not None:
                self._save()

    def delete_user(self, user, keep=None):
        """End every session of a user, except `keep` (the one asking)."""
        keep_digest = _digest(keep) if keep else None
        with self.lock:
            for digest in [d for d, r in self.items.items()
                           if r["user"] == user and d != keep_digest]:
                del self.items[digest]
            self._save()

    def _expire(self, now):
        for digest in [d for d, r in self.items.items()
                       if now - r["seen"] > SESSION_IDLE or now - r["created"] > SESSION_MAX]:
            del self.items[digest]


def cookie_header(session_id, secure, clear=False):
    parts = ["%s=%s" % (SESSION_COOKIE, "" if clear else session_id), "Path=/",
             "HttpOnly", "SameSite=Lax"]
    parts.append("Max-Age=0" if clear else "Max-Age=%d" % SESSION_MAX)
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def read_cookie(header, name=SESSION_COOKIE):
    for part in (header or "").split(";"):
        key, _sep, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def is_https(headers):
    """Did this request reach us over HTTPS, directly or through a proxy?

    A spoofed header can only make the cookie Secure, which harms nobody but
    the one spoofing it.
    """
    if (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https":
        return True
    visitor = headers.get("Cf-Visitor") or ""
    return '"https"' in visitor.replace(" ", "")


# ------------------------------------------------------------------ CSRF


def same_origin(headers):
    """False when the browser says this request came from another site.

    Origin is sent on every cross-site POST by every current browser; when it
    is missing, Referer is the fallback. A request with neither is not from a
    browser page, and is left to the token check.
    """
    source = headers.get("Origin") or headers.get("Referer")
    if not source:
        return True
    from urllib.parse import urlsplit
    origin_host = urlsplit(source).netloc.lower()
    hosts = {(headers.get("Host") or "").lower()}
    forwarded = (headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()
    if forwarded:
        hosts.add(forwarded)
    return origin_host in hosts


def csrf_ok(session, token):
    return bool(session and token) and hmac.compare_digest(session["csrf"], token)


# ------------------------------------------------------------------ rate limit


class LoginLimiter(object):
    """Slow down password guessing.

    Five failures for one username from one address lock that pair out for
    15 minutes; twenty failures for one username from anywhere lock the name
    for 15 minutes. A success clears the pair. Kept in memory: a restart
    clearing it costs an attacker a restart they cannot cause.
    """

    WINDOW = 15 * 60
    PER_PAIR = 5
    PER_USER = 20

    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.failures = {}          # key -> [times]

    def _recent(self, key, now):
        times = [t for t in self.failures.get(key, []) if now - t < self.WINDOW]
        self.failures[key] = times
        return times

    def blocked(self, user, address):
        """Seconds until this pair may try again, or 0."""
        now = self.now()
        user = (user or "").lower()
        with self.lock:
            waits = []
            for key, limit in (((user, address), self.PER_PAIR), ((user, None), self.PER_USER)):
                times = self._recent(key, now)
                if len(times) >= limit:
                    waits.append(int(times[-limit] + self.WINDOW - now) + 1)
            return max(waits) if waits else 0

    def failed(self, user, address):
        now = self.now()
        user = (user or "").lower()
        with self.lock:
            for key in ((user, address), (user, None)):
                self._recent(key, now).append(now)

    def succeeded(self, user, address):
        with self.lock:
            self.failures.pop(((user or "").lower(), address), None)


# ------------------------------------------------------------------ Cloudflare Access


def _fetch(url, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": "BlockSlot-server/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ASN.1 DigestInfo for SHA-256 (RFC 8017, 9.2 note 1).
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
MIN_RSA_BITS = 2048
CLOCK_LEEWAY = 30


class TokenError(Exception):
    pass


def _b64url(text):
    if isinstance(text, str):
        text = text.encode("ascii")
    return base64.urlsafe_b64decode(text + b"=" * (-len(text) % 4))


def rsa_public_key(jwk):
    """(n, e) from an RSA JWK. Refuses anything that is not an RSA signing key."""
    if jwk.get("kty") != "RSA" or jwk.get("alg", "RS256") != "RS256":
        raise TokenError("not an RS256 key")
    n = int.from_bytes(_b64url(jwk["n"]), "big")
    e = int.from_bytes(_b64url(jwk["e"]), "big")
    if n.bit_length() < MIN_RSA_BITS or e < 3 or e % 2 == 0:
        raise TokenError("weak RSA key")
    return n, e


def token_header(token):
    try:
        header = json.loads(_b64url(token.split(".")[0]))
    except (ValueError, IndexError, UnicodeDecodeError):
        raise TokenError("malformed token")
    if not isinstance(header, dict):
        raise TokenError("malformed token")
    return header


def verify_rs256(token, key):
    """The claims of an RS256 JWT whose signature `key` (n, e) made.

    RSASSA-PKCS1-v1_5 verification the way RFC 8017 8.2.2 says to do it:
    build the whole expected encoding and compare it byte for byte, never
    parse the decrypted block. Parsing it is where forgeries come from.
    Raises TokenError for anything else, including every other "alg".
    """
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise TokenError("malformed token")
    if token_header(token).get("alg") != "RS256":
        raise TokenError("only RS256 is accepted")
    n, e = key
    size = (n.bit_length() + 7) // 8
    try:
        signature = _b64url(parts[2])
    except ValueError:
        raise TokenError("malformed signature")
    number = int.from_bytes(signature, "big")
    if len(signature) != size or number >= n:
        raise TokenError("bad signature")
    decoded = pow(number, e, n).to_bytes(size, "big")
    digest = hashlib.sha256(("%s.%s" % (parts[0], parts[1])).encode("ascii")).digest()
    tail = SHA256_DIGEST_INFO + digest
    expected = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    if not hmac.compare_digest(decoded, expected):
        raise TokenError("bad signature")
    try:
        claims = json.loads(_b64url(parts[1]))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed claims")
    if not isinstance(claims, dict):
        raise TokenError("malformed claims")
    return claims


def check_claims(claims, audience, issuer, now):
    """Raises TokenError unless the token is current, for us, from the team."""
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + CLOCK_LEEWAY:
        raise TokenError("expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + CLOCK_LEEWAY < nbf:
        raise TokenError("not valid yet")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if audience not in auds:
        raise TokenError("wrong audience")
    if claims.get("iss") != issuer:
        raise TokenError("wrong issuer")


class AccessVerifier(object):
    """Checks Cloudflare Access's signed header against the team's keys.

    The keys are cached by kid. A token signed with a kid not in the cache
    makes one refetch, because Cloudflare rotates its keys; refetches are
    spaced by REFETCH_GAP so a stream of forged kids cannot turn this server
    into a flood against Cloudflare.
    """

    REFETCH_GAP = 60

    def __init__(self, fetch=None, now=time.time):
        self.fetch = fetch or _fetch
        self.now = now
        self.lock = threading.Lock()
        self.keys = {}              # (team, kid) -> key
        self.fetched = {}           # team -> when

    @staticmethod
    def team_url(team_domain):
        team = (team_domain or "").strip().rstrip("/")
        if "://" not in team:
            team = "https://" + team
        return team

    def _load(self, team):
        raw = json.loads(self.fetch(team + "/cdn-cgi/access/certs"))
        found = {}
        for jwk in raw.get("keys") or []:
            try:
                found[(team, jwk["kid"])] = rsa_public_key(jwk)
            except (KeyError, ValueError, TokenError):
                continue
        self.keys = {k: v for k, v in self.keys.items() if k[0] != team}
        self.keys.update(found)
        self.fetched[team] = self.now()

    def _key(self, team, kid):
        with self.lock:
            key = self.keys.get((team, kid))
            if key is not None:
                return key
            if self.now() - self.fetched.get(team, 0) < self.REFETCH_GAP:
                return None
            self._load(team)
            return self.keys.get((team, kid))

    def verify(self, token, team_domain, audience):
        """The verified email, or None. Never raises for a bad token."""
        if not (token and team_domain and audience):
            return None
        team = self.team_url(team_domain)
        try:
            kid = token_header(token).get("kid")
            key = self._key(team, kid)
            if key is None:
                log.warning("Cloudflare Access header ignored: unknown signing key")
                return None
            claims = verify_rs256(token, key)
            check_claims(claims, audience, team, self.now())
        except Exception as exc:                     # noqa: BLE001, any failure means "not signed in"
            log.warning("Cloudflare Access header ignored: %s", exc)
            return None
        email = (claims.get("email") or "").strip().lower()
        return email or None


def email_allowed(email, allowed):
    email = (email or "").strip().lower()
    return bool(email) and email in {e.strip().lower() for e in allowed or [] if e.strip()}
