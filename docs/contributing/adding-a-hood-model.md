# Add a hood model

1. Check [supported hoods](../reference/supported-hoods.md). Open a [compatibility report](https://github.com/dmednis/hoodini/issues/new?template=compatibility.yml) with model family and manually redacted behaviour; never post raw capability (`cap_<id>`) values, device identifiers, logs or credentials.
2. Identify the hood's reported capability IDs and map only independently understood values into an original profile. Keep unknown values read-only. Follow the [fixture provenance](https://github.com/dmednis/hoodini/blob/main/protocol-fixtures/README.md) rules.
3. Add synthetic tests for profile resolution and commands; run the checks in [development](development.md). Ask for real hardware confirmation separately.

Success: the profile tests pass and the compatibility report distinguishes actual observation from assumptions. If a meaning is unclear, leave it out of controls and ask for more redacted observations.
