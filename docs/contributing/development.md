# Development

Python 3.12+; root is a non-distributable uv workspace with editable `server/` package and root dev dependencies. From the repository root:

```sh
uv sync
uv lock --check
uv run pytest server/tests -q
uv run ruff check server
uv run ruff format --check server
uv build --package hoodini
uv run mkdocs build --strict
```

Broker integration tests need Docker: the test fixture starts its own digest-pinned `eclipse-mosquitto` container on `127.0.0.1` and removes it afterwards. Setting `HOODINI_TEST_MQTT_URL` points the tests at another dedicated test broker instead, but tests that restart the broker then skip. Image tests require `docker build -t hoodini:local .` and `HOODINI_TEST_IMAGE=hoodini:local`. Without these prerequisites pytest legitimately skips gated tests; CI provides Docker and the image (no external broker) and treats **any skip as a failure**. Never use an owner's broker or data in tests. The multi-arch CI build creates a local OCI artifact only; runtime on both architectures is not proven by a build.

## Admin dashboard

The dashboard uses locally vendored Pico CSS 2.1.1 and vanilla JavaScript. Asset
provenance and hashes are in `server/src/hoodini/api/static/vendor/README.md`.
Python projection, ingress/host boundary and wheel asset checks run in the normal
suite. Optional browser checks use only synthetic loopback fixtures, with no hood,
Bluetooth, broker or Home Assistant connection:

```sh
uv run --with playwright playwright install chromium
uv run --with playwright python server/tests/browser/admin_smoke.py
```

Alternatively set `HOODINI_BROWSER_EXECUTABLE` to an installed Chrome executable.
Set `HOODINI_UI_ARTIFACTS` to save synthetic screenshots. The browser checks cover
mobile/desktop, light/dark themes, token reveal/copy, refresh failures and form
preservation, and a same-origin reverse-proxied iframe. They do not replace a real
HA ingress check. `server/tests/browser/admin_smoke.py` is a manual browser check, not collected by pytest or CI. See [Dashboard](../using/dashboard.md) for behaviour.

Fixtures contain independently labelled dated-note transcriptions vs constructed/synthetic cases; neither establishes live compatibility. Never commit unredacted exports, credentials, APK contents, or proprietary assets. Publication, real HA OS checks and physical compatibility require a separate human acceptance gate.
