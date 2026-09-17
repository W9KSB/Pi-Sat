# HTTPS and browser microphones

Pi-Sat's normal launcher (`python -m pi_sat_controller.backend.run_server`)
serves HTTPS on port 443 by default. Open `https://<PI-IP>/` on the local
network. Pi-Sat does not run a parallel HTTP listener or redirect HTTP requests.
Set `https_port` explicitly when a different HTTPS port is required. The
provided systemd service grants the capability needed to use port 443.

## Configuration

These are config-file-only settings under `[server]` in `pi-sat-controller.conf`;
restart the service after changes. Normal Settings saves preserve them.

```ini
[server]
host = 0.0.0.0
port = 80
https_enabled = true
https_port = 443
tls_certfile = data/tls/server.crt
tls_keyfile = data/tls/server.key
tls_names =
```

- With HTTPS enabled, only `https_port` is used. A nonstandard port requires
  `https://<PI-IP>:<PORT>/` in the browser. An existing firewall must allow it.
- Both certificate and key present: validate the PEM/key pairing and reuse it.
  Supplied certificates are never replaced automatically.
- Certificate absent: use the installed `openssl` executable to generate a
  365-day self-signed RSA-2048/SHA-256 certificate. Generate a private key only
  if none exists; an existing unencrypted key is reused.
- Certificate present but key absent, mismatched/invalid PEM, unavailable
  OpenSSL, or failed generation: stop startup with an error. Never silently
  switch to HTTP. Inspect `journalctl -u pi-sat -n 50 --no-pager`.
- If OpenSSL is missing, install the OS `openssl` package or provide a PEM pair.
  There is no added Python dependency or automatic package installation.
- Relative paths resolve against the project root, not the launch directory.
  Default generated files are git-ignored and not in the frontend's served tree.
  Generated key permissions are owner-only (0600) on Linux. Keep custom keys
  outside the frontend directory and out of version control too.
- Local hostname, resolved host addresses, loopback and (on Pi/Linux) interface
  addresses are included as certificate Subject Alternative Names. For another
  DNS alias or IP, put comma-separated names/addresses in `tls_names` **before
  first generation**. No connection to a radio or external host is used to
  discover interface addresses.
- Changing `tls_names` or the Pi's address does not replace an existing cert.
  For expiry or name changes, supply a renewed pair or select new certificate
  and key filenames, then restart to generate a new pair. Browser trust will
  need updating. There is no automatic certificate renewal.

To use an existing TLS-terminating reverse proxy, explicitly set
`https_enabled = false`. The `host` and `port` settings then control the local
HTTP listener. The browser-facing proxy must serve trusted HTTPS and support
WebSockets. Pi-Sat does not configure a proxy or firewall.

## Browser acceptance and microphone permission

A self-signed certificate is not automatically trusted. Verify it belongs to
your Pi before accepting the browser warning or trusting the public certificate
on your laptop. Never transfer the private key to the browser or share it.
Browser/organization policies differ: dismissing a warning is not a guarantee
that microphone access will be allowed. If access is still blocked, establish
certificate trust in that browser/OS and allow the microphone for the HTTPS
site. Do not disable browser security or TLS verification globally.

The [browser microphone API requires a secure context and user permission](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia).
Audio already uses a secure connection when you open Pi-Sat over HTTPS. Moving
from HTTP to HTTPS is a new browser origin, so preferences stored in the browser
and the microphone permission may need setting again. Server-side radio settings
are unchanged. HTTPS does not add application authentication; keep this
radio-control service on your trusted LAN, not exposed to the Internet.
