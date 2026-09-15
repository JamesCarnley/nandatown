"""Credentials written into an endpoint URL: used, never recorded.

An operator can reach an endpoint that wants basic authentication by
writing the credentials into its URL, as in ``http://user:secret@host``;
httpx sends them. The endpoint is tested exactly as written. What Town
prints and records is the same URL with its user information replaced by
a label:

- ``<credentials 1a2b3c4d>`` in evidence Town records and in Pulse history:
  a keyed digest of the credentials httpx would send, so a report can tell
  two sets of credentials for one host apart. The key is a random secret
  kept in the Town home and never recorded, so the label reveals nothing
  about a password, however short, to anyone who holds a bundle or report.
- ``<credentials withheld>`` in anything meant to travel, such as a
  receipt, when displaying evidence recorded before labelling existed, and
  whenever the key cannot be used.

Credentials are found where httpx finds them, in the URL's own authority,
not by searching text for things that look like URLs. A run registers the
operator's locator, and only the exact credentials it carries are replaced
in what the run records, so an agent's own text is never rewritten. Only
user information is recognised: a secret in a query string or a header is
not.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import tempfile
from typing import Any, Callable
from urllib.parse import unquote

import httpx

WITHHELD = "<credentials withheld>"
KEY_FILENAME = "url-credentials.key"
_KEY_BYTES = 32
_SCHEME = re.compile(r"(?i)^[a-z][a-z0-9+.\-]*://")
_LABEL_USERINFO = re.compile(r"^<credentials (?:[0-9a-f]{8}|withheld)>$")


class CredentialKeyError(ValueError):
    """The Town home's labelling key exists but cannot be used."""


def town_home() -> str:
    return os.environ.get("NANDATOWN_HOME",
                          os.path.expanduser("~/.nandatown"))


def _parses(url: str) -> httpx.URL | None:
    try:
        parsed = httpx.URL(url)
        parsed.host  # a punycode host decodes, and can raise, only here
        return parsed
    except (httpx.InvalidURL, UnicodeError):
        return None


def _split(url: object) -> tuple[str, str, str] | None:
    """(scheme and "://", user information, "@" and the rest), or None.

    The authority runs to the first "/", "?" or "#", and the user
    information is everything before its last "@": httpx's own rule, so a
    password holding a quote, a bracket or a space is found where httpx
    finds it. A URL httpx cannot parse may hold credentials past such a
    character, which is exactly what made it unparseable; there, everything
    before the last "@" counts, because a URL that cannot be used is better
    hidden too widely than not enough.
    """
    if not isinstance(url, str):
        return None
    scheme = _SCHEME.match(url)
    if scheme is None:
        return None
    prefix = scheme.group(0)
    remainder = url[len(prefix):]
    ends = [i for i in (remainder.find(c) for c in "/?#") if i >= 0]
    at = remainder[:min(ends, default=len(remainder))].rfind("@")
    parsed = _parses(url)
    if parsed is not None:
        if at < 0 or not (parsed.username or parsed.password):
            return None  # httpx sends no credentials for this URL
    elif at < 0:
        at = remainder.rfind("@")
        if at <= 0:
            return None
    userinfo = remainder[:at]
    if not userinfo:
        return None
    return prefix, userinfo, remainder[at:]


def has_credentials(url: object) -> bool:
    """Whether url carries credentials that are not already a label."""
    split = _split(url)
    return split is not None and not _LABEL_USERINFO.match(split[1])


def withhold(url: str) -> str:
    """url with its credentials, raw or labelled, replaced by WITHHELD."""
    split = _split(url)
    if split is None:
        return url
    prefix, _userinfo, rest = split
    return f"{prefix}{WITHHELD}{rest}"


def safe_message(url: object, message: str) -> str:
    """message, safe to print beside a URL that may carry credentials.

    httpx's own error for a URL it cannot parse can quote part of it, and
    for a password holding "#", "/" or "?" that part is the password
    itself ("Invalid port: 'Hash'"). Such a message is not repeated; any
    other message has the URL's exact credentials withheld.
    """
    if not has_credentials(url):
        return message
    if _parses(url) is None:
        return "the URL cannot be parsed; its credentials are not shown"
    scrubber = Scrubber(Labeller(withhold_only=True))
    scrubber.register(url)
    return scrubber(message)


