from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.A_memorix.core.runtime.services.background_task_service import MemoryBackgroundTaskService


@pytest.mark.asyncio
@pytest.mark.parametrize("final_error", ["", "vector_unclassified_error", "v2_fingerprint_mismatch"])
async def test_cleanup_waits_without_claiming_and_resumes_after_verification(final_error):
    cleanup = AsyncMock(return_value={"failed": 0})
    reconcile = Mock()
    kernel = SimpleNamespace(
        _background_stopping=False,
        _vector_health={"error_code": "embedding_fingerprint_unavailable"},
        _maintenance_service=SimpleNamespace(_reconcile_relation_graph_projection_jobs=reconcile),
        _delete_admin_service=SimpleNamespace(process_pending_storage_cleanup_jobs=cleanup),
    )
    ticks = 0

    async def sleep(seconds):
        nonlocal ticks
        assert seconds == 10.0
        ticks += 1
        if ticks == 3:
            # 两轮等待不应调用领取任务的入口；图投影维护仍正常进行。
            cleanup.assert_not_awaited()
            assert reconcile.call_count == 2
            kernel._vector_health["error_code"] = final_error
        if ticks == 4:
            kernel._background_stopping = True

    kernel._sleep_background = sleep
    await MemoryBackgroundTaskService(kernel)._storage_cleanup_loop()
    cleanup.assert_awaited_once_with(limit=100)
    assert reconcile.call_count == 3


@pytest.mark.asyncio
async def test_cleanup_can_stop_while_waiting_for_verification():
    cleanup = AsyncMock()
    kernel = SimpleNamespace(
        _background_stopping=False,
        _vector_health={"error_code": "embedding_fingerprint_unavailable"},
        _maintenance_service=SimpleNamespace(_reconcile_relation_graph_projection_jobs=Mock()),
        _delete_admin_service=SimpleNamespace(process_pending_storage_cleanup_jobs=cleanup),
    )
    ticks = 0

    async def sleep(seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            kernel._background_stopping = True

    kernel._sleep_background = sleep
    await MemoryBackgroundTaskService(kernel)._storage_cleanup_loop()
    cleanup.assert_not_awaited()
