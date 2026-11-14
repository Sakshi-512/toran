import pytest

from app.services.themes import MIN_CONTRAST, THEMES, contrast_ratio, get_theme


@pytest.mark.parametrize("name", list(THEMES))
def test_every_colour_pair_meets_contrast(name: str) -> None:
    for label, (fg, bg) in THEMES[name].contrast_pairs().items():
        assert contrast_ratio(fg, bg) >= MIN_CONTRAST, f"{name}: {label} {fg} on {bg}"


def test_theme_colours_as_specified() -> None:
    warm, elegant, simple = THEMES["warm"], THEMES["elegant"], THEMES["simple"]
    assert (warm.background, warm.text, warm.heading, warm.accent) == (
        "#FFF8EE",
        "#3B2416",
        "#7C2D12",
        "#C2410C",
    )
    assert (elegant.background, elegant.text, elegant.accent) == ("#1F2A2E", "#F5E9D7", "#D4A373")
    assert (simple.background, simple.text, simple.accent) == ("#FFFFFF", "#1F1B16", "#0F766E")
    assert warm.heading_font == elegant.heading_font == "Rozha One"
    assert simple.heading_font == "Mukta"


def test_contrast_ratio_reference_values() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)


def test_unknown_theme_falls_back() -> None:
    assert get_theme("neon").name == "warm"
