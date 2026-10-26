# Security policy

For a vulnerability, contact the maintainer privately through [GitHub security advisories](https://github.com/dmednis/hoodini/security/advisories/new); do not open a public issue with secrets or exploit details. If private reporting is not available, request a private contact channel without including the vulnerability.

Hoodini's hood TLS is not certificate-authenticated by the hood; first-use credential binding cannot prevent a race on an untrusted LAN. The LAN API requires a token for writes and never serves admin or the token; admin is loopback-only standalone or restricted to Home Assistant ingress in the add-on. Keep the server and broker on a trusted network, and never attach raw protocol logs, Wi-Fi passwords, tokens or setup keys. See the [security and privacy reference](docs/reference/security.md) for the full trust model.
