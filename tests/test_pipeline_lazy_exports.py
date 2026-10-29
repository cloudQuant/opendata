"""Regression tests for side-effect-free imports from the pipeline package."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest


def test_lazy_public_exports_use_runner_identity_and_preserve_patch_targets() -> None:
    """Exercise the package's lazy API in-process without ORM initialization."""
    import opendata
    import opendata.pipeline as pipeline

    runner_exports = ("DataPipeline", "PipelineOutcome", "PipelineSpec", "Window")
    missing = object()
    previous_exports = {name: pipeline.__dict__.get(name, missing) for name in runner_exports}
    for name in runner_exports:
        pipeline.__dict__.pop(name, None)
    previous_runner_attribute = pipeline.__dict__.pop("runner", missing)
    previous_models_attribute = opendata.__dict__.get("models", missing)
    module_names = (
        "opendata.models",
        "opendata.models.pipeline",
        "opendata.pipeline.runner",
    )
    previous_modules = {name: sys.modules.get(name, missing) for name in module_names}
    core_module_names = ("opendata.core", "opendata.core.config", "opendata.core.database")
    previous_core_modules = {name: sys.modules.get(name, missing) for name in core_module_names}
    previous_core_package_attribute = opendata.__dict__.get("core", missing)
    previous_core_module = previous_core_modules["opendata.core"]
    previous_core_attributes = (
        {name: previous_core_module.__dict__.get(name, missing) for name in ("config", "database")}
        if previous_core_module is not missing
        else {}
    )
    previous_database_modules = {
        name for name in ("opendata.core.config", "opendata.core.database") if name in sys.modules
    }

    try:
        # Import the real runner against a narrow ORM module stub. This keeps
        # the public classes real while avoiding the database model package's
        # eager Base/settings/engine imports in this process.
        models_package = types.ModuleType("opendata.models")
        models_package.__path__ = []
        pipeline_models = types.ModuleType("opendata.models.pipeline")
        pipeline_models.PipelineProgress = type("PipelineProgress", (), {})
        pipeline_models.ShardStatus = type("ShardStatus", (), {})
        sys.modules["opendata.models"] = models_package
        sys.modules["opendata.models.pipeline"] = pipeline_models
        sys.modules.pop("opendata.pipeline.runner", None)

        from opendata.pipeline import DataPipeline, PipelineOutcome, PipelineSpec, Window, runner

        assert runner is sys.modules["opendata.pipeline.runner"]
        assert set(runner_exports).issubset(pipeline.__all__)
        assert set(runner_exports).issubset(dir(pipeline))
        for name in runner_exports:
            assert getattr(pipeline, name) is getattr(runner, name)
        assert DataPipeline is runner.DataPipeline
        assert PipelineOutcome is runner.PipelineOutcome
        assert PipelineSpec is runner.PipelineSpec
        assert Window is runner.Window

        replacement = object()
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(pipeline, "DataPipeline", replacement)
            assert pipeline.DataPipeline is replacement
        assert pipeline.DataPipeline is runner.DataPipeline

        with pytest.raises(AttributeError, match="not_a_pipeline_export"):
            assert pipeline.not_a_pipeline_export is None

        assert {
            name
            for name in ("opendata.core.config", "opendata.core.database")
            if name in sys.modules
        } == previous_database_modules
    finally:
        for name in runner_exports:
            if previous_exports[name] is missing:
                pipeline.__dict__.pop(name, None)
            else:
                pipeline.__dict__[name] = previous_exports[name]
        if previous_runner_attribute is missing:
            pipeline.__dict__.pop("runner", None)
        else:
            pipeline.__dict__["runner"] = previous_runner_attribute
        if previous_models_attribute is missing:
            opendata.__dict__.pop("models", None)
        else:
            opendata.__dict__["models"] = previous_models_attribute
        for name in module_names:
            if previous_modules[name] is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_modules[name]
        for name in core_module_names:
            if previous_core_modules[name] is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_core_modules[name]
        if previous_core_module is not missing:
            for name, previous_attribute in previous_core_attributes.items():
                if previous_attribute is missing:
                    previous_core_module.__dict__.pop(name, None)
                else:
                    previous_core_module.__dict__[name] = previous_attribute
        if previous_core_package_attribute is missing:
            opendata.__dict__.pop("core", None)
        else:
            opendata.__dict__["core"] = previous_core_package_attribute


def test_pipeline_exports_load_runner_on_demand_without_database_setup(tmp_path: Path) -> None:
    """Keep DDL imports light while retaining lazy runner API compatibility."""
    project_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import sys
        import types
        from unittest.mock import patch

        import opendata.pipeline as pipeline

        runner_exports = ("DataPipeline", "PipelineOutcome", "PipelineSpec", "Window")
        assert "opendata.pipeline.runner" not in sys.modules
        assert "opendata.core.config" not in sys.modules
        assert "opendata.core.database" not in sys.modules
        assert set(runner_exports).issubset(pipeline.__all__)
        assert set(runner_exports).issubset(dir(pipeline))
        assert all(name not in pipeline.__dict__ for name in runner_exports)

        # The real runner imports ORM models. Supply only those type names so
        # the compatibility checks do not initialize the application's DB.
        models_package = types.ModuleType("opendata.models")
        models_package.__path__ = []
        pipeline_models = types.ModuleType("opendata.models.pipeline")
        pipeline_models.PipelineProgress = type("PipelineProgress", (), {})
        pipeline_models.ShardStatus = type("ShardStatus", (), {})
        sys.modules["opendata.models"] = models_package
        sys.modules["opendata.models.pipeline"] = pipeline_models

        from opendata.pipeline import runner
        from opendata.pipeline import DataPipeline, PipelineOutcome, PipelineSpec, Window

        assert runner is sys.modules["opendata.pipeline.runner"]
        for name in runner_exports:
            assert getattr(pipeline, name) is getattr(runner, name)
        assert DataPipeline is runner.DataPipeline
        assert PipelineOutcome is runner.PipelineOutcome
        assert PipelineSpec is runner.PipelineSpec
        assert Window is runner.Window
        assert all(name in pipeline.__dict__ for name in runner_exports)
        assert pipeline.Column.__module__ == "opendata.pipeline.ddl"

        replacement = object()
        with patch("opendata.pipeline.DataPipeline", replacement):
            assert pipeline.DataPipeline is replacement
        assert pipeline.DataPipeline is runner.DataPipeline

        try:
            pipeline.not_a_pipeline_export
        except AttributeError:
            pass
        else:
            raise AssertionError("unknown pipeline package attributes must raise AttributeError")
        """
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root),
        "PYTHONHASHSEED": "0",
        "APP_ENV": "testing",
        "TESTING": "true",
        "REDIS_URL": "",
        "SENTRY_DSN": "",
    }

    result = subprocess.run(  # noqa: S603 - fixed interpreter runs a literal isolated probe
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
