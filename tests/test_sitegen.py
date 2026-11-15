import io
from urllib.parse import parse_qs, quote, urlparse

import pytest
from PIL import Image

from app.services.page_weight import PAGE_WEIGHT_LIMIT, page_weight
from app.services.samples import load_samples
from app.services.site_images import SitePhoto
from app.services.sitegen import format_inr, format_phone, monogram, whatsapp_link
from app.services.themes import THEMES
from tests.site_helpers import (
    ALL_PAGES,
    PAGES,
    Page,
    build,
    build_sample,
    numbers_in,
    page,
    profile,
)

SAMPLE_SLUGS = [s.slug for s in load_samples()]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "₹0"),
        (40, "₹40"),
        (999, "₹999"),
        (1250, "₹1,250"),
        (125000, "₹1,25,000"),
        (10000000, "₹1,00,00,000"),
    ],
)
def test_indian_price_format(amount: int, expected: str) -> None:
    assert format_inr(amount) == expected


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
@pytest.mark.parametrize("path", PAGES)
def test_every_price_matches_profile(slug: str, path: str) -> None:
    site, prof = build_sample(slug)
    expected = [format_inr(o.price) for p in prof.products for o in p.options]
    assert page(site, path).by_class.get("price", []) == expected


@pytest.mark.parametrize("path", PAGES)
def test_prices_never_translated(path: str) -> None:
    prices = page(build(), path).by_class["price"]
    assert prices == ["₹1,250", "₹1,25,000", "₹300"]


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
def test_every_number_on_page_comes_from_profile(slug: str) -> None:
    site, prof = build_sample(slug)
    allowed = numbers_in(prof.model_dump_json()) | numbers_in("14/11/2026")
    allowed |= {str(o.price) for p in prof.products for o in p.options}
    allowed |= numbers_in(format_phone(prof.phone))
    for path in ALL_PAGES:
        assert numbers_in(page(site, path).text) <= allowed, path


def test_address_is_never_rendered_and_is_returned_as_warning() -> None:
    prof = profile(
        area="Flat 12, Shanti Society, Kothrud 411038",
        delivery_areas=["B-204 Sunrise Apartments", "Baner"],
    )
    site = build(prof)
    for path in ALL_PAGES:
        html = site.files[path].decode()
        assert "Shanti" not in html and "411038" not in html and "Sunrise" not in html
    assert "Baner" in page(site).text
    fields = {(w.field, w.value) for w in site.warnings}
    assert ("area", "Flat 12, Shanti Society, Kothrud 411038") in fields
    assert ("delivery_areas", "B-204 Sunrise Apartments") in fields
    assert all(w.code.startswith("looks_like_address") for w in site.warnings)


def test_clean_profile_has_no_warnings() -> None:
    assert build().warnings == []


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
@pytest.mark.parametrize("theme", list(THEMES))
def test_page_weight_under_limit(slug: str, theme: str) -> None:
    site, _ = build_sample(slug, theme)
    for path in ALL_PAGES:
        weight = page_weight(site.files, path)
        assert weight.total < PAGE_WEIGHT_LIMIT, (path, weight.parts)


def test_page_weight_counts_fonts_for_each_language() -> None:
    site = build()
    english = page_weight(site.files, "index.html")
    hindi = page_weight(site.files, "hi/index.html")
    assert any("mukta-400-latin" in p for p in english.parts)
    assert any("devanagari" in p for p in hindi.parts)
    assert any(p.endswith(".css") for p in hindi.parts)
    assert hindi.total > 0 and english.total > 0


def _jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.effect_noise((width, height), 80).convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


def test_uploaded_photos_become_resized_webp_and_stay_under_limit() -> None:
    photos = (
        SitePhoto(data=_jpeg(3000, 2000)),
        SitePhoto(subject="Chocolate cake", data=_jpeg(2000, 2000)),
    )
    site = build(photos=photos)
    images = [p for p in site.files if p.startswith("assets/img/")]
    assert images and all(p.endswith(".webp") for p in images)
    for path in ALL_PAGES:
        assert page_weight(site.files, path).total < PAGE_WEIGHT_LIMIT
    imgs = page(site).find("img")
    assert "loading" not in imgs[0]  # hero is above the fold
    assert imgs[1]["loading"] == "lazy"
    assert "480w" in imgs[0]["srcset"] and "960w" in imgs[0]["srcset"]


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
def test_every_image_has_alt_text(slug: str) -> None:
    site, _ = build_sample(slug)
    for path in ALL_PAGES:
        for img in page(site, path).find("img"):
            assert img.get("alt", "").strip(), (path, img)


