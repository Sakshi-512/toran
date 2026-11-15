"""Business site themes. All three share one layout and differ only in colours and fonts."""

from dataclasses import dataclass

BODY_FONT = "Mukta"
DISPLAY_FONT = "Rozha One"
MIN_CONTRAST = 4.5
CHIP_ALPHA = 0.10
BAND_ALPHA = 0.04


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
    border: str
    footer_bg: str
    footer_text: str
    shadow: str

    @property
    def band_bg(self) -> str:
        """Tint behind every other section: the accent at 4% over the background."""
        return mix(self.accent, self.background, BAND_ALPHA)

    @property
    def chip_bg(self) -> str:
        """Soft chip fill: the accent at 10% over the background."""
        return mix(self.accent, self.background, CHIP_ALPHA)

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
            "text on chip": (self.text, self.chip_bg),
            "heading on chip": (self.heading, self.chip_bg),
            "footer text on footer": (self.footer_text, self.footer_bg),
            "text on band": (self.text, self.band_bg),
            "heading on band": (self.heading, self.band_bg),
            "link on band": (self.accent, self.band_bg),
            "text on chip over band": (self.text, mix(self.accent, self.band_bg, CHIP_ALPHA)),
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
        border="#EBDCC8",
        footer_bg="#3B2416",
        footer_text="#FFF8EE",
        shadow="0 1px 2px rgba(59, 36, 22, .06), 0 12px 32px rgba(59, 36, 22, .08)",
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
        border="#34474E",
        footer_bg="#151D20",
        footer_text="#F5E9D7",
        shadow="0 1px 2px rgba(0, 0, 0, .25), 0 12px 32px rgba(0, 0, 0, .28)",
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
        border="#E7E5E4",
        footer_bg="#1F1B16",
        footer_text="#FAFAF9",
        shadow="0 1px 2px rgba(31, 27, 22, .05), 0 12px 32px rgba(31, 27, 22, .07)",
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


def mix(foreground: str, background: str, alpha: float) -> str:
    """Blend foreground over background at alpha (0 to 1); returns #RRGGBB."""
    fg = [int(foreground.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    bg = [int(background.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(
        f"{round(f * alpha + b * (1 - alpha)):02X}" for f, b in zip(fg, bg, strict=True)
    )