def local_key(home: str | None = None) -> bytes:
    """The Town home's labelling key, created on first use.

    Published only once complete, so two processes creating it at once
    agree on one key instead of each labelling with its own.
    """
    directory = home or town_home()
    path = os.path.join(directory, KEY_FILENAME)
    key = _read_key(path)
    if key is not None:
        return key
    os.makedirs(directory, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".url-credentials-", dir=directory)
    try:
        # mkstemp creates the file readable by its owner alone, and fdopen
        # writes every byte or raises.
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(_KEY_BYTES))
        try:
            os.link(staged, path)
        except FileExistsError:
            pass
        except OSError:
            # A file system without hard links: fall back to an exclusive
            # create, which still lets only one process write the key.
            _exclusive_copy(staged, path)
    finally:
        os.unlink(staged)
    key = _read_key(path)
    if key is None:
        raise CredentialKeyError(f"{path} could not be created")
    return key


def _exclusive_copy(source: str, path: str) -> None:
    with open(source, "rb") as f:
        data = f.read()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _read_key(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            key = f.read()
    except FileNotFoundError:
        return None
    if len(key) != _KEY_BYTES:
        raise CredentialKeyError(
            f"{path} is not a {_KEY_BYTES}-byte key; remove it to create a"
            " new one, which changes every label from here on")
    return key


class Labeller:
    """Labels the credentials in one URL at a time.

    The key is loaded only when a URL actually carries credentials, and if
    it cannot be loaded or created the label is WITHHELD: that loses the
    ability to tell credentials apart, never the credentials themselves.
    """

    def __init__(self, key: bytes | Callable[[], bytes] | None = None,
                 withhold_only: bool = False):
        self._key = key
        # Display that must never create a key, such as a report or a
        # message about a URL, withholds instead of labelling.
        self._failed = withhold_only

    def _resolved_key(self) -> bytes | None:
        if self._failed:
            return None
        try:
            if self._key is None:
                self._key = local_key()
            elif callable(self._key):
                self._key = self._key()
        except (OSError, CredentialKeyError):
            self._failed = True
            return None
        return self._key

    def label_for(self, url: str) -> str | None:
        """The label for url's credentials, or None if it carries none."""
        split = _split(url)
        if split is None:
            return None
        userinfo = split[1]
        if _LABEL_USERINFO.match(userinfo):
            return userinfo
        parsed = _parses(url)
        if parsed is not None:
            # What httpx sends: "tok@h" and "tok:@h" are the same credentials.
            credentials = f"{parsed.username}\x00{parsed.password}"
        else:
            name, _colon, password = unquote(userinfo).partition(":")
            credentials = f"{name}\x00{password}"
        key = self._resolved_key()
        if key is None:
            return WITHHELD
        digest = hmac.new(key, credentials.encode("utf-8", "surrogatepass"),
                          hashlib.sha256).hexdigest()[:8]
        return f"<credentials {digest}>"

    def label(self, url: str) -> str:
        """url with its credentials replaced by their label."""
        split = _split(url)
        label = self.label_for(url)
        if split is None or label is None:
            return url
        prefix, _userinfo, rest = split
        return f"{prefix}{label}{rest}"


class Scrubber:
    """Replaces the credentials of registered locators in recorded text.

    Only the exact credentials a registered URL carries are replaced, in
    every spelling they can take: as the operator wrote them, as httpx
    normalises them, and percent-decoded. Each replacement keeps the "@"
    that ends user information, so text that merely shares a word with a
    password is left alone, while a URL quoted in an error message is not.
    """

    def __init__(self, label: Labeller | None = None):
        self.labeller = label or Labeller()
        self._pairs: list[tuple[str, str]] = []

    def register(self, url: object) -> None:
        if not has_credentials(url):
            return
        _prefix, userinfo, _rest = _split(url)
        label = self.labeller.label_for(url)
        spellings = {userinfo, unquote(userinfo)}
        parsed = _parses(url)
        if parsed is not None:
            spellings.add(parsed.userinfo.decode("ascii", "replace"))
        for spelling in spellings:
            if spelling and (f"{spelling}@", f"{label}@") not in self._pairs:
                self._pairs.append((f"{spelling}@", f"{label}@"))
        self._pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    def __call__(self, text: str) -> str:
        for secret, label in self._pairs:
            text = text.replace(secret, label)
        return text

    def __bool__(self) -> bool:
        return bool(self._pairs)


def scrub(value: Any, text: Callable[[str], str]) -> Any:
    """Apply text to every string in a JSON-shaped value, keys included."""
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        return {scrub(k, text): scrub(v, text) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, text) for v in value]
    return value
