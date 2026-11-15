"""Customer tests: a real Chromium visits a business site the way a customer would.

Each run checks a phone (360 x 740), a laptop (1280 x 800) and a slow network, all in fresh
browser profiles so nothing is cached. At most MAX_BROWSERS Chromium instances run at once.
"""

import asyncio
import re
import statistics
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, Page, ViewportSize, async_playwright

from app.services.address_guard import looks_like_address
from app.services.score import Measurements

MAX_BROWSERS = 2
RUNS = 3
PHONE = ViewportSize(width=360, height=740)
LAPTOP = ViewportSize(width=1280, height=800)
SLOW_NETWORK = {
    "offline": False,
    "latency": 300,
    "downloadThroughput": 1_500_000 / 8,
    "uploadThroughput": 750_000 / 8,
}
NAV_TIMEOUT_MS = 60_000
POLICY_WORDS = re.compile(
    r"refund|cancellation|policy|रिफ़ंड|रिफंड|रद्द|वापसी|नीति|परतावा|धोरण",
    re.IGNORECASE,
)

_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_active = {"now": 0, "peak": 0}


@asynccontextmanager
async def chromium_slot() -> AsyncIterator[None]:
    """Hold one of the MAX_BROWSERS Chromium slots for this event loop."""
    loop = asyncio.get_running_loop()
    semaphore = _slots.setdefault(loop, asyncio.Semaphore(MAX_BROWSERS))
    async with semaphore:
        _active["now"] += 1
        _active["peak"] = max(_active["peak"], _active["now"])
        try:
            yield
        finally:
            _active["now"] -= 1


def peak_browsers() -> int:
    """Most Chromium instances seen open at once (for tests and monitoring)."""
    return _active["peak"]


def reset_peak() -> None:
    """Reset the peak counter."""
    _active["peak"] = _active["now"]


@dataclass(frozen=True)
class Expectations:
    """What the site should show, taken from the confirmed profile."""

    phone_digits: str
    areas: tuple[str, ...]


@dataclass
class RunResult:
    """Raw results of one run (phone, laptop and slow network)."""

    content_ms: float
    bytes_transferred: int
    cta_found: bool
    cta_above_fold: bool
    small_targets: int
    small_examples: list[str]
    sideways_scroll: bool
    laptop_overflow: int
    phone_visible: bool
    area_shown: bool
    address_found: bool
    https: bool
    policy_text_found: bool
    screenshot: bytes = field(repr=False, default=b"")


_PHONE_JS = """() => {
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const skip = e => e.closest('.trap') || e.classList.contains('skip');
  const cta = document.querySelector('.cta .btn-main, a[href^="https://wa.me"]');
  const small = [...document.querySelectorAll('a, button, input, textarea, select')]
    .filter(e => visible(e) && !skip(e))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width < 44 || r.height < 44; })
    .map(e => e.outerHTML.slice(0, 80));
  const areas = [...document.querySelectorAll('.area-list li, .contact-list dd, .footer-area')]
    .map(e => e.innerText.trim());
  return {
    ctaFound: !!cta,
    ctaAboveFold: !!cta && cta.getBoundingClientRect().bottom <= window.innerHeight,
    small,
    sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    text: document.body.innerText,
    areaTexts: areas,
  };
}"""

_LAPTOP_JS = """() => {
  const vw = document.documentElement.clientWidth;
  const spill = [...document.querySelectorAll('body *')].filter(e => {
    if (e.closest('.trap') || e.classList.contains('skip')) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.right > vw + 1;
  });
  return spill.length + (document.documentElement.scrollWidth > vw ? 1 : 0);
}"""

# Records when the main content appeared: Largest Contentful Paint, or the first visible H1.
_TIMING_INIT = """
window.__lcp = 0; window.__h1 = 0;
try {
  new PerformanceObserver(list => {
    for (const e of list.getEntries()) window.__lcp = Math.max(window.__lcp, e.startTime);
  }).observe({type: 'largest-contentful-paint', buffered: true});
} catch (e) {}
(function watch() {
  const h = document.querySelector('h1');
  if (h && h.getBoundingClientRect().height > 0) { window.__h1 = performance.now(); return; }
  requestAnimationFrame(watch);
})();
"""


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


