"""Website builder: BusinessProfile + theme -> a complete static website (a map of files).

Business text comes only from the profile and is never rewritten. Interface labels come from
i18n. Prices and numbers are formatted in code from the profile, the same on every language page.
build_site() does no I/O apart from reading bundled templates, fonts and sample illustrations;
write_site() swaps a finished build into place.
"""

import hashlib
import shutil
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader

from app.schemas import BusinessProfile, Product
from app.services.address_guard import address_reason
from app.services.i18n import t
from app.services.site_images import ImageRef, SitePhoto, prepare_photo
from app.services.themes import DISPLAY_FONT, Theme, get_theme

SITE_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "sites"
FONT_DIR = SITE_TEMPLATE_DIR / "assets" / "fonts"
LANGUAGES: tuple[str, ...] = ("en", "hi", "mr")
LANG_PREFIX = {"en": "", "hi": "hi/", "mr": "mr/"}
ROAD_WORDS = frozenset({"road", "rd", "marg", "path", "रोड", "मार्ग", "पथ"})
UNIT_SUFFIX_UNITS = frozenset({"kg", "box", "month", "hand"})
EGGLESS_WORDS = (
    "eggless",
    "egg-free",
    "egg free",
    "बिना अंडे",
    "बिना अंडा",
    "अंडारहित",
    "अंडे विरहित",
    "बिनअंड्याचे",
    "बिना अंड्याचे",
)

LATIN_RANGE = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, "
    "U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)
DEVANAGARI_RANGE = (
    "U+0900-097F, U+1CD0-1CF4, U+1CF7-1CF9, U+200C-200D, U+20A8, U+20B9, U+20F0, U+25CC, "
    "U+A830-A839, U+A8E0-A8FF, U+11B00-11B0A"
)


@dataclass(frozen=True)
class FontFace:
    """One self-hosted WOFF2 file covering one script subset."""

    family: str
    weight: int
    subset: str
    filename: str

    @property
    def unicode_range(self) -> str:
        """CSS unicode-range for this subset."""
        return LATIN_RANGE if self.subset == "latin" else DEVANAGARI_RANGE


FONT_FACES: tuple[FontFace, ...] = (
    FontFace("Mukta", 400, "latin", "mukta-400-latin.woff2"),
    FontFace("Mukta", 400, "devanagari", "mukta-400-devanagari.woff2"),
    FontFace("Mukta", 700, "latin", "mukta-700-latin.woff2"),
    FontFace("Mukta", 700, "devanagari", "mukta-700-devanagari.woff2"),
    FontFace(DISPLAY_FONT, 400, "latin", "rozha-400-latin.woff2"),
    FontFace(DISPLAY_FONT, 400, "devanagari", "rozha-400-devanagari.woff2"),
)


@dataclass(frozen=True)
class SiteInput:
    """Everything needed to build one business site."""

    slug: str
    display_name: str
    profile: BusinessProfile
    theme: str
    is_sample: bool
    prices_updated: date
    site_url: str
    main_url: str
    enquiry_url: str
    app_name: str
    photos: tuple[SitePhoto, ...] = ()


@dataclass(frozen=True)
class SiteWarning:
    """Something left off the site that the owner should be asked about."""

    code: str
    field: str
    value: str


@dataclass
class SiteBuild:
    """A finished site: files by path relative to the site root, plus warnings for the owner."""

    files: dict[str, bytes] = field(default_factory=dict)
    warnings: list[SiteWarning] = field(default_factory=list)

    @staticmethod
    def page_path(lang: str, page: str = "") -> str:
        """Path of a page, e.g. page_path("hi") -> "hi/index.html"."""
        return f"{LANG_PREFIX[lang]}{page + '/' if page else ''}index.html"


@dataclass
class _Assets:
    css: str
    preload: dict[str, str]
    hero: ImageRef | None
    product_images: dict[str, ImageRef]