def test_alt_text_in_page_language() -> None:
    photos = (
        SitePhoto(data=_jpeg(800, 600)),
        SitePhoto(subject="Chocolate cake", data=_jpeg(800, 600)),
    )
    site = build(photos=photos)
    alts = [i["alt"] for i in page(site).find("img")]
    assert alts == ["Photo of Priya Cakes", "Photo of Chocolate cake"]
    assert [i["alt"] for i in page(site, "hi/index.html").find("img")][
        1
    ] == "Chocolate cake की फ़ोटो"
    assert [i["alt"] for i in page(site, "mr/index.html").find("img")][
        1
    ] == "Chocolate cake चा फोटो"


def test_sample_photo_alt_says_sample() -> None:
    site, _ = build_sample("asha-home-bakery")
    assert page(site).find("img")[0]["alt"] == "Sample photo of Asha's Home Bakery"


@pytest.mark.parametrize(
    ("path", "lang"),
    [
        ("index.html", "en"),
        ("hi/index.html", "hi"),
        ("mr/index.html", "mr"),
        ("hi/thanks/index.html", "hi"),
    ],
)
def test_language_pages_and_hreflang(path: str, lang: str) -> None:
    p = page(build(), path)
    assert p.find("html")[0]["lang"] == lang
    alternates = {a["hreflang"]: a["href"] for a in p.find("link") if a.get("rel") == "alternate"}
    assert alternates == {
        "en": "https://shop.example.test/",
        "hi": "https://shop.example.test/hi/",
        "mr": "https://shop.example.test/mr/",
        "x-default": "https://shop.example.test/",
    }


def test_language_switch_works_without_javascript() -> None:
    site = build()
    for path in ALL_PAGES:
        assert b"<script" not in site.files[path]
    links = {
        a["lang"]: a["href"] for a in page(site, "hi/index.html").find("a") if a.get("hreflang")
    }
    assert links == {"en": "../", "hi": "../hi/", "mr": "../mr/"}
    thanks = {
        a["lang"]: a["href"] for a in page(site, "thanks/index.html").find("a") if a.get("hreflang")
    }
    assert thanks == {"en": "../thanks/", "hi": "../hi/thanks/", "mr": "../mr/thanks/"}


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("index.html", "Hello Priya Cakes, I would like to place an order."),
        ("hi/index.html", "नमस्ते Priya Cakes, मुझे ऑर्डर देना है।"),
        ("mr/index.html", "नमस्कार Priya Cakes, मला ऑर्डर द्यायची आहे."),
    ],
)
def test_whatsapp_link_and_message(path: str, message: str) -> None:
    links = [a["href"] for a in page(build(), path).find("a") if "wa.me" in a.get("href", "")]
    assert links == [f"https://wa.me/919876543210?text={quote(message, safe='')}"]
    query = parse_qs(urlparse(links[0]).query)
    assert query["text"] == [message]
    calls = [
        a["href"] for a in page(build(), path).find("a") if a.get("href", "").startswith("tel:")
    ]
    assert "tel:+919876543210" in calls


def test_whatsapp_link_helper() -> None:
    assert (
        whatsapp_link("+919876543210", "Hi & bye")
        == "https://wa.me/919876543210?text=Hi%20%26%20bye"
    )


@pytest.mark.parametrize(
    ("business_type", "label"),
    [
        ("bakery", "Order on WhatsApp"),
        ("tailoring", "Order on WhatsApp"),
        ("other", "Order on WhatsApp"),
        ("beauty", "Book on WhatsApp"),
        ("mehendi", "Book on WhatsApp"),
        ("tuition", "Book on WhatsApp"),
    ],
)
def test_order_or_book_label(business_type: str, label: str) -> None:
    p = page(build(profile(business_type=business_type)))
    assert p.by_class["btn btn-main"][0] == label


