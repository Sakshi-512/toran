"""Make the home page's phone screenshot from a real sample site: python -m scripts.home_shots.

Builds the asha-home-bakery sample with the normal site builder (so it carries the Sample
banner), opens it in Chromium at phone size and saves a light WebP for the home page hero.
"""

import io
import sys
import tempfile
from datetime import date
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

from app.services.samples import get_sample
from app.services.sitegen import SiteInput, build_site, write_site

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "home" / "hero-sample.webp"
SLUG = "asha-home-bakery"
WIDTH, HEIGHT, SCALE = 360, 720, 1.5


def main() -> int:
    """Build, screenshot and save the hero image."""
    sample = get_sample(SLUG)
    if sample is None:
        print(f"No sample {SLUG}")
        return 1
    site = SiteInput(
        slug=sample.slug,
        display_name=sample.display_name,
        profile=sample.profile,
        theme=sample.theme,
        is_sample=True,
        prices_updated=date.today(),
        site_url=f"https://{SLUG}.example.test",
        main_url="https://example.test",
        enquiry_url="https://example.test/api/enquiry/" + SLUG,
        app_name="Toran",
        photos=sample.photos,
    )
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as p:
        root = Path(folder) / "site"
        write_site(build_site(site), root)
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=SCALE
        )
        page.goto((root / "index.html").as_uri())
        page.wait_for_load_state("networkidle")
        png = page.screenshot()
        browser.close()
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="WEBP", quality=72, method=6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(out.getvalue())
    print(f"Saved {OUT.relative_to(Path.cwd())} ({len(out.getvalue()) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
