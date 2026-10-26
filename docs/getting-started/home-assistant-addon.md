# Install the Home Assistant add-on

1. Check [requirements](requirements.md), especially host port 8883 and the MQTT broker.
2. [Add the Hoodini repository](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdmednis%2Fhoodini) to the add-on store, or enter `https://github.com/dmednis/hoodini` under **Repositories**.
3. Select **Hoodini**, install and start it. Supervisor pulls `ghcr.io/dmednis/hoodini:0.1.0` for the host architecture. This requires a public repository and image; the GitHub install URL and anonymous image pull can only be verified after publication.
4. Open **Open Web UI** and, if using an external broker, set `mqtt.url` in the Configuration tab and restart.

Success: the dashboard opens and the logs show a bound hood listener. If it fails, check [troubleshooting](../reference/troubleshooting.md). Then [pair your hood](pairing.md).
