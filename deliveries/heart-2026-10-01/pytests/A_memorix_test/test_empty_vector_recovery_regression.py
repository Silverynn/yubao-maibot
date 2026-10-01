from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from src.A_memorix.core.runtime.sdk_memory_kernel import SDKMemoryKernel
from src.A_memorix.core.runtime import sdk_memory_kernel as kernel_module
from src.A_memorix.core.storage import MetadataStore, VectorStore, VectorStoreIntegrityError
from src.A_memorix.core.storage.vector_store import HAS_FAISS
from src.A_memorix.core.utils.summary_importer import SummaryImporter


def _kernel(tmp_path: Path, request) -> SDKMemoryKernel:
    kernel = SDKMemoryKernel(
        plugin_root=tmp_path,
        config={
            "storage": {"data_dir": str(tmp_path / "memory")},
            "embedding": {"dimension": 2},
            "retrieval": {"vector_pools": {"mode": "dual"}},
        },
    )
    kernel.embedding_manager = SimpleNamespace(
        get_embedding_fingerprint=lambda **kwargs: {"hash": "observed-model", "source": "observed"},
    )
    kernel.metadata_store = MetadataStore(data_dir=kernel.data_dir / "metadata")
    kernel.metadata_store.connect()
    request.addfinalizer(kernel.metadata_store.close)
    return kernel


@pytest.mark.skipif(not HAS_FAISS, reason="Faiss 未安装")
@pytest.mark.parametrize("existing_empty_metadata", [False, True])
def test_verified_empty_storage_creates_reloadable_dual_pools(tmp_path, existing_empty_metadata, request):
    kernel = _kernel(tmp_path, request)
    root = kernel._vectors_root()
    if existing_empty_metadata:
        VectorStore(dimension=2, data_dir=root).save(
            embedding_fingerprint={"hash": "configured-model", "source": "configured"},
        )
    original = (root / "vectors_metadata.json").read_bytes() if existing_empty_metadata else None

    assert kernel._embedding_state_service._initialize_verified_empty_vector_pools() is True
    assert kernel._dual_vector_pools_enabled() is True
    assert kernel._reload_dual_vector_stores_from_disk() is True
    assert kernel.paragraph_vector_store.num_vectors == 0
    assert kernel.graph_vector_store.num_vectors == 0
    if original is not None:
        assert (root / "vectors_metadata.json").read_bytes() == original


@pytest.mark.skipif(not HAS_FAISS, reason="Faiss 未安装")
@pytest.mark.parametrize("pool", ["", "paragraph", "graph"])
def test_nonempty_storage_is_never_overwritten_by_empty_initialization(tmp_path, pool, request):
    kernel = _kernel(tmp_path, request)
    directory = kernel._vectors_root() / pool
    store = VectorStore(dimension=2, data_dir=directory)
    store.add(np.asarray([[1.0, 0.0]], dtype=np.float32), ["existing"])
    store.save()
    original = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    assert kernel._embedding_state_service._initialize_verified_empty_vector_pools() is False
    assert not kernel._dual_vector_ready_manifest_path().exists()
    for name, content in original.items():
        assert (directory / name).read_bytes() == content


@pytest.mark.skipif(not HAS_FAISS, reason="Faiss 未安装")
def test_corrupt_empty_metadata_is_not_replaced(tmp_path, request):
    kernel = _kernel(tmp_path, request)
    root = kernel._vectors_root()
    root.mkdir(parents=True)
    metadata = root / "vectors_metadata.json"
    metadata.write_text("{broken", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        kernel._embedding_state_service._initialize_verified_empty_vector_pools()
    assert metadata.read_text(encoding="utf-8") == "{broken"
    assert not kernel._dual_vector_ready_manifest_path().exists()


def test_summary_persists_current_runtime_even_with_stale_none_reference():
    persist = Mock()
    importer = SummaryImporter(None, None, None, None, {
        "plugin_instance": SimpleNamespace(persist_memory=persist),
    })
    importer._persist_import()
    persist.assert_called_once_with()


def test_standalone_summary_persists_graph_when_vectors_are_degraded():
    graph = Mock()
    importer = SummaryImporter(None, graph, None, None, {
        "retrieval": {"vector_pools": {"mode": "single"}},
    })
    importer._persist_import()
    graph.save.assert_called_once_with()


def test_standalone_summary_saves_both_active_vector_pools():
    paragraph, graph_vectors, graph, legacy = Mock(), Mock(), Mock(), Mock()
    importer = SummaryImporter(legacy, graph, None, None, {
        "retrieval": {"vector_pools": {"mode": "dual"}},
        "paragraph_vector_store": paragraph,
        "graph_vector_store": graph_vectors,
    })
    importer._persist_import()
    paragraph.save.assert_called_once_with()
    graph_vectors.save.assert_called_once_with()
    graph.save.assert_called_once_with()
    legacy.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_FAISS, reason="Faiss 未安装")
async def test_embedding_recovery_reenables_empty_storage_and_persists_new_vectors(tmp_path, monkeypatch, request):
    kernel = _kernel(tmp_path, request)
    VectorStore(dimension=2, data_dir=kernel._vectors_root()).save(
        embedding_fingerprint={"hash": "configured-model", "source": "configured"},
    )
    kernel._disable_vector_channel(VectorStoreIntegrityError(
        "等待真实请求", error_code="embedding_fingerprint_unavailable",
    ))
    monkeypatch.setattr(kernel_module, "build_search_runtime", lambda **kwargs: SimpleNamespace(
        ready=True, retriever=object(), threshold_filter=None, sparse_index=None,
    ))
    monkeypatch.setattr(kernel, "_refresh_relation_write_service", Mock())
    monkeypatch.setattr(kernel, "_refresh_runtime_dependents", Mock())
    monkeypatch.setattr(kernel, "_apply_runtime_sparse_mode", Mock())

    assert await kernel._embedding_state_service._restore_vector_channel_after_embedding_recovery() is True
    assert kernel._runtime_capabilities["vector_write"] is True
    assert kernel._vector_health["state"] == "healthy"
    kernel.paragraph_vector_store.add(np.asarray([[1.0, 0.0]], dtype=np.float32), ["new-summary"])
    kernel._runtime_facade.persist_memory()
    assert kernel._reload_dual_vector_stores_from_disk() is True
    assert "new-summary" in kernel.paragraph_vector_store