def test_book_label_in_hindi_and_marathi() -> None:
    site = build(profile(business_type="beauty"))
    assert page(site, "hi/index.html").by_class["btn btn-main"][0] == "WhatsApp पर बुक करें"
    assert page(site, "mr/index.html").by_class["btn btn-main"][0] == "WhatsApp वर बुक करा"


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
def test_sample_badge_on_every_page(slug: str) -> None:
    site, _ = build_sample(slug)
    for path in ALL_PAGES:
        p = page(site, path)
        assert p.by_class.get("sample-badge"), path
        assert not any("wa.me" in a.get("href", "") for a in p.find("a"))


def test_real_business_has_no_sample_badge() -> None:
    site = build()
    for path in ALL_PAGES:
        assert "sample-badge" not in page(site, path).by_class
        assert "sample-note" not in site.files[path].decode()


def test_sections_without_data_are_omitted() -> None:
    p = page(build(profile(phone="", products=[], lead_time="", delivery_areas=[], area="")))
    html = p.html
    for absent in (
        'class="cta"',
        'class="menu',
        'class="howto',
        'class="areas',
        'class="contact',
        "Prices last updated",
        'class="badges"',
    ):
        assert absent not in html
    assert 'class="enquiry' in html


def test_eggless_badge_only_when_profile_says_so() -> None:
    assert "Eggless available" not in page(build()).text
    with_eggless = profile(
        products=[
            {
                "name": "Eggless sponge cake",
                "unit": "kg",
                "options": [{"label": "1 kg", "price": 700}],
            }
        ]
    )
    assert "Eggless available" in page(build(with_eggless)).text


def test_section_order() -> None:
    html = page(build()).html
    markers = [
        'class="topbar"',
        'class="hero',
        'class="cta"',
        'class="menu',
        'class="howto',
        'class="areas',
        'class="contact',
        'class="enquiry',
        'class="footer"',
    ]
    positions = [html.index(m) for m in markers]
    assert positions == sorted(positions)


def test_no_external_requests() -> None:
    site = build()
    for path in ALL_PAGES:
        p = page(site, path)
        loaded = [a["href"] for a in p.find("link") if a.get("rel") in ("stylesheet", "preload")]
        loaded += [i["src"] for i in p.find("img")]
        assert all(not href.startswith(("http:", "https:", "//")) for href in loaded)
    css = next(v for k, v in site.files.items() if k.endswith(".css")).decode()
    assert "http" not in css


def test_fonts_swap_and_only_body_font_preloaded() -> None:
    site = build()
    css = next(v for k, v in site.files.items() if k.endswith(".css")).decode()
    assert css.count("font-display: swap") == css.count("@font-face") == 6
    for path, subset in (("index.html", "latin"), ("hi/index.html", "devanagari")):
        preloads = [a["href"] for a in page(site, path).find("link") if a.get("rel") == "preload"]
        assert len(preloads) == 1 and f"mukta-400-{subset}" in preloads[0]


def test_simple_theme_skips_display_font() -> None:
    site = build(theme="simple")
    assert not any("rozha" in p for p in site.files)


def test_enquiry_form_fields() -> None:
    p = page(build(), "mr/index.html")
    form = p.find("form")[0]
    assert form["action"] == "https://example.test/api/enquiry/shop"
    inputs = {i["name"]: i for i in p.find("input")}
    assert inputs["lang"]["value"] == "mr"
    assert inputs["website"]["tabindex"] == "-1"


def test_footer() -> None:
    p = page(build(), "hi/index.html")
    assert "दाम आख़िरी बार बदले: 14/11/2026" in p.text
    assert "Test App से बनाई गई" in p.text
    assert any(a.get("href") == "https://example.test/privacy?lang=hi" for a in p.find("a"))


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
def test_sample_sites_show_no_dialable_phone(slug: str) -> None:
    site, prof = build_sample(slug)
    local_digits = prof.phone[3:]
    for path in ALL_PAGES:
        html = site.files[path].decode()
        assert local_digits not in html.replace(" ", ""), path
        assert "tel:" not in html and "wa.me" not in html
    assert "Sample business: no real phone number" in page(site).text
    assert "नमूना व्यवसाय: कोई असली फ़ोन नंबर नहीं" in page(site, "hi/index.html").text


