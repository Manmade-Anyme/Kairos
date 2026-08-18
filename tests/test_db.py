import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import date, datetime
from kairos.db import SupabaseDB
from kairos.models import SessionConfig, AvailableExpiry, PreviousDayLevels, EnvironmentScore

@pytest.fixture
def mock_supabase(mocker):
    # The client has sync chains (table, select, eq) that end in an async .execute()
    client = MagicMock()
    
    # Setup the builder chain to always return itself
    builder = MagicMock()
    client.table.return_value = builder
    builder.select.return_value = builder
    builder.insert.return_value = builder
    builder.upsert.return_value = builder
    builder.delete.return_value = builder
    builder.eq.return_value = builder
    builder.order.return_value = builder
    builder.limit.return_value = builder
    
    # execute() is the only async method
    execute_mock = AsyncMock()
    builder.execute = execute_mock
    
    # We assign building to client for assertions
    client._builder = builder
    client._execute = execute_mock
    
    mocker.patch("kairos.db.acreate_client", return_value=client)
    return client

@pytest.mark.asyncio
async def test_db_start_stop(mock_supabase):
    db = SupabaseDB()
    await db.start()
    assert db._client is not None
    await db.stop()
    assert db._client is None

@pytest.mark.asyncio
async def test_uninitialized_db():
    db = SupabaseDB()
    with pytest.raises(RuntimeError):
        await db.get_active_session()

@pytest.mark.asyncio
async def test_test_connectivity(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    mock_supabase._execute.return_value = True
    result = await db.test_connectivity()
    assert result is True
    
@pytest.mark.asyncio
async def test_test_connectivity_failure(mock_supabase):
    db = SupabaseDB()
    await db.start()
    mock_supabase._execute.side_effect = Exception("DB Down")
    result = await db.test_connectivity()
    assert result is False

@pytest.mark.asyncio
async def test_get_active_session(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    class FakeResult:
        data = [{"symbol": "NIFTY", "expiry": "2026-03-26", "expiry_type": "WEEKLY", "status": "ACTIVE"}]
        
    mock_supabase._execute.return_value = FakeResult()
    
    sess = await db.get_active_session()
    assert sess is not None
    assert sess.symbol == "NIFTY"
    assert sess.status == "ACTIVE"

@pytest.mark.asyncio
async def test_write_available_expiries(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    e = AvailableExpiry(symbol="NIFTY", expiry=date(2026,3,26), expiry_type="WEEKLY", fetched_at=datetime.now())
    await db.write_available_expiries([e])
    
    # Should be called twice (delete then insert)
    assert mock_supabase._execute.call_count == 2

@pytest.mark.asyncio
async def test_write_previous_day_levels(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    levels = PreviousDayLevels(symbol="NIFTY", trade_date=date(2026,3,25), prev_day_high=22000.0, prev_day_low=21800.0, fetched_at=datetime.now())
    await db.write_previous_day_levels(levels)
    mock_supabase._execute.assert_called_once()

@pytest.mark.asyncio
async def test_get_previous_status(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    class FakeResult:
        data = [{"status": "GO"}]
        
    mock_supabase._execute.return_value = FakeResult()
    
    status = await db.get_previous_status("NIFTY")
    assert status == "GO"

@pytest.mark.asyncio
async def test_db_extra_methods(mock_supabase):
    db = SupabaseDB()
    await db.start()
    
    class FakeResult:
        data = [{"expiry": "2026-03-26", "expiry_type": "WEEKLY", "fetched_at": "2026-03-23T00:00:00"}]
    mock_supabase._execute.return_value = FakeResult()
    
    score = EnvironmentScore(
        timestamp=datetime.now(),
        symbol="NIFTY",
        expiry=date(2026, 3, 26),
        dte=1,
        score=8,
        max_score=8,
        status="GO",
        iv_capped=False,
        conditions=[],
        summary_raw="Raw",
        summary="Summary"
    )
    await db.write_environment_log(score)
    await db.get_previous_day_levels("NIFTY", date(2026, 3, 23))
    await db.get_active_session()
