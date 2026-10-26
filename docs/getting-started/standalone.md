# Run standalone

## Linux Compose

1. Check [requirements](requirements.md). Clone the repository, create `./data` with `mkdir -p data`, and copy `config.example.toml` to a private `config.toml`.
2. Set `[mqtt] url = "mqtt://broker.example.com:1883"` as needed, and change the explicit `data_dir` to `/data` (the path inside the container). Never commit private configuration.
3. Hoodini reads `config.toml` only when started with `--config`, so mount it and pass it in `compose.yaml` under the `hoodini` service:

    ```yaml
        volumes:
          - type: bind
            source: ./data
            target: /data
            bind:
              create_host_path: false
          - ./config.toml:/config/config.toml:ro
        command: ["serve", "--config", "/config/config.toml"]
    ```

    Without this, Hoodini starts with defaults: the hood gateway and LAN API run, but no broker is used and Home Assistant sees nothing.
4. Run `docker compose up -d`; the image comes from `ghcr.io/dmednis/hoodini`. Compose uses host networking and maps `./data` to `/data`. The entrypoint sets data ownership to UID/GID 999 and drops privileges.

Success: the listener accepts TLS on host 8883 and admin opens on loopback 8099. If it fails, check `docker compose logs` and [troubleshooting](../reference/troubleshooting.md).

## Native macOS

1. Clone the repository, install [uv](https://docs.astral.sh/uv/), then run `uv sync` in the checkout.
2. Configure a private `config.toml` if using a broker; see [configuration](../reference/configuration.md).
3. Run `uv run hoodini serve --config config.toml`. Without `--config`, Hoodini runs with defaults and no broker. Native data defaults to `./data`; standalone admin binds to `127.0.0.1:8099`. Use an SSH tunnel for remote administration.

Success: the listener starts and the admin page loads locally. If not, see [troubleshooting](../reference/troubleshooting.md). Docker Desktop host networking differs from Linux; use native mode on macOS.
