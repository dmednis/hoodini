"""Optional real-browser dashboard checks using ONLY synthetic, loopback fixtures.

Run: uv run --with playwright python server/tests/browser/admin_smoke.py
Install a Playwright Chromium first, or set HOODINI_BROWSER_EXECUTABLE to Chrome.
HOODINI_UI_ARTIFACTS optionally saves synthetic-data screenshots to that directory.
"""

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from aiohttp import ClientSession, web
from hoodini.api.admin import create_admin_app
from hoodini.devices.models import DeviceView
from hoodini.protocol.profiles import resolve_profile
from playwright.async_api import async_playwright


class Devices:
    def __init__(self):
        profile = resolve_profile({"PRF": "PRF0182867A"}, None)
        self.views = [
            DeviceView(
                "DEMO-HOOD",
                {"PRF": "PRF0182867A"},
                profile,
                None,
                {},
                {96: 65, 110: 2, 64: 1},
                {96, 110, 64},
                -58,
                1792400000,
                65,
                2,
                "ready",
                True,
                "synthetic-session",
            )
        ]
        self.calls = []

    def list(self):
        return self.views[:]

    def get(self, cuid):
        return next((view for view in self.views if view.cuid == cuid), None)

    async def update(self, cuid, *, profile):
        self.calls.append(("profile", cuid, profile))
        self.views = [replace(view, profile_override=profile) for view in self.views]

    async def forget(self, cuid):
        self.calls.append(("forget", cuid))
        self.views = []


