from dataclasses import replace

import pytest

from app.services.score import (
    PARTS,
    SPECS,
    Context,
    Item,
    Measurements,
    Score,
    score,
    shared_maximums,
)

GOOD = Measurements(
    content_ms=1200,
    bytes_transferred=250_000,
    cta_found=True,
    cta_above_fold=True,
    small_targets=0,
    sideways_scroll=False,
    laptop_overflow=0,
    phone_visible=True,
    area_shown=True,
    address_found=False,
    https=True,
    policy_text_found=False,
)
LIVE_REAL = Context(is_sample=False, is_production=True)
LOCAL_SAMPLE = Context(is_sample=True, is_production=False)


def _item(result_score: Score, key: str) -> Item:
    return next(i for i in result_score.items if i.key == key)


def test_specs_match_docs_score_md() -> None:
    for part in PARTS:
        assert sum(s.max_points for s in SPECS if s.part == part) == 20
    assert {s.key for s in SPECS if s.source == "ai_assessed"} == {
        "language_clarity",
        "real_photos",
        "policy_shown",
        "questions_answered",
    }


def test_perfect_real_site_in_production() -> None:
    result = score(GOOD, LIVE_REAL)
    assert result.total == 52
    assert result.possible_so_far == 52
    assert result.pending_points == 48
    assert all(i.points == i.max_points for i in result.items if i.status == "scored")


def test_every_item_records_measured_or_ai_assessed() -> None:
    for item in score(GOOD, LIVE_REAL).items:
        assert item.source in ("measured", "ai_assessed")
        if item.source == "ai_assessed":
            assert item.status == "pending" and item.points == 0


def test_not_applicable_points_are_shared_within_the_part() -> None:
    result = score(GOOD, LOCAL_SAMPLE)
    assert _item(result, "phone_visible").status == "not_applicable"
    assert _item(result, "https").status == "not_applicable"
    # Trust: 5 + 3 points do not apply; 8 shared over area, photos and policy (4 each).
    for key in ("area_shown", "real_photos", "policy_shown"):
        assert _item(result, key).max_points == pytest.approx(20 / 3, abs=0.01)
    assert result.possible_so_far == pytest.approx(40 + 20 / 3, abs=0.01)
    assert result.pending_points == pytest.approx(40 + 40 / 3, abs=0.02)


def test_shared_maximums_keep_every_part_at_20() -> None:
    statuses = {s.key: "scored" for s in SPECS}
    statuses["https"] = "not_applicable"
    maximums = shared_maximums(statuses)  # type: ignore[arg-type]
    for part in PARTS:
        assert sum(maximums[s.key] for s in SPECS if s.part == part) == pytest.approx(20)
    assert maximums["https"] == 0
    assert maximums["phone_visible"] == pytest.approx(5 + 3 * 5 / 17)


def test_https_only_counts_in_production() -> None:
    insecure = replace(GOOD, https=False)
    assert _item(score(insecure, LIVE_REAL), "https").points == 0
    assert _item(score(insecure, Context(False, False)), "https").status == "not_applicable"


@pytest.mark.parametrize(
    ("ms", "points"),
    [(1000, 14), (4000, 14), (4001, 7), (7000, 7), (7001, 3.5), (10000, 3.5), (10001, 0)],
)
def test_content_time_tiers(ms: int, points: float) -> None:
    assert _item(score(replace(GOOD, content_ms=ms), LIVE_REAL), "content_time").points == points


@pytest.mark.parametrize(
    ("size", "points"),
    [(100_000, 6), (500 * 1024 - 1, 6), (500 * 1024, 3), (1024 * 1024 - 1, 3), (1024 * 1024, 0)],
)
def test_page_weight_tiers(size: int, points: float) -> None:
    result = score(replace(GOOD, bytes_transferred=size), LIVE_REAL)
    assert _item(result, "page_weight").points == points


def test_failures_score_zero_with_reasons() -> None:
    bad = replace(
        GOOD,
        cta_above_fold=False,
        small_targets=3,
        sideways_scroll=True,
        laptop_overflow=2,
        phone_visible=False,
        address_found=True,
    )
    result = score(bad, LIVE_REAL)
    for key in (
        "cta_above_fold",
        "tap_targets",
        "no_sideways_scroll",
        "laptop_no_overflow",
        "phone_visible",
        "area_shown",
    ):
        assert _item(result, key).points == 0, key
    assert (
        _item(result, "tap_targets").reasons["en"]
        == "3 buttons or links are smaller than 44 pixels."
    )
    assert "3 बटन" in _item(result, "tap_targets").reasons["hi"]
    assert "घराच्या पत्त्यासारखे" in _item(result, "area_shown").reasons["mr"]
    assert result.total == 14 + 6 + 3


def test_missing_order_button() -> None:
    result = score(replace(GOOD, cta_found=False, cta_above_fold=False), LIVE_REAL)
    assert _item(result, "cta_above_fold").reasons["en"] == "There is no order button."


def test_reasons_in_three_languages_with_numbers() -> None:
    item = _item(score(replace(GOOD, content_ms=3240), LIVE_REAL), "content_time")
    assert item.reasons["en"] == "On slow internet, the main content appeared in 3.2 seconds."
    assert "3.2 सेकंड" in item.reasons["hi"]
    assert "3.2 सेकंदांत" in item.reasons["mr"]


def test_policy_text_kept_as_detail_but_pending() -> None:
    item = _item(score(replace(GOOD, policy_text_found=True), LIVE_REAL), "policy_shown")
    assert item.status == "pending"
    assert item.detail == {"policy_text_found": True}
