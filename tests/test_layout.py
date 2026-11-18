"""Layout check in real Chromium: no element sticks out of its card or container, and nothing is
wider than the screen, on every owner page and the public pages, at four screen widths.

The page itself may not scroll sideways while a child still overflows its card (a grid column
grows past the card, for example), so this checks each element against its nearest container,
not only the page width.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.config import get_settings
from app.models import (
    ActionLog,
    AgentRun,
    DomainLookup,
    OwnerQuestion,
    ScoreItem,
    TestRun,
    utcnow,
)
from app.services import publish as pub
from app.services.score_runs import queue_score
from tests.live_server import live_server
from tests.test_publish import _business

pytestmark = pytest.mark.browser

WIDTHS = (360, 768, 1024, 1280)
LONG_NAME = "Chocolate cake with a very long translated name for the layout check"
LONG_WHY = "English readers can understand the product name and order it easily."
HEIGHT = 900

# Boxes an element must stay inside: the nearest one of these around it.
CONTAINERS = ", ".join(
    [
        "main",
        ".card",
        ".try-card",
        ".how-step",
        ".stat",
        ".option",
        ".before-send",
        ".domain-item",
        ".score-item",
        ".tool-call",
        ".delete-warning",
        ".notice-ok",
        ".notice-error",
        ".progress",
        ".gallery-card",
        ".phone-frame",
    ]
)

CHECK_JS = (Path(__file__).parent / "layout_check.js").read_text(encoding="utf-8")


@dataclass
class Owner:
    """A signed-in owner whose business has data for every owner page."""

    base: str
    business_id: int
    run_id: int
    agent_run_id: str


def _seed_owner_pages(engine: Engine, business_id: int) -> tuple[int, str]:
    """A draft, a score report, an agent run with rounds, a question and a domain lookup."""
    pub.build_draft(engine, get_settings(), business_id)
    with Session(engine) as session:
        run = TestRun(
            business_id=business_id,
            site_hash="h",
            url="u",
            site_kind="draft",
            total=72,
            possible_so_far=100,
            ai_status="done",
        )
        session.add(run)
        session.commit()
        assert run.id is not None
        for key, part, maximum in (
            ("cta_above_fold", "devices", 8),
            ("phone_visible", "trust", 5),
            ("language_clarity", "language", 20),
            ("https", "trust", 3),
        ):
            session.add(
                ScoreItem(
                    run_id=run.id,
                    part=part,
                    key=key,
                    source="measured",
                    status="scored",
                    points=0,
                    max_points=maximum,
                    reason_en="A long reason that wraps over more than one line on a phone screen.",
                    reason_hi="एक लंबा कारण जो फ़ोन की स्क्रीन पर एक से ज़्यादा पंक्तियों में आता है।",
                    reason_mr="एक मोठे कारण जे फोनच्या स्क्रीनवर एकापेक्षा जास्त ओळींत येते.",
                )
            )
        agent = AgentRun(
            id="ag_layout",
            business_id=business_id,
            status="done",
            stop_reason="needs_owner",
            rounds=1,
            score_before=72,
            score_after=88,
            finished_at=utcnow(),
        )
        session.add(agent)
        steps: list[tuple[str, str | None, dict[str, Any]]] = [
            (
                "diagnosis",
                None,
                {
                    "score": 72,
                    "lost": [
                        {
                            "key": "language_clarity",
                            "points": 10,
                            "max": 20,
                            "reason": {"en": "Names are in another script."},
                        }
                    ],
                },
            ),
            (
                "tool",
                "add_translation",
                {
                    "input": {
                        "target": "product_name",
                        "product": 1,
                        "lang": "en",
                        "text": LONG_NAME,
                        "why": LONG_WHY,
                    },
                    "result": "Saved.",
                    "change": {
                        "kind": "translation",
                        "field": "product_name",
                        "source": "चॉकलेट केक",
                        "lang": "en",
                        "text": LONG_NAME,
                    },
                },
            ),
            (
                "tool",
                "edit_layout",
                {
                    "input": {"change": "large_buttons", "why": "w"},
                    "result": "Saved.",
                    "change": {"kind": "layout", "change": "large_buttons"},
                },
            ),
            (
                "round_result",
                None,
                {"score_from": 72, "score_to": 88, "kept": True, "run_id": run.id},
            ),
        ]
        for step, tool, detail in steps:
            session.add(
                ActionLog(
                    business_id=business_id,
                    run_id="ag_layout",
                    round=1,
                    step=step,
                    tool=tool,
                    decision="A reason from Claude that is long enough to wrap." if tool else None,
                    detail=detail,
                )
            )
        session.add(
            OwnerQuestion(business_id=business_id, agent_run_id="ag_layout", topic="policy")
        )
        results = [
            {
                "domain": f"{name}.{tld}",
                "available": True,
                "price_usd": 12.99,
                "currency": "USD",
                "definitive": True,
                "error": None,
            }
            for name in ("ashahomebakerykothrudpune", "ashascakes")
            for tld in ("in", "com")
        ]
        session.add(
            DomainLookup(
                business_id=business_id,
                results=results,
                status="ok",
                created_at=utcnow() - timedelta(minutes=5),
            )
        )
        session.commit()
        return run.id, "ag_layout"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        launched = p.chromium.launch()
        yield launched
        launched.close()


@pytest.fixture
def owner(engine: Engine, browser: Browser) -> Iterator[tuple[Owner, Page]]:
    business_id, token, pin = _business(engine)
    run_id, agent_id = _seed_owner_pages(engine, business_id)
    with live_server() as port:
        base = f"http://localhost:{port}"
        context = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
        page = context.new_page()
        page.goto(f"{base}/o/{token}")
        page.fill("#pin", pin)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/owner")
        yield Owner(base, business_id, run_id, agent_id), page
        context.close()


def _problems(page: Page, url: str, shot: str = "") -> list[str]:
    """Problems at every width. With LAYOUT_SHOTS=<folder>, `shot` names saved screenshots."""
    found: list[str] = []
    folder = os.environ.get("LAYOUT_SHOTS")
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": HEIGHT})
        page.goto(url)
        page.wait_for_load_state("networkidle")
        if folder and shot:
            page.screenshot(path=str(Path(folder) / f"{shot}-{width}.png"), full_page=True)
        found += [f"{width}px {url}: {p}" for p in page.evaluate(CHECK_JS, CONTAINERS)]
    return found


def test_owner_pages_fit_their_cards(engine: Engine, owner: tuple[Owner, Page]) -> None:
    o, page = owner
    problems = _problems(page, o.base + "/owner", shot="owner-draft")
    pages = [
        "/owner",  # draft, with a score, the agent card, questions and domain suggestions
        "/owner?lang=mr",
        "/owner/details",
        "/owner/menu",
        "/owner/menu?lang=hi",
        "/owner/menu?lang=mr",
        "/owner/styles",
        f"/owner/score/{o.run_id}",
        f"/owner/agent/{o.agent_run_id}",
        "/owner/publish",
        "/owner/delete",
    ]
    problems += [p for path in pages for p in _problems(page, o.base + path)]
    queue_score(engine, o.business_id, "draft")  # "Testing your site like a customer..." notice
    problems += _problems(page, o.base + "/owner", shot="owner-scoring")
    problems += _problems(page, o.base + "/owner?lang=hi")
    pub.publish(engine, get_settings(), o.business_id)
    problems += _problems(page, o.base + "/owner")  # live
    pub.set_offline(engine, o.business_id, True)
    problems += _problems(page, o.base + "/owner")  # offline
    assert not problems, "\n".join(problems[:40])


def test_public_pages_fit(browser: Browser, engine: Engine) -> None:
    from tests.test_home import _usage

    _usage(engine, "ai_checks", 0.04, mock=False)  # shows "Measured on the live system"
    with live_server() as port:
        context = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
        page = context.new_page()
        problems = _problems(page, f"http://localhost:{port}/", shot="home")
        problems += _problems(page, f"http://localhost:{port}/?lang=hi", shot="home-hi")
        for path in ("/?lang=mr", "/start", "/start?option=photo&lang=hi", "/privacy?lang=hi"):
            problems += _problems(page, f"http://localhost:{port}{path}")
        context.close()
    assert not problems, "\n".join(problems[:40])


def test_business_site_fits(browser: Browser, engine: Engine) -> None:
    """A live business site in all three languages, including the enquiry note."""
    business_id, _, _ = _business(engine, slug="layout-shop")
    pub.build_draft(engine, get_settings(), business_id)
    pub.publish(engine, get_settings(), business_id)
    with live_server() as port:
        context = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
        page = context.new_page()
        problems: list[str] = []
        for lang in ("", "hi/", "mr/"):
            url = f"http://layout-shop.localhost:{port}/{lang}"
            problems += _site_problems(page, url, shot=f"site-{lang.strip('/') or 'en'}")
        context.close()
    assert not problems, "\n".join(problems[:40])


def _site_problems(page: Page, url: str, shot: str) -> list[str]:
    """Business sites use <main> too, so the same check applies."""
    return _problems(page, url, shot=shot)


def test_the_check_finds_a_card_overflow(browser: Browser) -> None:
    """The check itself works: a grid column that grows past its card is reported."""
    context = browser.new_context(viewport={"width": 1024, "height": HEIGHT})
    page = context.new_page()
    page.set_content(
        "<main style='max-width:400px'><section class='card' style='display:grid'>"
        "<p class='notice-ok'>Notice</p>"
        "<div style='display:grid;grid-template-columns:repeat(4,max-content)'>"
        + "".join(f"<a style='padding:0 40px'>Button {i}</a>" for i in range(4))
        + "</div></section></main>"
    )
    found = page.evaluate(CHECK_JS, CONTAINERS)
    context.close()
    assert any("sticks out" in p and "section.card" in p for p in found), json.dumps(found)


@pytest.mark.parametrize("lang", ["en", "hi", "mr"])
def test_done_page_fits(browser: Browser, client: Any, engine: Engine, lang: str) -> None:
    """The "Your details are saved!" page (shown once, so it is rendered and then laid out)."""
    import threading

    from tests.test_confirm_once import _ready
    from tests.test_intake_flow import _confirm

    result: dict[str, str] = {}

    def make() -> None:  # in its own thread: Playwright already runs an event loop here
        token, fields = _ready(client, engine)
        client.get(f"/start?lang={lang}")
        result["html"] = _confirm(client, token, fields).text

    worker = threading.Thread(target=make)
    worker.start()
    worker.join(timeout=60)
    html = result["html"]
    assert "secret-value pin" in html
    with live_server() as port:
        context = browser.new_context(viewport={"width": WIDTHS[0], "height": HEIGHT})
        page = context.new_page()
        problems: list[str] = []
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": HEIGHT})
            page.goto(f"http://localhost:{port}/start")  # same origin, so the styles load
            page.set_content(html, wait_until="networkidle")
            assert page.evaluate("document.styleSheets[0].cssRules.length") > 0  # styles loaded
            problems += [
                f"{width}px done ({lang}): {p}" for p in page.evaluate(CHECK_JS, CONTAINERS)
            ]
        context.close()
    assert not problems, "\n".join(problems[:40])
