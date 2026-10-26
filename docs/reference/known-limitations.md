# Known limitations

- **Tested:** The Hidden Advance (PRF0182867A) has been tested on one hood; this is not a guarantee for other units, firmware or Home Assistant deployments.
- **Unverified:** Other Elica Connect hoods use a generic capability profile until independently checked. Linux/Windows Bluetooth pairing is untested. The add-on's ingress page has been checked on one real HA OS install.
- **Tested:** The hood uses fixed TLS **8883**. Mosquitto may reserve this host port in its Network card even without TLS; manually check existing clients before changing the mapping. Hoodini cannot change broker mappings.
- **Tested:** A newly paired/forgotten hood may take **3.5–5 minutes after command readiness** for first status. After restart, stored state is unavailable until fresh status confirms it. There is no known safe status-request command.
- **Inferred:** Laptop reachability of the server does not prove the hood's network route is working. Pairing requires a reachable reserved server IPv4 and 2.4 GHz Wi-Fi.
- **Unverified:** BLE reply CRC and outbound fragmentation. The CLI rejects provisioning payloads above 245 bytes.
- **Unverified:** Cloud isolation; use trusted networks. A self-signed TLS certificate is not validated by the hood; first enrolment can be raced on an untrusted LAN.
- **Known:** The admin page does not time-limit request bodies, so a slow partial upload can hold a request handler open. Only Home Assistant's ingress proxy (add-on) or, by default, localhost (standalone) can reach the admin page; setting a non-loopback `admin.bind` exposes it to other hosts.

For symptoms and remedies, use [troubleshooting](troubleshooting.md).