def format_inr(amount: int) -> str:
    """Format whole rupees with Indian digit grouping: 125000 -> "₹1,25,000"."""
    digits = str(amount)
    if len(digits) <= 3:
        return f"₹{digits}"
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join([*groups, tail])


def format_phone(phone: str) -> str:
    """Readable phone number: +919876543210 -> "+91 98765 43210"."""
    if phone.startswith("+91") and len(phone) == 13:
        return f"+91 {phone[3:8]} {phone[8:]}"
    return phone


def whatsapp_link(phone: str, message: str) -> str:
    """wa.me link with a pre-filled message."""
    return f"https://wa.me/{phone.lstrip('+')}?text={quote(message, safe='')}"


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def _environment() -> Environment:
    return Environment(loader=FileSystemLoader(SITE_TEMPLATE_DIR), autoescape=True)


_ENV = _environment()


def build_site(site: SiteInput) -> SiteBuild:
    """Build every page (en, hi, mr and their thank-you pages) and all assets for a site."""
    build = SiteBuild()
    theme = get_theme(site.theme)
    areas = _safe_areas(site.profile, build.warnings)
    assets = _build_assets(site, theme, build.files)
    for lang in LANGUAGES:
        for page, template in (("", "index.html"), ("thanks", "thanks.html")):
            path = SiteBuild.page_path(lang, page)
            context = _page_context(site, theme, assets, areas, lang, path)
            build.files[path] = _ENV.get_template(template).render(context).encode("utf-8")
    return build


def _safe_areas(profile: BusinessProfile, warnings: list[SiteWarning]) -> dict[str, Any]:
    """Area and delivery areas without anything that looks like an address (rule 3)."""

    def keep(field_name: str, value: str) -> bool:
        reason = address_reason(value)
        if reason:
            warnings.append(SiteWarning(f"looks_like_address:{reason}", field_name, value))
        return reason is None

    area = profile.area if profile.area and keep("area", profile.area) else ""
    delivery = [a for a in profile.delivery_areas if keep("delivery_areas", a)]
    return {"area": area, "delivery": delivery}


def _build_assets(site: SiteInput, theme: Theme, files: dict[str, bytes]) -> _Assets:
    faces = [
        f for f in FONT_FACES if f.family != DISPLAY_FONT or theme.heading_font == DISPLAY_FONT
    ]
    font_paths: dict[FontFace, str] = {}
    for face in faces:
        data = (FONT_DIR / face.filename).read_bytes()
        target = f"assets/fonts/{face.filename.removesuffix('.woff2')}.{_short_hash(data)}.woff2"
        files[target] = data
        font_paths[face] = target
    css = _ENV.get_template("site.css").render(theme=theme, faces=font_paths).encode("utf-8")
    css_path = f"assets/site.{_short_hash(css)}.css"
    files[css_path] = css
    preload = {
        "latin": font_paths[FONT_FACES[0]],
        "devanagari": font_paths[FONT_FACES[1]],
    }
    hero, product_images = _build_photos(site, files)
    return _Assets(css_path, preload, hero, product_images)


def _build_photos(
    site: SiteInput, files: dict[str, bytes]
) -> tuple[ImageRef | None, dict[str, ImageRef]]:
    hero: ImageRef | None = None
    products: dict[str, ImageRef] = {}
    product_names = {p.name for p in site.profile.products}
    for index, photo in enumerate(site.photos):
        if photo.subject is None and hero is None:
            ref, new_files = prepare_photo(photo, "hero")
            hero = ref
        elif photo.subject in product_names and photo.subject not in products:
            ref, new_files = prepare_photo(photo, f"item-{index}")
            products[photo.subject] = ref
        else:
            continue
        files.update(new_files)
    return hero, products


