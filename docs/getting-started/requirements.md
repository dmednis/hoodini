# Requirements

1. Choose Home Assistant OS/Supervised on **amd64 or aarch64** for the add-on, Linux with Docker host networking, or native macOS.
2. Configure a separate MQTT broker (for example, Mosquitto). Free host **8883** for Hoodini's fixed TLS listener: check existing clients before blanking/remapping Mosquitto's 8883 Network-card mapping. The broker itself can still serve MQTT on 1883.
3. Reserve a stable server IPv4 address in DHCP; the hood remembers this address. Keep the hood and server on a trusted LAN.
4. For one-time pairing, use a Bluetooth laptop with Python 3.12+, [uv](https://docs.astral.sh/uv/) and a 2.4 GHz Wi-Fi network.

Success: the server can bind 8883, the broker is reachable and the laptop can reach the LAN API (default 8765). If not, see [troubleshooting](../reference/troubleshooting.md).

!!! warning
    Before freeing 8883, check existing TLS broker clients. See [known limitations](../reference/known-limitations.md).
