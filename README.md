# Hoodini

[![CI](https://github.com/dmednis/hoodini/actions/workflows/ci.yml/badge.svg)](https://github.com/dmednis/hoodini/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-read-blue)](https://dmednis.github.io/hoodini/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Local control for Elica Connect cooker hoods.** Hoodini connects your hood to Home Assistant on your own network via MQTT.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdmednis%2Fhoodini)

## Why

Hoodini answers the hood locally instead of depending on the Elica app or cloud service. The hood connects to your server, and Home Assistant uses a separate MQTT broker for control and state. This is not a guarantee of cloud isolation; see [security](docs/reference/security.md).

## Features

- Fan (three speeds plus boost on Hidden Advance), dimmable light, filter and Wi-Fi diagnostics discovered in Home Assistant.
- Bluetooth setup through the hood's normal Wi-Fi setup; no firmware modifications or DNS changes.
- State comes from hood reports, not guessed updates.
- Ingress dashboard for status, profile settings and diagnostics.
- Reversible with a manual Wi-Fi reset and the Elica Connect app.

## How it works

```text
hood ── TLS 8883 ──▶ Hoodini ── MQTT ──▶ broker ◀── Home Assistant
  ▲
  └── one-time Bluetooth setup from a laptop (hoodini pair)
```

## Compatibility

| Hood | Status |
|---|---|
| Elica Hidden Advance (PRF0182867A) | Tested |
| Other Elica Connect hoods | Generic profile; [report your model](https://github.com/dmednis/hoodini/issues/new?template=compatibility.yml) |

See [supported hoods](docs/reference/supported-hoods.md) and [known limitations](docs/reference/known-limitations.md).

## Requirements

- Home Assistant OS/Supervised on **amd64 or aarch64** for the add-on; alternatively Linux with Docker host networking or native macOS.
- A separate MQTT broker and free host port **8883** for the hood. Check Mosquitto's Network-card mapping before changing it.
- A reserved server IPv4 address and a Bluetooth laptop with Python 3.12+ and [uv](https://docs.astral.sh/uv/) for one-time pairing.

## Getting started

1. [Install the add-on](docs/getting-started/home-assistant-addon.md) from the store (or the button above); Supervisor pulls the versioned image from `ghcr.io/dmednis/hoodini`. Or [run standalone](docs/getting-started/standalone.md).
2. Install the release-tagged pairing CLI from Git (not PyPI):

   ```sh
   uv tool install --python 3.12 "hoodini[pairing] @ git+https://github.com/dmednis/hoodini@v0.1.0#subdirectory=server"
   ```

3. [Pair your hood](docs/getting-started/pairing.md). Wait about 3.5–5 minutes after readiness for the first status; the device then appears under **Settings → Devices & services → MQTT**.

Pairing uses the Elica Connect setup key, a single value shared by every hood, which is bundled with Hoodini. It only works while the hood is in setup mode. `hoodini extract-key` can read it from your own copy of the Elica app instead; see [security](docs/reference/security.md).

If it fails, see [troubleshooting](docs/reference/troubleshooting.md).

## Contributing

Compatibility reports for other hood models help most. See [CONTRIBUTING.md](CONTRIBUTING.md). Never submit raw logs, credentials or device identifiers.

## Acknowledgements

- [local_roborock_server](https://github.com/Python-roborock/local_roborock_server) for the add-on-plus-standalone layout.
- [Bumper](https://github.com/bmartin5692/bumper), [Valetudo](https://valetudo.cloud/) and [NoLongerEvil](https://github.com/codykociemba/NoLongerEvil-SelfHosted) for cloud-free appliance work.

## Disclaimer

Unofficial; not affiliated with or endorsed by Elica S.p.A.

## License

MIT. See [LICENSE](LICENSE).
