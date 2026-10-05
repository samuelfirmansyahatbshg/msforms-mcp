"""Own exactly one isolated persistent browser; never attach to a personal browser."""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

from .errors import FormsError
from .ids import ORIGIN


def windows_pac() -> str:
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        ) as key:
            return str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "")
    except OSError:
        return ""


class BrowserSession:
    def __init__(self, profile: Path | None = None, channel: str | None = None):
        self.profile = (
            (
                profile
                or Path(
                    os.environ.get(
                        "FORMS_MCP_PROFILE", str(Path.home() / ".forms-mcp" / "browser-profile")
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        self.channel = channel or os.environ.get(
            "FORMS_MCP_BROWSER", "msedge" if os.name == "nt" else "chromium"
        )
        self.context = None
        self.page = None
        self.runtime = None
        self.lock = asyncio.Lock()
        self.last_write = 0.0
        self.min_interval = float(os.environ.get("FORMS_MCP_MIN_INTERVAL", "0.35"))

    async def start(self):
        if self.context:
            return self
        # Explicitly reject common personal-profile roots, even when overridden.
        forbidden = [Path.home(), Path(self.profile.anchor)]
        if os.name == "nt":
            local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            forbidden += [local / "Microsoft/Edge/User Data", local / "Google/Chrome/User Data"]
        if any(
            self.profile == p.resolve() or p.resolve() in self.profile.parents
            for p in forbidden[2:]
        ) or self.profile in [p.resolve() for p in forbidden[:2]]:
            raise FormsError("unsafe_profile", "Use a dedicated automation profile directory.")
        self.profile.mkdir(parents=True, exist_ok=True)
        self.runtime = await async_playwright().start()
        args = []
        if pac := windows_pac():
            args.append(f"--proxy-pac-url={pac}")
        try:
            self.context = await self.runtime.chromium.launch_persistent_context(
                str(self.profile),
                channel=self.channel,
                headless=False,
                accept_downloads=True,
                args=args,
            )
        except Exception:
            await self.runtime.stop()
            self.runtime = None
            raise FormsError(
                "browser_start_failed",
                "Could not launch the dedicated browser. Check profile ownership and browser installation.",
            ) from None
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(15000)
        return self

    async def ready(self, form_id: str | None = None, timeout: int = 30000):
        await self.start()
        url = ORIGIN + "/Pages/DesignPageV2.aspx"
        if form_id:
            from urllib.parse import urlencode

            url += "?" + urlencode({"subpage": "design", "id": form_id})
        if self.page.url != url:
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except Exception:
                raise FormsError(
                    "navigation_failed",
                    "Forms did not load; check the browser sign-in and network/proxy settings.",
                ) from None
        try:
            await self.page.wait_for_function(
                "!!window.OfficeFormServerInfo?.antiForgeryToken && !!window.OfficeFormServerInfo?.userInfo?.UserId",
                timeout=timeout,
            )
        except Exception:
            raise FormsError(
                "login_required",
                "Sign into Forms in the dedicated browser using `msforms-mcp login`.",
            ) from None
        return self.page

    async def identity(self) -> dict:
        return await self.page.evaluate("""() => {
          const u = window.OfficeFormServerInfo?.userInfo || {};
          return {tenant: u.TenantId, owner: u.UserId};
        }""")

    async def pace(self):
        """Minimum spacing between every Forms request, reads included.

        120 ms is the measured-safe figure for writes, but a burst of ~50 full-form reads
        (an account-wide fleet plan) earns a sustained 429, so the default is gentler and
        tunable. Lower it only for small, known-size runs.
        """
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, self.min_interval - (loop.time() - self.last_write)))
        self.last_write = loop.time()

    async def close(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.runtime:
            await self.runtime.stop()
            self.runtime = None
