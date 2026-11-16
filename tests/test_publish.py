import json
import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    Business,
    ClaudeUsage,
    IntakeSession,
    Job,
    ProfileVersion,
    SiteVersion,
    TestRun,
    utcnow,
)
from app.services import publish as pub
from app.services.auth import issue_access
from app.services.samples import get_sample
from app.services.security import CSRF_FIELD

SLUG = "asha-test-bakery"


def _business(
    engine: Engine,
    *,
    is_sample: bool = False,
    source: str = "intake",
    created_ago: timedelta = timedelta(0),
    slug: str = SLUG,
) -> tuple[int, str, str]:
    sample = get_sample("asha-home-bakery")
    assert sample is not None
    with Session(engine) as session:
        business = Business(
            slug=slug,
            display_name="Asha",
            business_type="bakery",
            is_sample=is_sample,
            source=source,
            created_at=utcnow() - created_ago,
        )
        access = issue_access(business)
        session.add(business)
        session.commit()
        assert business.id is not None
        session.add(
            ProfileVersion(
                business_id=business.id,
                version=1,
                created_by="owner",
                profile=sample.profile.model_dump(mode="json"),
            )
        )
        session.add(
            IntakeSession(
                source="voice",
                business_id=business.id,
                status="done",
                created_at=utcnow() - timedelta(minutes=10),
            )
        )
        session.commit()
        return business.id, access.link_token, access.pin


@pytest.fixture
def owner(client: TestClient, engine: Engine) -> Iterator[int]:
    business_id, token, pin = _business(engine)
    page = client.get(f"/o/{token}").text
    csrf = re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    client.post(f"/o/{token}", data={CSRF_FIELD: csrf, "pin": pin})
    yield business_id


def _csrf(client: TestClient, path: str = "/owner") -> str:
    return re.search(rf'name="{CSRF_FIELD}" value="([^"]+)"', client.get(path).text).group(1)  # type: ignore[union-attr]


def _status(engine: Engine, business_id: int) -> str:
    with Session(engine) as session:
        business = session.get(Business, business_id)
        assert business is not None
        return business.status


# --- publish service ----------------------------------------------------------------------------


def test_publish_versions_and_undo(engine: Engine) -> None:
    settings = get_settings()
    business_id, _, _ = _business(engine)
    pub.build_draft(engine, settings, business_id)
    first = pub.publish(engine, settings, business_id)
    paths = pub.folders(settings, SLUG)
    warm_css = next(paths.live.glob("assets/site.*.css")).read_text()
    assert first.number == 1 and _status(engine, business_id) == "live"
    pub.build_draft(engine, settings, business_id, theme="simple")
    second = pub.publish(engine, settings, business_id)
    assert second.number == 2 and next(paths.live.glob("assets/site.*.css")).read_text() != warm_css
    restored = pub.undo_publish(engine, settings, business_id)
    assert restored is not None and restored.number == 1
    assert next(paths.live.glob("assets/site.*.css")).read_text() == warm_css
    with Session(engine) as session:
        statuses = {v.number: v.status for v in session.exec(select(SiteVersion))}
        intake = session.exec(select(IntakeSession)).one()
        business = session.get(Business, business_id)
    assert statuses == {1: "live", 2: "undone"}
    assert intake.seconds_to_live is not None and 590 <= intake.seconds_to_live <= 700
    assert business is not None and business.first_live_at is not None
    assert paths.version(1).is_dir() and paths.version(2).is_dir()


def test_undo_first_publish_returns_to_draft(engine: Engine) -> None:
    settings = get_settings()
    business_id, _, _ = _business(engine)
    pub.build_draft(engine, settings, business_id)
    pub.publish(engine, settings, business_id)
    assert pub.undo_publish(engine, settings, business_id) is None
    assert _status(engine, business_id) == "draft"
    assert not pub.folders(settings, SLUG).live.exists()
    with pytest.raises(Exception, match=r"publish\.nothing_to_undo"):
        pub.undo_publish(engine, settings, business_id)


def test_publish_needs_a_draft(engine: Engine) -> None:
    business_id, _, _ = _business(engine)
    with pytest.raises(Exception, match=r"publish\.no_draft"):
        pub.publish(engine, get_settings(), business_id)


# --- serving by status ----------------------------------------------------------------------------