def _page_context(
    site: SiteInput,
    theme: Theme,
    assets: _Assets,
    areas: dict[str, Any],
    lang: str,
    path: str,
) -> dict[str, Any]:
    tr = partial(t, lang=lang)
    profile = site.profile
    root = "../" * path.count("/")
    name = profile.business_name or site.display_name
    service = profile.is_service
    kind = "book" if service else "order"
    return {
        "lang": lang,
        "t": tr,
        "root": root,
        "name": name,
        "theme": theme,
        "is_sample": site.is_sample,
        "css_href": root + assets.css,
        "preload_href": root + assets.preload["latin" if lang == "en" else "devanagari"],
        "languages": _language_links(lang, path, root),
        "alternates": [(code, site.site_url + "/" + LANG_PREFIX[code]) for code in LANGUAGES],
        "canonical": site.site_url + "/" + LANG_PREFIX[lang],
        "home_href": root + LANG_PREFIX[lang] or "./",
        "tagline": _tagline(profile, areas["area"], tr),
        "badges": _badges(profile, areas, kind, tr),
        "hero": _image(assets.hero, root, tr, name, site.is_sample, lazy=False),
        "cta": _cta(site, name, kind, tr),
        "products": [_product(p, assets, root, tr, site.is_sample) for p in profile.products],
        "monogram": monogram(name),
        "menu_overline": tr(f"site.overline_menu_{kind}"),
        "menu_title": tr(f"site.menu_title_{kind}"),
        "areas_overline": tr(f"site.overline_areas_{kind}"),
        "steps": _steps(profile, kind, tr),
        "howto_title": tr(f"site.howto_title_{kind}"),
        "areas": _area_list(areas),
        "area_title": tr(f"site.area_title_{kind}"),
        "area": areas["area"],
        **_phone(profile.phone, site.is_sample, tr),
        "enquiry_url": site.enquiry_url,
        "updated": site.prices_updated.strftime("%d/%m/%Y"),
        "app_name": site.app_name,
        "privacy_href": f"{site.main_url}/privacy?lang={lang}",
    }


def _phone(phone: str, is_sample: bool, tr: Callable[..., str]) -> dict[str, str]:
    """Phone text and link for the contact section. Sample sites never show a dialable number."""
    if not phone:
        return {"phone_text": "", "phone_href": ""}
    if is_sample:
        return {"phone_text": tr("site.sample_no_phone"), "phone_href": ""}
    return {"phone_text": format_phone(phone), "phone_href": f"tel:{phone}"}


def _language_links(lang: str, path: str, root: str) -> list[dict[str, Any]]:
    page = "thanks/" if "thanks/" in path else ""
    return [
        {
            "code": code,
            "label": t(f"lang.{code}", code),
            "href": (root + LANG_PREFIX[code] + page) or "./",
            "current": code == lang,
        }
        for code in LANGUAGES
    ]


def is_road(area: str) -> bool:
    """True when the area's first part names a road ("Sinhagad Road, Pune"), which takes "on"."""
    first = area.split(",")[0].strip().lower().rstrip(".")
    return bool(first) and first.split()[-1] in ROAD_WORDS


def _tagline(profile: BusinessProfile, area: str, tr: Callable[..., str]) -> str:
    kind = profile.business_type
    if not area:
        return tr(f"site.tagline_noarea.{kind}")
    key = "site.tagline_on" if is_road(area) else "site.tagline"
    return tr(f"{key}.{kind}", area=area)


def monogram(name: str) -> str:
    """First letter of the name, kept whole for Devanagari (base letter plus its marks)."""
    text = name.strip()
    if not text:
        return ""
    letter = text[0]
    for char in text[1:]:
        if unicodedata.category(char) in ("Mn", "Mc") or char == "\u094d":
            letter += char
        else:
            break
    if letter.endswith("\u094d"):  # a half letter (virama) needs the next letter to render
        letter += text[len(letter)] if len(text) > len(letter) else ""
    return letter.upper()


