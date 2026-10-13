"""Connections to Anthropic that survive a flaky network.

Two things go wrong on real Wi-Fi: DNS lookups fail for a few minutes while
connections would still go through, and a broken IPv6 path makes a connect attempt
hang. This module remembers the last addresses that resolved (on disk, so they
survive restarts) and tries each address with a short connect timeout.
"""

import json
import os
import socket
import threading

from . import config
from .usage import log

_lock = threading.Lock()
_cache = {}   # host -> [address, ...] that last resolved


def load():
    global _cache
    try:
        with open(config.DNS_PATH) as f:
            _cache = {str(h): [str(a) for a in v] for h, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        _cache = {}


def resolve(host, port):
    """Addresses for host, falling back to the last ones that resolved.

    TLS still verifies the host name, so a stale address can only fail, never be
    trusted.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        with _lock:
            cached = list(_cache.get(host) or [])
        if not cached:
            raise
        log("dns_fallback", host=host, addrs=cached)
        return cached
    addrs = []
    for *_, sockaddr in infos:
        if sockaddr[0] not in addrs:
            addrs.append(sockaddr[0])
    snap = None
    with _lock:
        changed = _cache.get(host) != addrs
        if changed:
            _cache[host] = addrs
            snap = dict(_cache)
    if changed:
        try:
            tmp = config.DNS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, config.DNS_PATH)
        except OSError:
            pass
    return addrs


def create_connection(address, timeout=None, source_address=None):
    """Drop-in for socket.create_connection (http.client's hook): resolves through
    resolve(), connects with CONNECT_TIMEOUT_S per address, then applies the long
    read timeout."""
    host, port = address[0], address[1]
    last = None
    for ip in resolve(host, port):
        try:
            sock = socket.create_connection((ip, port), config.CONNECT_TIMEOUT_S, source_address)
            sock.settimeout(timeout)
            return sock
        except OSError as e:
            last = e
    raise last or OSError(f"no usable address for {host}")
