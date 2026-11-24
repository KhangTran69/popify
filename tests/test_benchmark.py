import pytest
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.benchmark import main, percentile_ms


def test_percentile_ms_uses_nearest_rank_for_small_samples():
    assert percentile_ms([1.0, 2.0, 3.0, 4.0], 0.50) == 2.0
    assert percentile_ms([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0


def test_percentile_ms_rejects_empty_samples_and_invalid_quantiles():
    with pytest.raises(ValueError, match="sample must not be empty"):
        percentile_ms([], 0.5)
    with pytest.raises(ValueError, match="quantile must be between 0 and 1"):
        percentile_ms([1.0], 1.1)


def test_benchmark_default_uses_and_removes_its_temporary_sqlite_database():
    repository = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, STORE_POLICIES="not-json")
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.benchmark", "--count", "2"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    report = json.loads(completed.stdout)
    assert report["database"] == "sqlite"
    assert report["database_scope"] == "temporary database removed at exit"
    assert report["sample_count"] == 2
    assert report["queue_processing"]["processed"] == 2
    assert report["queue_processing"]["items_per_second"] > 0
    assert "Does not measure network latency" in report["measurement_scope"]


def test_benchmark_requires_explicit_confirmation_for_configured_database(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["--use-configured-database"])
    assert raised.value.code == 2
    assert "requires --confirm-disposable-db" in capsys.readouterr().err
