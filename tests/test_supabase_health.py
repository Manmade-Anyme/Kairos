"""Regression coverage for a session bridge outage before scoring starts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kairos.scheduler as scheduler
from kairos.db import SupabaseDB


@pytest.fixture
def bridge(monkeypatch):
    database = SupabaseDB()
    client = MagicMock()
    query = client.table.return_value
    for method in ("select", "eq", "order", "limit"):
        getattr(query, method).return_value = query
    query.execute = AsyncMock(return_value=SimpleNamespace(data=[]))
    database._client = client
    notifications = AsyncMock()
    notifications.post_critical_alert.return_value = True
    notifications.post_supabase_recovered.return_value = True
    monkeypatch.setattr(scheduler, "db", database)
    monkeypatch.setattr(scheduler, "state", scheduler.SessionState())
    monkeypatch.setattr(scheduler, "notifier", notifications)
    return database, query, notifications


async def test_session_read_failure_is_distinct_from_inactive_session(bridge):
    database, query, _ = bridge
    query.execute.side_effect = RuntimeError("PGRST205: missing session table")
    with pytest.raises(RuntimeError, match="Supabase"):
        await database.get_active_session()


@pytest.mark.parametrize("job", [scheduler.run_cycle, scheduler.run_heartbeat])
async def test_session_read_failure_warns_before_first_successful_cycle(bridge, job):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("PGRST205: missing session table")
    await job()
    notifications.post_critical_alert.assert_awaited_once()


async def test_two_jobs_do_not_repeat_an_outage_warning(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("database unavailable")
    await scheduler.run_cycle()
    await scheduler.run_heartbeat()
    notifications.post_critical_alert.assert_awaited_once()


async def test_concurrent_jobs_do_not_duplicate_outage_notification(bridge):
    _, query, notifications = bridge

    async def unavailable():
        await asyncio.sleep(0)
        raise RuntimeError("database unavailable")

    async def delivered(**kwargs):
        await asyncio.sleep(0)
        return True

    query.execute.side_effect = unavailable
    notifications.post_critical_alert.side_effect = delivered
    await asyncio.gather(scheduler.run_cycle(), scheduler.run_heartbeat())
    notifications.post_critical_alert.assert_awaited_once()


async def test_bridge_recovery_notifies_even_without_active_session(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("database unavailable")
    await scheduler.run_cycle()
    query.execute.side_effect = None
    await scheduler.run_heartbeat()
    await scheduler.run_cycle()
    notifications.post_supabase_recovered.assert_awaited_once()


async def test_failed_warning_delivery_is_retried(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("database unavailable")
    notifications.post_critical_alert.side_effect = [False, True]
    await scheduler.run_cycle()
    await scheduler.run_heartbeat()
    assert notifications.post_critical_alert.await_count == 2


async def test_failed_recovery_delivery_is_retried(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("database unavailable")
    await scheduler.run_cycle()
    query.execute.side_effect = None
    notifications.post_supabase_recovered.side_effect = [False, True]
    await scheduler.run_heartbeat()
    await scheduler.run_cycle()
    assert notifications.post_supabase_recovered.await_count == 2


async def test_new_outage_warns_when_previous_recovery_message_failed(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("first outage")
    await scheduler.run_cycle()
    query.execute.side_effect = None
    notifications.post_supabase_recovered.return_value = False
    await scheduler.run_heartbeat()
    query.execute.side_effect = RuntimeError("second outage")
    await scheduler.run_cycle()
    assert notifications.post_critical_alert.await_count == 2


async def test_inactive_session_does_not_send_health_messages(bridge):
    _, _, notifications = bridge
    await scheduler.run_cycle()
    await scheduler.run_heartbeat()
    assert notifications.mock_calls == []


async def test_raw_database_error_is_not_sent_to_discord(bridge):
    _, query, notifications = bridge
    query.execute.side_effect = RuntimeError("sensitive connection details")
    await scheduler.run_cycle()
    assert "sensitive connection details" not in str(notifications.post_critical_alert.call_args)
