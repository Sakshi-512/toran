"""Business site themes. All three share one layout and differ only in colours and fonts."""

from dataclasses import dataclass

BODY_FONT = "Mukta"
DISPLAY_FONT = "Rozha One"
MIN_CONTRAST = 4.5


@dataclass(frozen=True)
class Theme:
    """Colours and fonts for one theme. Every text/background pair must reach 4.5:1."""

    name: str
    background: str
    surface: str
    text: str
    heading: str
    accent: str
    on_accent: str
    heading_font: str

    def contrast_pairs(self) -> dict[str, tuple[str, str]]:
        """Every foreground/background pair the layout uses."""
        return {
            "text on background": (self.text, self.background),
            "text on surface": (self.text, self.surface),
            "heading on background": (self.heading, self.background),
            "heading on surface": (self.heading, self.surface),
            "link on background": (self.accent, self.background),
            "link on surface": (self.accent, self.surface),
            "button text on accent": (self.on_accent, self.accent),
        }


THEMES: dict[str, Theme] = {
    "warm": Theme(
        name="warm",
        background="#FFF8EE",
        surface="#FFFFFF",
        text="#3B2416",
        heading="#7C2D12",
        accent="#C2410C",
        on_accent="#FFFFFF",
        heading_font=DISPLAY_FONT,
    ),
    "elegant": Theme(
        name="elegant",
        background="#1F2A2E",
        surface="#26343A",
        text="#F5E9D7",
        heading="#D4A373",
        accent="#D4A373",
        on_accent="#1F2A2E",
        heading_font=DISPLAY_FONT,
    ),
    "simple": Theme(
        name="simple",
        background="#FFFFFF",
        surface="#F5F5F4",
        text="#1F1B16",
        heading="#1F1B16",
        accent="#0F766E",
        on_accent="#FFFFFF",
        heading_font=BODY_FONT,
    ),
}
DEFAULT_THEME = "warm"


def get_theme(name: str) -> Theme:
    """Return the named theme, or the default one for unknown names."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else float(((c + 0.055) / 1.055) ** 2.4)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a #RRGGBB colour."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two colours (1 to 21)."""
    high, low = sorted(
        (relative_luminance(foreground), relative_luminance(background)), reverse=True
    )
    return (high + 0.05) / (low + 0.05)
