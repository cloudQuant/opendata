"""Regression tests for opt-in operational settings in Docker Compose."""

from pathlib import Path

import yaml


def test_backend_compose_wires_patrol_and_cache_settings_into_the_data_volume():
    """Keep operator toggles available to the container with mounted paths."""
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    backend = compose["services"]["backend"]
    environment = backend["environment"]

    assert environment["PATROL_ENABLED"] == "${PATROL_ENABLED:-false}"
    assert environment["PATROL_FAILURE_ALERT_THRESHOLD"] == ("${PATROL_FAILURE_ALERT_THRESHOLD:-2}")
    assert environment["RAW_RESPONSE_CACHE_ENABLED"] == "${RAW_RESPONSE_CACHE_ENABLED:-false}"
    assert environment["CACHE_TTL_SECONDS"] == "${CACHE_TTL_SECONDS:-900}"
    assert environment["DATA_DIR"] == "/opendata/data"
    assert environment["CACHE_DIR"] == "/opendata/data/cache"
    assert "./data:/opendata/data" in backend["volumes"]


def test_paired_backup_is_opt_in_and_mounts_minute_archive_for_locking():
    """Keep paired snapshots in the optional backup profile and shared data root."""
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    backup = compose["services"]["backup"]

    assert backup["profiles"] == ["backup"]
    assert backup["environment"]["BACKUP_MINUTE_ARCHIVE_DIR"] == "/data/minute_archive"
    assert backup["environment"]["BACKUP_PYTHON"] == "python3"
    assert "./data:/data" in backup["volumes"]
    assert "BACKUP_MINUTE_ARCHIVE_DIR" not in compose["services"]["backend"]["environment"]