class Diagnostics:
    @property
    def history(self):
        return [
            {
                "kind": "mqtt",
                "direction": "in",
                "packet_type": 3,
                "length": 2,
                "hex": "3000",
                "timestamp": 1792400000,
            }
        ]


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def main():
    devices = Devices()
    runner, base = await start(
        create_admin_app(devices, "SYNTHETIC-ONLY-TOKEN", Diagnostics(), addon=False)
    )
    proxy_runner = None
    errors, external, requests = [], [], []
    try:
        async with ClientSession() as session:

            async def proxy(request):
                async with session.request(
                    request.method,
                    base + "/" + request.match_info["tail"],
                    data=await request.read(),
                    headers={
                        key: request.headers[key]
                        for key in ("Host", "Origin", "Content-Type")
                        if key in request.headers
                    },
                    allow_redirects=False,
                ) as response:
                    return web.Response(
                        body=await response.read(),
                        status=response.status,
                        headers={
                            key: value
                            for key, value in response.headers.items()
                            if key.lower()
                            not in {
                                "content-length",
                                "transfer-encoding",
                                "content-encoding",
                                "connection",
                            }
                        },
                    )

            async def parent(request):
                return web.Response(
                    text='<iframe title="Hoodini" src="./ingress/synthetic/" style="width:100%;height:100vh;border:0"></iframe>',
                    content_type="text/html",
                )

            proxy_app = web.Application()
            proxy_app.router.add_get("/", parent)
            proxy_app.router.add_route("*", "/ingress/synthetic/{tail:.*}", proxy)
            proxy_runner, proxy_base = await start(proxy_app)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    executable_path=os.environ.get("HOODINI_BROWSER_EXECUTABLE")
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 1000}
                )
                await context.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=base
                )

                async def local_only(route):
                    if not route.request.url.startswith((base + "/", proxy_base + "/")):
                        external.append(route.request.url)
                        await route.abort()
                    else:
                        requests.append(route.request.url)
                        await route.continue_()

                await context.route("**/*", local_only)
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda msg: (
                        errors.append(msg.text)
                        if "Content Security Policy" in msg.text
                        else None
                    ),
                )
                await page.goto(base)
                await page.locator("#reveal-token").wait_for(state="visible")
                assert not any(url.endswith("/token") for url in requests)
                assert "SYNTHETIC-ONLY-TOKEN" not in await page.content()
                assert await page.locator(".badge").inner_text() == "Online"
                assert await page.locator('select[name="profile"]').input_value() == ""
                assert await page.locator("#api-token").is_hidden()
                await page.locator("#reveal-token").click()
                await page.locator("#token-panel").wait_for(state="visible")
                assert (
                    await page.locator("#api-token").input_value()
                    == "SYNTHETIC-ONLY-TOKEN"
                )
                await page.locator("#copy-token").click()
                await page.wait_for_function(
                    "() => document.getElementById('token-status').textContent.startsWith('Copied')"
                )
                await page.locator("#reveal-token").click()
                assert await page.locator("#api-token").input_value() == ""
                # Clipboard-denied/manual-copy path.
                await page.locator("#reveal-token").click()
                await page.locator("#token-panel").wait_for(state="visible")
                await page.evaluate(
                    "() => { navigator.clipboard.writeText = async () => {throw new Error('denied')}; }"
                )
                await page.locator("#copy-token").click()
                await page.wait_for_function(
                    "() => document.getElementById('token-status').textContent.includes('manually')"
                )
                await page.locator("#reveal-token").click()
                # Open settings pause refresh and preserve unsaved selections.
                await page.get_by_text("Device settings", exact=True).click()
                await page.locator('select[name="profile"]').select_option("GENERIC")
                devices.views[0] = replace(
                    devices.views[0], caps={96: 20, 110: 3, 64: 1}
                )
                await page.locator("#refresh").click()
                assert "paused" in await page.locator("#refresh-status").inner_text()
                assert (
                    await page.locator('select[name="profile"]').input_value()
                    == "GENERIC"
                )
                await page.locator('button:has-text("Save profile")').click()
                await page.wait_for_load_state("networkidle")
                assert devices.calls == [("profile", "DEMO-HOOD", "GENERIC")], (
                    devices.calls,
                    await page.locator("body").inner_text(),
                )
                assert (
                    await page.locator('select[name="profile"]').input_value()
                    == "GENERIC"
                )
                await page.get_by_text("Device settings", exact=True).click()
                await page.get_by_text("Forget this hood", exact=True).click()
                await page.locator('input[type="checkbox"]').check()
                page.once("dialog", lambda dialog: dialog.dismiss())
                await page.get_by_role("button", name="Forget hood", exact=True).click()
                assert len(devices.calls) == 1
                await page.get_by_text("Device settings", exact=True).click()
                await page.locator("#refresh").click()
                try:
                    await page.wait_for_function(
                        "() => document.getElementById('refresh-status').textContent.startsWith('Updated')",
                        timeout=2000,
                    )
                except Exception as error:
                    raise AssertionError(
                        await page.evaluate(
                            "() => ({status: document.getElementById('refresh-status').textContent, active: document.activeElement.outerHTML, details: [...document.querySelectorAll('details')].map(el => [el.open,el.getClientRects().length,el.querySelector('summary').textContent])})"
                        )
                    ) from error
                assert "Speed 3" in await page.locator("#devices").inner_text()
                # Failed refresh is visible; previous reports remain on screen.
                await page.route("**/snapshot", lambda route: route.abort())
                await page.locator("#refresh").click()
                await page.wait_for_function(
                    "() => document.getElementById('refresh-status').textContent.startsWith('Cannot')"
                )
                assert "Speed 3" in await page.locator("#devices").inner_text()
                await page.unroute("**/snapshot")
                await page.locator("#refresh").click()
                await page.wait_for_function(
                    "() => document.getElementById('refresh-status').textContent.startsWith('Updated')"
                )
                artifacts = os.environ.get("HOODINI_UI_ARTIFACTS")
                if artifacts:
                    Path(artifacts).mkdir(parents=True, exist_ok=True)
                for width in (390, 1280):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    for theme in ("light", "dark"):
                        await page.locator("#theme").select_option(theme)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        # Allow CSS color transitions to settle before visual inspection.
                        await page.wait_for_timeout(250)
                        if artifacts:
                            await page.screenshot(
                                path=str(
                                    Path(artifacts) / f"admin-{width}-{theme}.png"
                                ),
                                full_page=True,
                            )
                # Same-origin iframe + stripped ingress prefix, including mutation redirect.
                await page.goto(proxy_base)
                frame = page.frame_locator('iframe[title="Hoodini"]')
                await frame.locator("#reveal-token").wait_for(state="visible")
                await frame.locator("#reveal-token").click()
                await frame.locator("#token-panel").wait_for(state="visible")
                assert (
                    await frame.locator("#api-token").input_value()
                    == "SYNTHETIC-ONLY-TOKEN"
                )
                await frame.locator("#reveal-token").click()
                await frame.get_by_text("Device settings", exact=True).click()
                await frame.locator('select[name="profile"]').select_option("")
                await frame.get_by_role("button", name="Save profile").click()
                await page.wait_for_load_state("networkidle")
                assert devices.calls[-1] == ("profile", "DEMO-HOOD", None)
                # Empty state and status transitions via refresh.
                await page.goto(base)
                devices.views[0] = replace(devices.views[0], online=False)
                await page.locator("#refresh").click()
                await page.wait_for_function(
                    "() => document.querySelector('.badge').textContent === 'Waiting for first status'"
                )
                assert (
                    "Awaiting fresh status"
                    in await page.locator("#devices").inner_text()
                )
                devices.views = []
                await page.locator("#refresh").click()
                await page.get_by_text("No hoods paired yet", exact=True).wait_for()
                # Page-hidden handling clears a revealed token, including pending reads.
                await page.locator("#reveal-token").click()
                await page.locator("#token-panel").wait_for(state="visible")
                await page.evaluate(
                    "() => { Object.defineProperty(document, 'hidden', { configurable: true, value: true }); document.dispatchEvent(new Event('visibilitychange')); }"
                )
                assert await page.locator("#api-token").input_value() == ""
                assert await page.locator("#token-panel").is_hidden()
                await page.evaluate(
                    "() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); }"
                )
                # With JS disabled, the server-rendered dashboard and forms remain useful.
                nojs = await browser.new_context(java_script_enabled=False)
                nojs_page = await nojs.new_page()
                await nojs_page.goto(base)
                assert await nojs_page.get_by_text(
                    "No hoods paired yet", exact=True
                ).is_visible()
                assert await nojs_page.locator("#reveal-token").is_hidden()
                await nojs.close()
                assert not errors, errors
                assert not external, external
                await browser.close()
        print(
            "PASS: desktop/mobile, light/dark, token reveal/hide/copy/fallback, refresh/pause/failure, settings, forget cancellation, ingress-style iframe, status/empty states; no JS/CSP errors or external requests"
        )
    finally:
        if proxy_runner:
            await proxy_runner.cleanup()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
