import pytest
import respx
import httpx
from datetime import datetime, date
from kairos.notifier import Notifier
from kairos.models import EnvironmentScore, ConditionResult, SessionConfig
from kairos.config import settings

@pytest.fixture
def dummy_score():
    return EnvironmentScore(
        timestamp=datetime.now(),
        symbol="NIFTY",
        expiry=date(2026, 3, 26),
        dte=1,
        score=8,
        max_score=8,
        status="GO",
        iv_capped=False,
        conditions=[],
        summary_raw="Raw Summary",
        summary="Test Summary"
    )

@pytest.fixture
def dummy_session():
    return SessionConfig(
        symbol="NIFTY",
        expiry=date(2026,3,26),
        expiry_type="WEEKLY",
        status="ACTIVE"
    )

@pytest.mark.asyncio
async def test_notifier_start_stop():
    notifier = Notifier()
    await notifier.start()
    assert notifier._client is not None
    await notifier.stop()
    assert notifier._client.is_closed

@pytest.mark.asyncio
@respx.mock
async def test_post_environment_alert(dummy_score):
    route = respx.post(settings.discord_webhook_url).respond(status_code=204)
    notifier = Notifier()
    await notifier.start()
    
    # dummy_score has iv_capped=False, score=8
    await notifier.post_environment_alert(dummy_score)
    await notifier.stop()

    # Verify payload — no cap note
    call = route.calls.last
    content = call.request.content.decode()
    assert "⚠️ IV Cap Active" not in content
    assert "⚠️ Capped at CAUTION" not in content
    assert "⚠️ IV contracting" not in content

@pytest.mark.asyncio
@respx.mock
async def test_post_startup(dummy_session):
    respx.post(settings.discord_health_webhook_url).respond(status_code=204)
    notifier = Notifier()
    await notifier.start()
    
    await notifier.post_startup(
        symbol="NIFTY",
        dhan_ok=True,
        supabase_ok=True,
        pdh=22000.0,
        pdl=21800.0,
        expiry_str="26 Mar 2026 (Weekly)"
    )
    await notifier.stop()

@pytest.mark.asyncio
@respx.mock
async def test_post_environment_alert_failure_does_not_raise(dummy_score):
    respx.post(settings.discord_webhook_url).respond(status_code=500)
    notifier = Notifier()
    await notifier.start()
    
    # Notifier uses safe async logging gracefully absorbing 500 exceptions
    await notifier.post_environment_alert(dummy_score)
    
    await notifier.stop()

@pytest.mark.asyncio
@respx.mock
async def test_all_notifier_methods():
    respx.post(settings.discord_webhook_url).respond(status_code=204)
    respx.post(settings.discord_health_webhook_url).respond(status_code=204)
    notifier = Notifier()
    await notifier.start()
    
    await notifier.post_warmup_complete("NIFTY")
    await notifier.post_heartbeat(datetime.now(), 5, "NIFTY", "26 Mar 2026", True)
    await notifier.post_api_warning(3, "Timeout")
    await notifier.post_critical_alert("Error", "Detail", datetime.now(), "Hint")
    await notifier.post_stale_signal_warning(datetime.now(), "NIFTY")
    await notifier.post_stopped("NIFTY")
    await notifier.post_session_boundary(True, "Morning")
    
    await notifier.stop()


@pytest.mark.asyncio
@respx.mock
async def test_iv_cap_message_suppressed_at_low_score(dummy_score):
    # iv_capped=True but score=3 — cap had no effect, header badge should not appear
    route = respx.post(settings.discord_webhook_url).respond(status_code=204)
    capped_low_score = dummy_score.model_copy()
    capped_low_score.iv_capped = True
    capped_low_score.score = 3
    capped_low_score.status = "AVOID"
    
    notifier = Notifier()
    await notifier.start()
    await notifier.post_environment_alert(capped_low_score)
    await notifier.stop()
    
    # Verify payload
    call = route.calls.last
    content = call.request.content.decode()
    assert "⚠️ IV Cap Active" not in content
    assert "⚠️ IV contracting — premium at risk" in content
    assert "⚠️ Capped at CAUTION" not in content


@pytest.mark.asyncio
@respx.mock
async def test_iv_cap_message_shown_at_high_score(dummy_score):
    # iv_capped=True and score=8 — cap has effect, header badge should appear
    route = respx.post(settings.discord_webhook_url).respond(status_code=204)
    capped_high_score = dummy_score.model_copy()
    capped_high_score.iv_capped = True
    capped_high_score.score = 8
    capped_high_score.status = "CAUTION"
    
    notifier = Notifier()
    await notifier.start()
    await notifier.post_environment_alert(capped_high_score)
    await notifier.stop()
    
    # Verify payload
    call = route.calls.last
    content = call.request.content.decode()
    assert "⚠️ IV Cap Active" in content
    assert "⚠️ Capped at CAUTION — IV contracting" in content
    assert "⚠️ IV contracting — premium at risk" not in content


@pytest.mark.asyncio
@respx.mock
async def test_iv_cap_at_boundary_score(dummy_score):
    # iv_capped=True and score=7 — exactly at boundary, should show cap note
    route = respx.post(settings.discord_webhook_url).respond(status_code=204)
    capped_score = dummy_score.model_copy()
    capped_score.iv_capped = True
    capped_score.score = 7
    capped_score.status = "CAUTION"
    
    notifier = Notifier()
    await notifier.start()
    await notifier.post_environment_alert(capped_score)
    await notifier.stop()
    
    # Verify payload
    call = route.calls.last
    content = call.request.content.decode()
    assert "⚠️ IV Cap Active" in content
    assert "⚠️ Capped at CAUTION — IV contracting" in content


@pytest.mark.asyncio
@respx.mock
async def test_iv_cap_just_below_boundary_score(dummy_score):
    # iv_capped=True and score=6 — just below boundary, should be suppressed
    route = respx.post(settings.discord_webhook_url).respond(status_code=204)
    capped_score = dummy_score.model_copy()
    capped_score.iv_capped = True
    capped_score.score = 6
    capped_score.status = "CAUTION"
    
    notifier = Notifier()
    await notifier.start()
    await notifier.post_environment_alert(capped_score)
    await notifier.stop()
    
    # Verify payload
    call = route.calls.last
    content = call.request.content.decode()
    assert "⚠️ IV Cap Active" not in content
    assert "⚠️ IV contracting — premium at risk" in content
    assert "⚠️ Capped at CAUTION" not in content