def _badges(
    profile: BusinessProfile, areas: dict[str, Any], kind: str, tr: Callable[..., str]
) -> list[dict[str, str]]:
    badges = []
    texts = " ".join(f"{p.name} {p.notes}" for p in profile.products).lower()
    if any(word in texts for word in EGGLESS_WORDS):
        badges.append({"icon": "leaf", "text": tr("site.badge_eggless")})
    if profile.lead_time:
        lead = tr(f"site.badge_lead_time_{kind}", lead_time=profile.lead_time)
        badges.append({"icon": "clock", "text": lead})
    delivery = areas["delivery"]
    if delivery:
        key = f"site.badge_area_{kind}" + ("_more" if len(delivery) > 1 else "")
        badges.append({"icon": "pin", "text": tr(key, area=delivery[0])})
    return badges


def _image(
    ref: ImageRef | None,
    root: str,
    tr: Callable[..., str],
    subject: str,
    sample: bool,
    lazy: bool,
) -> dict[str, Any] | None:
    if ref is None:
        return None
    alt_key = "site.sample_photo_alt" if sample else "site.photo_alt"
    srcset = ", ".join(f"{root}{src} {width}w" for src, width in ref.srcset)
    return {
        "src": root + ref.src,
        "srcset": srcset,
        "width": ref.width,
        "height": ref.height,
        "alt": tr(alt_key, subject=subject),
        "lazy": lazy,
        "label": tr("site.sample_photo_label") if sample else "",
    }


def _cta(site: SiteInput, name: str, kind: str, tr: Callable[..., str]) -> dict[str, str] | None:
    phone = site.profile.phone
    if not phone:
        return None
    if site.is_sample:
        wa_href = call_href = "#sample-note"
    else:
        wa_href = whatsapp_link(phone, tr(f"site.wa_message_{kind}", business_name=name))
        call_href = f"tel:{phone}"
    show_wa = "whatsapp" in site.profile.order_methods
    return {
        "wa_label": tr(f"cta.{kind}_whatsapp") if show_wa else "",
        "wa_href": wa_href if show_wa else "",
        "call_label": tr("site.cta_call"),
        "call_href": call_href,
    }


def _product(
    product: Product, assets: _Assets, root: str, tr: Callable[..., str], sample: bool
) -> dict[str, Any]:
    unit = tr(f"site.unit.{product.unit}")
    single = len(product.options) == 1 and not product.options[0].label
    suffix = tr(f"site.per_unit.{product.unit}") if product.unit in UNIT_SUFFIX_UNITS else ""
    return {
        "name": product.name,
        "notes": product.notes,
        "single_price": format_inr(product.options[0].price) if single else "",
        "unit_suffix": suffix if single else "",
        "options": []
        if single
        else [(o.label or unit, format_inr(o.price)) for o in product.options],
        "image": _image(
            assets.product_images.get(product.name), root, tr, product.name, sample, lazy=True
        ),
    }


def _steps(profile: BusinessProfile, kind: str, tr: Callable[..., str]) -> list[str]:
    if not profile.phone:
        return []
    last = (
        tr(f"site.step_lead_time_{kind}", lead_time=profile.lead_time)
        if profile.lead_time
        else tr("site.step_confirm")
    )
    return [tr(f"site.step_choose_{kind}"), tr("site.step_message"), last]


def _area_list(areas: dict[str, Any]) -> list[str]:
    listed = list(areas["delivery"])
    if areas["area"] and areas["area"] not in listed:
        listed.insert(0, areas["area"])
    return listed


def write_site(build: SiteBuild, target: Path) -> None:
    """Write a build to target, replacing any previous version in one step."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    for rel_path, data in build.files.items():
        out = staging / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    old = target.with_name(f".{target.name}-old")
    if target.exists():
        shutil.rmtree(old, ignore_errors=True)
        target.replace(old)
    staging.replace(target)
    shutil.rmtree(old, ignore_errors=True)
