from .production import *  # noqa: F401, F403

# NALARIN self-host topology note
# -----------------------------------------------------------------------
# Public HTTPS is terminated at Cloudflare's edge, forwarded through a local
# cloudflared tunnel to Nalarin's shared Caddy instance on plain HTTP
# (:80), which then reverse-proxies to this app on 127.0.0.1:3023 — also
# plain HTTP. This is the exact same internal-plain-HTTP pattern every
# other Nalarin app uses behind that same Caddy instance.
#
# Caddy's `reverse_proxy` always sets `X-Forwarded-Proto` to match the
# scheme of the connection IT received (http, since our Caddyfile block is
# `http://medsos.nalar.army`), overwriting whatever cloudflared/Cloudflare
# already set upstream. That means `SECURE_PROXY_SSL_HEADER` never actually
# reads "https" here, so upstream's `SECURE_SSL_REDIRECT = True` would
# redirect every request to https://... forever (an infinite loop), even
# though the public-facing connection really is HTTPS.
#
# HTTPS is already fully enforced at the real edge (Cloudflare), so the
# app-level redirect is redundant for this deployment and must be disabled.
# Cookies stay Secure/HttpOnly (inherited from production.py) since browsers
# only ever reach this app over the public https:// origin.
SECURE_SSL_REDIRECT = False