def test_real_business_shows_phone_link() -> None:
    p = page(build())
    assert "+91 98765 43210" in p.text
    assert any(a.get("href") == "tel:+919876543210" for a in p.find("a"))


@pytest.mark.parametrize("slug", SAMPLE_SLUGS)
def test_business_name_is_the_only_h1(slug: str) -> None:
    site, prof = build_sample(slug)
    for path in PAGES:
        p = page(site, path)
        assert p.html.count("<h1") == 1
        h1 = p.html.split("<h1>")[1].split("</h1>")[0]
        assert h1 == p.html.split("<title>")[1].split("</title>")[0]
        topbar = p.html.split('<header class="topbar">')[1].split("</header>")[0]
        assert "<h1" not in topbar and "<h2" not in topbar
        # The name may appear in the top bar only as the plain desktop brand-name span.
        brand = f'<span class="brand-name" aria-hidden="true">{h1}</span>'
        assert brand in topbar
        assert prof.business_name not in Page(topbar.replace(brand, "")).text


@pytest.mark.parametrize(
    ("name", "letter"),
    [
        ("Asha's Home Bakery", "A"),
        ("meena", "M"),
        ("सुनीता ब्युटी पार्लर", "सु"),
        ("क्षमा केक्स", "क्ष"),
        ("  priya", "P"),
        ("", ""),
    ],
)
def test_monogram(name: str, letter: str) -> None:
    assert monogram(name) == letter


def test_monogram_in_top_bar() -> None:
    p = page(build())
    link = next(a for a in p.find("a") if a.get("class") == "monogram")
    assert link["aria-label"] == "Priya Cakes"
    assert 'aria-hidden="true">P</span>' in p.html


def test_chips_have_icons() -> None:
    eggless = profile(
        products=[
            {"name": "Eggless cake", "unit": "kg", "options": [{"label": "1 kg", "price": 700}]}
        ]
    )
    html = page(build(eggless)).html
    badges = html.split('<ul class="badges">')[1].split("</ul>")[0]
    assert badges.count('<li class="chip">') == 3
    assert badges.count("<svg") == 3
    assert 'aria-hidden="true"' in badges


def test_buttons_have_icons() -> None:
    cta = page(build()).html.split('<div class="cta">')[1].split("</div>")[0]
    assert cta.count("<svg") == 2


def test_no_photo_business_has_text_only_hero() -> None:
    site, _ = build_sample("savitri-gruh-udyog")
    for path in ALL_PAGES:
        html = site.files[path].decode()
        assert "<img" not in html
        assert "has-image" not in html
    assert "hero-grid no-photo" in page(site).html


def test_items_without_photos_have_no_image_area() -> None:
    site, _ = build_sample("asha-home-bakery")
    html = page(site).html
    assert html.count('class="item has-image"') == 1
    assert html.count('class="item"') == 2


def test_long_names_render_in_full() -> None:
    site, prof = build_sample("savitri-gruh-udyog")
    for path in PAGES:
        text = page(site, path).text
        assert prof.business_name in text
        for product in prof.products:
            assert product.name in text


def test_step_numbers_come_from_css_not_text() -> None:
    p = page(build())
    steps = p.html.split('<ol class="steps">')[1].split("</ol>")[0]
    assert not any(ch.isdigit() for ch in Page(steps).text.replace("3 days", ""))
    css = next(v for k, v in build().files.items() if k.endswith(".css")).decode()
    assert "counter(step)" in css


def test_display_font_only_on_h1_and_h2() -> None:
    css = next(v for k, v in build().files.items() if k.endswith(".css")).decode()
    display_rules = [line for line in css.splitlines() if "var(--display)" in line]
    assert len(display_rules) == 1 and display_rules[0].startswith("h1, h2 {")


def test_brand_name_hidden_on_mobile_shown_on_desktop() -> None:
    css = next(v for k, v in build().files.items() if k.endswith(".css")).decode()
    assert ".brand-name { display: none;" in css
    desktop = css.split("@media (min-width: 768px)")[1]
    assert ".brand-name { display: inline; }" in desktop