def test_draft_is_never_public_live_is_offline_shows_notice(
    client: TestClient, engine: Engine
) -> None:
    settings = get_settings()
    business_id, _, _ = _business(engine)
    pub.build_draft(engine, settings, business_id)
    host = {"Host": f"{SLUG}.example.test"}
    assert client.get("/", headers=host).status_code == 404
    assert client.get(f"/internal/tls-check?domain={SLUG}.example.test").status_code == 404
    pub.publish(engine, settings, business_id)
    assert client.get("/", headers=host).status_code == 200
    pub.set_offline(engine, business_id, offline=True)
    offline = client.get("/", headers={**host, "Accept-Language": "mr"})
    assert offline.status_code == 503 and "ही वेबसाइट सध्या बंद आहे" in offline.text
    assert client.get(f"/internal/tls-check?domain={SLUG}.example.test").status_code == 200
    pub.set_offline(engine, business_id, offline=False)
    assert client.get("/", headers=host).status_code == 200


def test_internal_draft_address_is_direct_only(client: TestClient, engine: Engine) -> None:
    business_id, _, _ = _business(engine)
    pub.build_draft(engine, get_settings(), business_id)
    assert client.get(f"/internal/draft/{SLUG}/").status_code == 200
    assert client.get(f"/internal/draft/{SLUG}/hi/").status_code == 200
    proxied = client.get(f"/internal/draft/{SLUG}/", headers={"X-Forwarded-For": "1.2.3.4"})
    assert proxied.status_code == 404


# --- owner pages ---


def test_private_preview(client: TestClient, engine: Engine, owner: int) -> None:
    assert client.get("/owner/preview/").status_code == 404  # no draft yet
    pub.build_draft(engine, get_settings(), owner)
    page = client.get("/owner/preview/")
    assert page.status_code == 200 and page.headers["x-robots-tag"] == "noindex, nofollow"
    assert "<form" not in page.text and "private preview" in page.text
    assert client.get("/owner/preview/hi/").status_code == 200
    client.post("/owner/logout", data={CSRF_FIELD: _csrf(client)})
    assert client.get("/owner/preview/").status_code == 401


def test_style_previews_and_choosing(client: TestClient, engine: Engine, owner: int) -> None:
    page = client.get("/owner/styles")
    assert page.text.count("<iframe") == 3
    preview = client.get("/owner/style-preview/elegant/")
    assert (
        preview.status_code == 200
        and "frame-ancestors 'self'" in preview.headers["content-security-policy"]
    )
    assert "x-robots-tag" in preview.headers
    css = re.search(r'href="(assets/site\.[^"]+\.css)"', preview.text).group(1)  # type: ignore[union-attr]
    assert "#1F2A2E" in client.get(f"/owner/style-preview/elegant/{css}").text
    client.post("/owner/styles", data={CSRF_FIELD: _csrf(client), "theme": "elegant"})
    with Session(engine) as session:
        business = session.get(Business, owner)
        jobs = session.exec(select(Job)).all()
    assert business is not None and business.theme == "elegant"
    assert [j.payload for j in jobs] == [{"kind": "draft"}]
    assert (pub.folders(get_settings(), SLUG).draft / "index.html").is_file()


def test_publish_needs_confirmation(client: TestClient, engine: Engine, owner: int) -> None:
    pub.build_draft(engine, get_settings(), owner)
    screen = client.get("/owner/publish", headers={"Accept-Language": "hi"})
    assert (
        "अपनी साइट लाइव करें?" in screen.text
        and "http://asha-test-bakery.localhost:8000/" in screen.text
    )
    assert client.post("/owner/publish", data={}).status_code == 403  # no CSRF token
    assert _status(engine, owner) == "draft"
    client.post("/owner/publish", data={CSRF_FIELD: _csrf(client)})
    assert _status(engine, owner) == "live"
    with Session(engine) as session:
        assert [j.payload for j in session.exec(select(Job))] == [{"kind": "live"}]
    home = client.get("/owner").text
    assert "status-live" in home and "Undo last publish" in home and "Take my site offline" in home
    client.post("/owner/offline", data={CSRF_FIELD: _csrf(client)})
    assert (
        _status(engine, owner) == "offline"
        and "Bring my site back online" in client.get("/owner").text
    )
    client.post("/owner/undo", data={CSRF_FIELD: _csrf(client)})
    assert _status(engine, owner) == "draft"


