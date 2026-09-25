"""The engine that ships with Blockslot, and the copy installed on this device.

Both faces install it and both say whether the installed copy is current. The
rule lives here so that one of them cannot quietly install it differently.
"""

import hashlib
import os
import shutil
import subprocess

from . import paths

# The ludusavi release a Windows PC downloads when it has none: the same
# version decky/package.json pins for the Deck. The hash is the one GitHub
# publishes for this asset, and it was checked against a download of it.
LUDUSAVI_VERSION = "0.31.0"
LUDUSAVI_WINDOWS_URL = (
    "https://github.com/mtkennerly/ludusavi/releases/download/v0.31.0/"
    "ludusavi-v0.31.0-win64.zip")
LUDUSAVI_WINDOWS_SHA256 = (
    "f47a8ad8c708f01d2eb124704973beffab205e292f5287a10fc4a101f8d68706")
LUDUSAVI_WINDOWS_MEMBER = "ludusavi.exe"
DOWNLOAD_TIMEOUT = 60

# The store library and the daemon travel with the engine and are installed
# beside it: savepick imports them from its own folder.
COMPANIONS = ("slotstore.py", "slotd.py", "saveunits.py")


def source(extra=None):
    """The engine that came with this copy of Blockslot, or None.

    `extra` is a path to try first, for a face that carries the engine
    somewhere `paths.resource` does not look.
    """
    if extra is not None and extra.is_file():
        return extra
    return paths.resource("engine/" + paths.ENGINE_NAME)


def is_current(shipped):
    """True when the installed engine, and each companion shipped with it,
    is byte for byte the shipped one."""
    installed = paths.engine_path()
    try:
        pairs = [(installed, shipped)] + [
            (installed.parent / name, shipped.parent / name)
            for name in COMPANIONS if (shipped.parent / name).is_file()]
        for have, want in pairs:
            if have.stat().st_size != want.stat().st_size:
                return False
            if have.read_bytes() != want.read_bytes():
                return False
        return True
    except (OSError, AttributeError):
        return False


def install(shipped):
    """Copy the shipped engine to where Steam will start it. Raises OSError."""
    target = paths.engine_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    for name in COMPANIONS:
        if (shipped.parent / name).is_file():
            shutil.copy2(str(shipped.parent / name), str(target.parent / name))
    shutil.copy2(str(shipped), str(target))
    # Steam starts it through python, but a person may start it by hand.
    os.chmod(str(target), 0o755)
    return target


class AlreadyThere(Exception):
    """ludusavi is already installed, and is never replaced from here."""


class WrongFile(OSError):
    """The download is not the file that was pinned."""


def sha256_of(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_ludusavi(url=LUDUSAVI_WINDOWS_URL, sha256=LUDUSAVI_WINDOWS_SHA256,
                      say=None, opener=None):
    """Fetch ludusavi's official Windows release, check it, and install it.

    Only the pinned file is accepted: the zip has to hash to `sha256` before
    anything is taken out of it. A ludusavi that is already there is never
    replaced, and that is decided before anything is downloaded.

    `opener(url)` returns something with read(); the default is urllib over a
    verifying TLS context. `say` hears progress in plain words. Raises
    AlreadyThere, or OSError with a sentence a person can act on.
    """
    say = say or (lambda _text: None)
    target = paths.ludusavi_path()
    if target.exists():
        raise AlreadyThere("ludusavi is already installed at %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.parent / "ludusavi-download.blockslot.tmp"
    say("Downloading ludusavi %s ..." % LUDUSAVI_VERSION)
    try:
        try:
            response = (opener or _open)(url)
        except OSError as exc:
            raise OSError("Could not download ludusavi. Check that this PC "
                          "is online. (%s)" % _reason(exc))
        got = 0
        with response, open(str(download), "wb") as out:
            while True:
                try:
                    block = response.read(1 << 16)
                except OSError as exc:
                    raise OSError("The ludusavi download stopped part way. "
                                  "Try again. (%s)" % _reason(exc))
                if not block:
                    break
                out.write(block)
                got += len(block)
                if got % (1 << 21) < len(block):
                    say("Downloading ludusavi ... %.0f MB" % (got / 1048576.0))
        say("Checking the download ...")
        install_ludusavi(download, sha256=sha256)
    finally:
        if download.exists():
            download.unlink()
    say("Installed ludusavi at %s" % target)
    return target


def _open(url):
    import urllib.request
    context = None
    try:
        from . import settings as settings_mod
        context = settings_mod.engine_module("slotstore").tls_context()
    except ImportError:
        # Without the engine's helper, urllib's own default still verifies.
        context = None
    return urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT,
                                  context=context)


def _reason(exc):
    return getattr(exc, "reason", None) or exc


def install_ludusavi(archive, sha256=None):
    """Unpack ludusavi's own release archive to where the engine looks for it.

    `archive` is the .tar.gz or the Windows .zip that ludusavi publishes,
    exactly as downloaded. Blockslot does not carry a copy of ludusavi: each
    device fetches the official file, and this only unpacks it. With `sha256`
    the archive must hash to it first, or WrongFile is raised and nothing is
    written.

    An installed ludusavi is left alone. It may be newer than the pinned
    release, or built by hand, and replacing it would change how saves are
    read on a device that is already syncing.
    """
    target = paths.ludusavi_path()
    if target.exists():
        raise AlreadyThere("ludusavi is already installed at %s" % target)
    if sha256 is not None and sha256_of(archive) != sha256.lower():
        raise WrongFile("The ludusavi download is not the expected file, so "
                        "it was not installed. Try again later.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / (target.name + ".blockslot.tmp")
    try:
        if str(archive).lower().endswith(".zip") or _is_zip(archive):
            _unzip_member(archive, LUDUSAVI_WINDOWS_MEMBER, temp)
        else:
            _unpack_member(archive, target.name, temp)
        os.chmod(str(temp), 0o755)
        os.replace(str(temp), str(target))
    finally:
        if temp.exists():
            temp.unlink()
    return target


def _is_zip(archive):
    import zipfile
    try:
        return zipfile.is_zipfile(str(archive))
    except OSError:
        return False


def _unzip_member(archive, name, destination):
    """Write one file out of a .zip. Raises OSError when it is not there."""
    import zipfile
    try:
        with zipfile.ZipFile(str(archive)) as bundle:
            try:
                member = bundle.open(name)
            except KeyError:
                raise OSError("%s holds no file called %s" % (archive, name))
            with member, open(str(destination), "wb") as out:
                shutil.copyfileobj(member, out)
    except zipfile.BadZipFile as exc:
        raise OSError("could not unpack %s: %s" % (archive, exc))


def _unpack_member(archive, name, destination):
    """Write one file out of a .tar.gz. Raises OSError when it is not there."""
    try:
        import tarfile
    except ImportError:
        # Decky's python is a frozen build with part of the standard library
        # left out. SteamOS always has tar.
        with open(str(destination), "wb") as out:
            done = subprocess.run(["tar", "-xzOf", str(archive), name],
                                  stdout=out, stderr=subprocess.PIPE)
        if done.returncode != 0 or not destination.stat().st_size:
            raise OSError("tar could not read %s from %s" % (name, archive))
        return
    try:
        with tarfile.open(str(archive), "r:gz") as bundle:
            member = bundle.extractfile(name)
            if member is None:
                raise OSError("%s holds no file called %s" % (archive, name))
            with open(str(destination), "wb") as out:
                shutil.copyfileobj(member, out)
    except (tarfile.TarError, KeyError) as exc:
        raise OSError("could not unpack %s: %s" % (archive, exc))
