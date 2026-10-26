"""Synthetic MQTT hood wire peer, with literal independent wire frames."""

import asyncio
import ssl


class Hood:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cuid: str
    ):
        self.reader, self.writer, self.cuid = reader, writer, cuid

    @classmethod
    async def open(cls, port: int, cuid: str = "hood1") -> "Hood":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context)
        return cls(reader, writer, cuid)

    async def send(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def packet(self) -> bytes:
        head = await asyncio.wait_for(self.reader.readexactly(1), 3)
        header = bytearray(head)
        length, multiplier = 0, 1
        for _ in range(4):
            byte = (await asyncio.wait_for(self.reader.readexactly(1), 3))[0]
            header.append(byte)
            length += (byte & 127) * multiplier
            if not byte & 128:
                return bytes(header) + await asyncio.wait_for(
                    self.reader.readexactly(length), 3
                )
            multiplier *= 128
        raise AssertionError("invalid server frame")

    async def connect(
        self,
        password: bytes = b"testpass",
        keepalive: int = 30,
        username: str | None = None,
    ) -> bytes:
        cuid = self.cuid.encode()
        user = username.encode() if username is not None else cuid
        body = (
            b"\x00\x04MQTT\x04\xc2"
            + keepalive.to_bytes(2, "big")
            + len(cuid).to_bytes(2, "big")
            + cuid
            + len(user).to_bytes(2, "big")
            + user
            + len(password).to_bytes(2, "big")
            + password
        )
        assert len(body) < 128
        await self.send(b"\x10" + bytes((len(body),)) + body)
        return await self.packet()

    async def subscribe(
        self, relative: str = "op/mode", packet_id: int = 1, qos: int = 0
    ) -> bytes:
        topic = f"v1/device/{self.cuid}/{relative}".encode()
        body = (
            packet_id.to_bytes(2, "big")
            + len(topic).to_bytes(2, "big")
            + topic
            + bytes((qos,))
        )
        await self.send(b"\x82" + bytes((len(body),)) + body)
        return await self.packet()

    async def publish(
        self,
        relative: str,
        payload: bytes,
        *,
        packet_id: int | None = None,
        dup: bool = False,
        retain: bool = False,
    ) -> None:
        topic = f"v1/device/{self.cuid}/{relative}".encode()
        body = len(topic).to_bytes(2, "big") + topic
        if packet_id is not None:
            body += packet_id.to_bytes(2, "big")
        body += payload
        assert len(body) < 128
        await self.send(
            bytes(
                (
                    0x30
                    | (2 if packet_id else 0)
                    | (8 if dup else 0)
                    | (1 if retain else 0),
                    len(body),
                )
            )
            + body
        )

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ssl.SSLError as error:
            if error.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                raise
