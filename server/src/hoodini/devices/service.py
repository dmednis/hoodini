"""Integration facade for the sole device state, commands and TLS gateway."""

import asyncio

from hoodini.devices.commands import CommandCoordinator
from hoodini.devices.state import PROFILE_UNSET, DeviceState
from hoodini.gateway.events import AckReceived, SessionClosed, SessionOpened
from hoodini.gateway.server import GatewayError
from hoodini.storage import AtomicStore, AtomicWriteError


class ForgetError(RuntimeError):
    """Only phase names and known atomic-commit facts cross the public boundary."""

    def __init__(self, phases: tuple[tuple[str, bool | None], ...]):
        self.phases = phases
        super().__init__("cannot forget device")


class DeviceService:
    def __init__(self, store: AtomicStore, *, clock=None, epoch=None):
        self._state = DeviceState(store, clock=clock, epoch=epoch)
        self._commands = CommandCoordinator(self._state, self._send, clock=clock)
        self._gateway = None
        self._running = False
        # This is operation ownership only; state remains the sole session authority.
        self._forgetting: dict[str, object] = {}

    def attach_gateway(self, gateway):
        if self._running or self._forgetting:
            raise RuntimeError("cannot change active gateway")
        self._gateway = gateway

    def get(self, cuid):
        return self._state.get(cuid)

    def list(self):
        return self._state.list()

    def subscribe(self, listener):
        return self._state.subscribe(listener)

    async def start(self):
        if self._gateway is None:
            raise RuntimeError("gateway not attached")
        if self._running or self._forgetting:
            raise RuntimeError("device service already active")
        await self._state.start()
        try:
            await self._commands.start()
        except BaseException:
            await self._state.stop()
            raise
        self._running = True

    async def stop(self):
        if self._forgetting:
            raise RuntimeError("device service operation active")
        if self._running:
            await self._commands.stop()
            await self._state.stop()
            self._running = False

    async def _send(self, cuid, session_id, seq, caps):
        return await self._gateway.send_command(cuid, session_id, seq, caps)

    async def handle_gateway(self, event):
        if isinstance(event, AckReceived):
            self._commands.handle_ack(event)
            return
        if event.cuid in self._forgetting and isinstance(
            event, (SessionOpened, SessionClosed)
        ):
            # Gateway.disconnect looks up and closes the current peer before its
            # first suspension. Never defer this call beyond the owned fence.
            if isinstance(event, SessionOpened):
                await self._gateway.disconnect(event.cuid)
            return
        await self._state.handle_gateway(event)

    async def set_caps(self, cuid, caps):
        return await self._commands.set_caps(cuid, caps)

    async def update(self, cuid: str, info: dict | None = None, profile=PROFILE_UNSET):
        return await self._state.update(cuid, info, profile)

    async def forget(self, cuid):
        if not self._running:
            raise RuntimeError("device service not started")
        token = self._state.begin_forget(cuid)
        if token is None:
            raise ForgetError((("fence", None),))
        self._forgetting[cuid] = token
        failures: list[tuple[str, bool | None]] = []
        cancelled = False
        prepared = False
        finish = False
        try:
            try:
                await self._state.prepare_forget(cuid)
                prepared = True
            except KeyError:
                raise
            except asyncio.CancelledError:
                cancelled = True
            except AtomicWriteError as error:
                failures.append(("intent", error.committed))
            except Exception:  # noqa: BLE001 - discard producer exception graphs
                failures.append(("intent", None))
            if prepared:
                trust_task = asyncio.create_task(self._gateway.forget(cuid))
                # Gateway offloads trust writes to a thread. The owning task must
                # retain its state lease until that offloaded work is terminal.
                while not trust_task.done():
                    try:
                        await asyncio.shield(trust_task)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:  # noqa: BLE001 - inspect terminal result below
                        break
                try:
                    trust_task.result()
                except asyncio.CancelledError:
                    failures.append(("trust", None))
                except GatewayError as error:
                    failures.append(("trust", error.committed))
                    finish = error.committed is True
                except Exception:  # noqa: BLE001 - discard producer graphs
                    failures.append(("trust", None))
                else:
                    finish = True
                if finish:
                    while True:
                        try:
                            await self._state.finish_forget(cuid)
                        except asyncio.CancelledError:
                            cancelled = True
                            continue  # same owning task retains its state lease
                        except AtomicWriteError as error:
                            failures.append(("state", error.committed))
                        except Exception:  # noqa: BLE001 - discard producer graphs
                            failures.append(("state", None))
                        break
        finally:
            try:
                await self._gateway.disconnect(cuid)
            except asyncio.CancelledError:
                cancelled = True
                failures.append(("peer", None))
            except Exception:  # noqa: BLE001 - discard producer graphs
                failures.append(("peer", None))
            self._forgetting.pop(cuid, None)
            self._state.end_forget(cuid, token)
        if cancelled:
            raise asyncio.CancelledError
        if failures:
            raise ForgetError(tuple(failures)) from None
