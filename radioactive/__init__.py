"""Discord bot for managing a Minecraft Azure VM."""

import os
import ssl

# ── CA certificate bootstrap ──────────────────────────────────────────
# Python 3.14+ on some distros (NixOS, static builds) has cafile=None
# and doesn't find system CA certs automatically.  aiohttp (used by
# discord.py) creates _SSL_CONTEXT_VERIFIED at module-import time, so
# SSL_CERT_FILE must be set BEFORE any import that triggers aiohttp.
# This runs when the package is first imported, before any submodule
# (like bot.py → discord → aiohttp) is loaded.

_CA_BUNDLE_CANDIDATES = [
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
]

if not os.environ.get("SSL_CERT_FILE"):
    _paths = ssl.get_default_verify_paths()
    if not (_paths.cafile and os.path.exists(_paths.cafile)):
        for _p in _CA_BUNDLE_CANDIDATES:
            if os.path.exists(_p):
                os.environ["SSL_CERT_FILE"] = _p
                break
