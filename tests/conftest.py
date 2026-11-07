import pytest


@pytest.fixture(autouse=True)
def private_index(tmp_path, monkeypatch):
    """Every test gets its own index database, never the real one in ~/Library/Caches."""
    monkeypatch.setenv("HARNESS_WATCH_DB", str(tmp_path / "index.sqlite3"))