def test_single_unnamed_option_shows_price_beside_name() -> None:
    cupcakes = page(build()).html.split("Cupcakes")[1].split("</li>")[0]
    assert '<span class="price">₹300</span>' in cupcakes
    assert '<span class="unit">/ box</span>' in cupcakes
    assert '<dl class="prices">' not in cupcakes
    assert "per box" not in cupcakes
    hindi = page(build(), "hi/index.html").html.split("Cupcakes")[1].split("</li>")[0]
    assert '<span class="unit">/ डिब्बा</span>' in hindi


@pytest.mark.parametrize(
    ("unit", "suffix"),
    [
        ("kg", "/ kg"),
        ("box", "/ box"),
        ("month", "/ month"),
        ("hand", "/ hand"),
        ("service", ""),
        ("piece", ""),
    ],
)
def test_unit_suffix_only_for_ambiguous_units(unit: str, suffix: str) -> None:
    prof = profile(
        products=[{"name": "Item", "unit": unit, "options": [{"label": "", "price": 1500}]}]
    )
    item = page(build(prof)).html.split('<li class="item">')[1].split("</li>")[0]
    assert '<span class="price">₹1,500</span>' in item
    if suffix:
        assert f'<span class="unit">{suffix}</span>' in item
    else:
        assert 'class="unit"' not in item
    assert "per service" not in item and "per piece" not in item


def test_named_options_keep_price_rows() -> None:
    chocolate = page(build()).html.split("Chocolate cake")[1].split("</li>")[0]
    assert '<dl class="prices">' in chocolate and "single" not in chocolate


@pytest.mark.parametrize(
    ("area", "lang", "tagline"),
    [
        ("Kothrud, Pune", "en", "Bakery in Kothrud, Pune"),
        ("FC Road, Pune", "en", "Bakery on FC Road, Pune"),
        ("Sinhagad Road", "en", "Bakery on Sinhagad Road"),
        ("Kothrud, Pune", "hi", "Kothrud, Pune में बेकरी"),
        ("FC Road, Pune", "hi", "FC Road, Pune पर बेकरी"),
        ("Sinhagad Road", "hi", "Sinhagad Road पर बेकरी"),
        ("Kothrud, Pune", "mr", "Kothrud, Pune मधील बेकरी"),
        ("FC Road, Pune", "mr", "FC Road, Pune वरील बेकरी"),
        ("Sinhagad Road", "mr", "Sinhagad Road वरील बेकरी"),
        ("Karve Nagar", "en", "Bakery in Karve Nagar"),
        ("Tilak Marg", "en", "Bakery on Tilak Marg"),
    ],
)
def test_tagline_uses_on_for_roads(area: str, lang: str, tagline: str) -> None:
    path = {"en": "index.html", "hi": "hi/index.html", "mr": "mr/index.html"}[lang]
    p = page(build(profile(area=area)), path)
    assert p.html.split('<p class="overline">')[1].split("</p>")[0] == tagline


def test_sample_photos_are_labelled_webp_within_budget() -> None:
    site, _ = build_sample("asha-home-bakery")
    images = {k: v for k, v in site.files.items() if k.startswith("assets/img/")}
    assert images and all(k.endswith(".webp") for k in images)
    for name, data in images.items():
        limit = 90 * 1024 if "-960." in name else 40 * 1024
        assert len(data) <= limit, name
    label = '<span class="photo-label" aria-hidden="true">{}</span>'
    assert page(site).html.count(label.format("Sample photo")) == 2
    assert label.format("नमूना फ़ोटो") in page(site, "hi/index.html").html


def test_real_photos_have_no_sample_label() -> None:
    photos = (SitePhoto(data=_jpeg(800, 600)),)
    assert "photo-label" not in page(build(photos=photos)).html


def test_unknown_sample_photo_rejected() -> None:
    from app.services.site_images import sample_photo_bytes

    for name in ("../../config.py", "missing.jpg", "bakery.png"):
        with pytest.raises(ValueError):
            sample_photo_bytes(name)


def test_alternate_sections_are_tinted() -> None:
    css = next(v for k, v in build().files.items() if k.endswith(".css")).decode()
    assert "main > section:nth-of-type(even) { background: var(--band); }" in css


def test_photos_between_sizes_get_a_full_width_version() -> None:
    site = build(photos=(SitePhoto(data=_jpeg(853, 1280)),))
    srcset = page(site).find("img")[0]["srcset"]
    assert "480w" in srcset and "853w" in srcset