def test_queue_position_in_her_language(client: TestClient, engine: Engine, owner: int) -> None:
    from app.services.jobs import enqueue

    other = _business(engine, slug="someone-else")[0]
    for _ in range(3):
        enqueue(engine, "score", other, {"kind": "draft"})
    enqueue(engine, "score", owner, {"kind": "draft"})
    assert "You&#39;re 4th in line, about 2 minutes." in client.get("/owner").text
    assert "आप लाइन में 4 नंबर पर हैं, लगभग 2 मिनट।" in client.get("/owner?lang=hi").text


def test_reports_are_hers_only(client: TestClient, engine: Engine, owner: int) -> None:
    other = _business(engine, slug="someone-else")[0]
    with Session(engine) as session:
        mine = TestRun(
            business_id=owner,
            site_hash="x",
            url="u",
            site_kind="draft",
            total=40,
            possible_so_far=50,
        )
        theirs = TestRun(business_id=other, site_hash="x", url="u")
        session.add(mine)
        session.add(theirs)
        session.commit()
        mine_id, theirs_id = mine.id, theirs.id
    report = client.get(f"/owner/score/{mine_id}?lang=mr")
    assert report.status_code == 200 and "40/100" in report.text
    assert "ग्राहकांनी पाहण्यापूर्वी तुमच्या साइटमध्ये काही सुधारणा हव्यात." in report.text
    assert "शक्य" not in report.text  # the detailed "X of Y possible" stays in the dev report
    assert client.get(f"/owner/score/{theirs_id}").status_code == 404
    assert "Score history" in client.get("/owner?lang=en").text


# --- cleanup and timing ---


def test_expired_samples_are_deleted_with_all_data(engine: Engine) -> None:
    settings = get_settings()
    old_sample, _, _ = _business(engine, is_sample=True, created_ago=timedelta(days=7, hours=1))
    seed, _, _ = _business(
        engine, is_sample=True, source="seed", created_ago=timedelta(days=9), slug="seeded-sample"
    )
    fresh, _, _ = _business(
        engine, is_sample=True, created_ago=timedelta(days=6), slug="fresh-sample"
    )
    real, _, _ = _business(engine, created_ago=timedelta(days=9), slug="real-shop")
    pub.build_draft(engine, settings, old_sample)
    with Session(engine) as session:
        session.add(
            ClaudeUsage(business_id=old_sample, purpose="x", model="m", day_ist=utcnow().date())
        )
        session.commit()
    assert pub.purge_expired_samples(engine, settings) == 1
    with Session(engine) as session:
        remaining = {b.id for b in session.exec(select(Business))}
        usage = session.exec(select(ClaudeUsage)).one()
        versions = session.exec(
            select(ProfileVersion).where(ProfileVersion.business_id == old_sample)
        ).all()
    assert remaining == {seed, fresh, real}
    assert usage.business_id is None  # cost kept, link removed
    assert versions == []
    assert not pub.folders(settings, SLUG).root.exists()


def test_time_to_draft_scored_recorded_once(engine: Engine) -> None:
    from app.services.score_runs import _record_time_to_draft_scored

    business_id, _, _ = _business(engine)
    with Session(engine) as session:
        business = session.get(Business, business_id)
        assert business is not None
        _record_time_to_draft_scored(engine, business)
        first = session.exec(select(IntakeSession)).one().seconds_to_draft_scored
    assert first is not None and 590 <= first <= 700
    _record_time_to_draft_scored(engine, business)
    with Session(engine) as session:
        assert session.exec(select(IntakeSession)).one().seconds_to_draft_scored == first


def test_accuracy_report_shows_both_medians(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.accuracy_report import main

    business_id, _, _ = _business(engine)
    with Session(engine) as session:
        intake = session.exec(select(IntakeSession)).one()
        intake.first_guess, intake.draft = {}, {}
        intake.seconds_to_draft_scored, intake.seconds_to_live = 180, 900
        session.add(intake)
        session.commit()
    main()
    out = capsys.readouterr().out
    assert "System time, voice note to draft scored: median 3.0 minutes" in out
    assert "Voice note to live website (includes her decision time): median 15.0 minutes" in out
    assert business_id


def test_job_payload_is_json() -> None:
    assert json.dumps({"kind": "draft"})