async def _phone_check(browser: Browser, url: str, expect: Expectations) -> dict[str, Any]:
    context = await browser.new_context(
        viewport=PHONE, is_mobile=True, has_touch=True, device_scale_factor=2
    )
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.evaluate("document.fonts.ready")
        result: dict[str, Any] = await page.evaluate(_PHONE_JS)
        result["screenshot"] = await page.screenshot(type="png")
        result["https"] = page.url.startswith("https://")
        text = result.pop("text")
        local = expect.phone_digits[-10:]
        result["phoneVisible"] = bool(local) and local in _digits(text)
        result["areaShown"] = any(area and area in text for area in expect.areas)
        result["addressFound"] = any(looks_like_address(a) for a in result.pop("areaTexts"))
        result["policyFound"] = bool(POLICY_WORDS.search(text))
        return result
    finally:
        await context.close()


async def _laptop_check(browser: Browser, url: str) -> int:
    context = await browser.new_context(viewport=LAPTOP)
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.evaluate("document.fonts.ready")
        return int(await page.evaluate(_LAPTOP_JS))
    finally:
        await context.close()


async def _slow_check(browser: Browser, url: str) -> tuple[float, int]:
    """Time to main content and bytes transferred on a throttled network, in a fresh profile."""
    context = await browser.new_context(viewport=PHONE, is_mobile=True)
    try:
        page: Page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        await cdp.send("Network.emulateNetworkConditions", SLOW_NETWORK)
        transferred = {"bytes": 0}

        def on_finished(event: dict[str, Any]) -> None:
            transferred["bytes"] += int(event.get("encodedDataLength", 0))

        cdp.on("Network.loadingFinished", on_finished)
        await page.add_init_script(_TIMING_INIT)
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        timing = await page.evaluate("({lcp: window.__lcp, h1: window.__h1})")
        content_ms = float(timing["lcp"] or timing["h1"])
        return content_ms, transferred["bytes"]
    finally:
        await context.close()


async def run_once(browser: Browser, url: str, expect: Expectations) -> RunResult:
    """One full run: phone, laptop, slow network."""
    phone = await _phone_check(browser, url, expect)
    overflow = await _laptop_check(browser, url)
    content_ms, transferred = await _slow_check(browser, url)
    return RunResult(
        content_ms=content_ms,
        bytes_transferred=transferred,
        cta_found=phone["ctaFound"],
        cta_above_fold=phone["ctaAboveFold"],
        small_targets=len(phone["small"]),
        small_examples=phone["small"][:5],
        sideways_scroll=phone["sideways"],
        laptop_overflow=overflow,
        phone_visible=phone["phoneVisible"],
        area_shown=phone["areaShown"],
        address_found=phone["addressFound"],
        https=phone["https"],
        policy_text_found=phone["policyFound"],
        screenshot=phone["screenshot"],
    )


async def run_customer_tests(url: str, expect: Expectations, runs: int = RUNS) -> list[RunResult]:
    """Run the customer tests `runs` times in one Chromium (fresh profile for every check)."""
    async with chromium_slot(), async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            return [await run_once(browser, url, expect) for _ in range(runs)]
        finally:
            await browser.close()


def _majority(values: list[bool]) -> bool:
    return sum(values) * 2 > len(values)


def median_measurements(runs: list[RunResult]) -> Measurements:
    """The middle result of each measured value across runs (majority for yes/no checks)."""
    return Measurements(
        content_ms=statistics.median(r.content_ms for r in runs),
        bytes_transferred=int(statistics.median(r.bytes_transferred for r in runs)),
        cta_found=_majority([r.cta_found for r in runs]),
        cta_above_fold=_majority([r.cta_above_fold for r in runs]),
        small_targets=int(statistics.median(r.small_targets for r in runs)),
        sideways_scroll=_majority([r.sideways_scroll for r in runs]),
        laptop_overflow=int(statistics.median(r.laptop_overflow for r in runs)),
        phone_visible=_majority([r.phone_visible for r in runs]),
        area_shown=_majority([r.area_shown for r in runs]),
        address_found=_majority([r.address_found for r in runs]),
        https=_majority([r.https for r in runs]),
        policy_text_found=_majority([r.policy_text_found for r in runs]),
    )
