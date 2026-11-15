"""Pydantic schemas. BusinessProfile is the single source of truth for every fact on a site."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BusinessType = Literal["bakery", "tailoring", "tuition", "mehendi", "beauty", "other"]
Unit = Literal["piece", "box", "kg", "month", "hand", "service"]
OrderMethod = Literal["whatsapp", "call", "sms", "instagram", "in_person"]

SERVICE_TYPES: frozenset[str] = frozenset({"beauty", "mehendi", "tuition"})
MAX_PRICE = 10_000_000
_PHONE_STRIP = re.compile(r"[\s\-().]")
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")
_INTERNATIONAL = re.compile(r"^\+\d{10,15}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductOption(_Strict):
    """One priced option of a product, for example "500 g" for 450 rupees."""

    label: str = Field(default="", max_length=80)
    price: int = Field(ge=0, le=MAX_PRICE)

    @field_validator("price", mode="before")
    @classmethod
    def _no_bool_or_fraction(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("price must be a number")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("price must be a whole number of rupees")
        return value


class Product(_Strict):
    """A product or service the business sells."""

    name: str = Field(min_length=1, max_length=120)
    unit: Unit
    options: list[ProductOption] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=500)


class BusinessProfile(_Strict):
    """The confirmed business profile, exactly as defined in CLAUDE.md."""

    business_name: str = Field(default="", max_length=120)
    business_type: BusinessType = "other"
    area: str = Field(default="", max_length=120)
    delivery_areas: list[str] = Field(default_factory=list, max_length=30)
    languages: list[str] = Field(default_factory=list, max_length=5)
    products: list[Product] = Field(default_factory=list, max_length=100)
    order_methods: list[OrderMethod] = Field(
        default_factory=lambda: _default_methods(), max_length=5
    )
    lead_time: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=20)
    missing: list[str] = Field(default_factory=list, max_length=50)
    policy: str = Field(default="", max_length=500)

    @field_validator("delivery_areas", "missing")
    @classmethod
    def _clean_list(cls, values: list[str]) -> list[str]:
        return _dedupe([v.strip() for v in values if v and v.strip()])

    @field_validator("languages")
    @classmethod
    def _clean_languages(cls, values: list[str]) -> list[str]:
        return _dedupe([v.strip().lower() for v in values if v and v.strip()])

    @field_validator("order_methods")
    @classmethod
    def _dedupe_methods(cls, values: list[OrderMethod]) -> list[OrderMethod]:
        return _dedupe(values)

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, value: str) -> str:
        return normalise_phone(value)

    @property
    def is_service(self) -> bool:
        """Service businesses show "Book on WhatsApp"; product businesses show "Order"."""
        return self.business_type in SERVICE_TYPES

    @property
    def cta_key(self) -> str:
        """i18n key for the main call-to-action button."""
        return "cta.book_whatsapp" if self.is_service else "cta.order_whatsapp"


def _default_methods() -> list[OrderMethod]:
    return ["whatsapp"]


def normalise_phone(value: str) -> str:
    """Return a phone number in +<country><number> form, or "" if empty. Raises on bad input."""
    raw = _PHONE_STRIP.sub("", value or "")
    if not raw:
        return ""
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("+"):
        if not _INTERNATIONAL.match(raw):
            raise ValueError("phone number is not valid")
        return raw
    if raw.startswith("0") and len(raw) == 11:
        raw = raw[1:]
    if raw.startswith("91") and len(raw) == 12:
        raw = raw[2:]
    if not _INDIAN_MOBILE.match(raw):
        raise ValueError("phone number is not valid")
    return "+91" + raw


def _dedupe[T](values: list[T]) -> list[T]:
    seen: list[T] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
