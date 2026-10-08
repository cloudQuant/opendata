"""Package contract tests for the independently installable SDK."""

from __future__ import annotations

import builtins
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = REPO_ROOT / "opendata_client"
RUN_INSTALL_TEST = os.environ.get("OPENDATA_CLIENT_RUN_INSTALL_TEST") == "1"


def test_sdk_metadata_declares_runtime_and_optional_dependencies() -> None:
    """The REST install declares its imports and isolates optional features."""
    with (SDK_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)["project"]

    assert set(project["dependencies"]) == {"httpx>=0.27", "loguru>=0.7"}
    extras = project["optional-dependencies"]
    assert extras["frames"] == ["pandas>=2.0"]
    assert extras["ws"] == ["websockets>=11.0"]
    assert project["readme"] == "README.md"


def test_rest_remains_available_when_optional_imports_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing pandas and websockets affect only their requested features."""
    from opendata_client import OpendataClient, OpendataClientError

    real_import = builtins.__import__

    def import_without_extras(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pandas" or name.startswith("websockets"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_extras)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "rows": [],
                    "columns": ["trade_date", "symbol"],
                    "page": 1,
                    "page_size": 500,
                    "count": 0,
                },
            },
        )

    with OpendataClient(
        "https://api.example.com",
        api_key="od-test",
        transport=httpx.MockTransport(handler),
    ) as client:
        page = client.stock_daily("600519")
        assert page.columns == ["trade_date", "symbol"]
        with pytest.raises(ImportError, match=r"opendata-client\[frames\]"):
            page.to_dataframe()
        with pytest.raises(OpendataClientError, match=r"opendata-client\[ws\]"):
            client.subscribe("stock_daily").__enter__()

    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/data/equity/stock_daily"


def test_dataframe_conversions_keep_service_and_row_column_order() -> None:
    """Frame conversion preserves REST columns, including an empty page."""
    pandas = pytest.importorskip("pandas")
    from opendata_client import DataUpdate, Page

    page = Page(
        rows=[{"symbol": "600519", "trade_date": "2024-01-02"}],
        columns=["trade_date", "symbol"],
    )
    assert list(page.to_dataframe().columns) == ["trade_date", "symbol"]

    empty_page = Page(rows=[], columns=["trade_date", "symbol"])
    assert list(empty_page.to_dataframe().columns) == ["trade_date", "symbol"]

    update = DataUpdate(
        domain="stock_daily",
        source="akshare",
        layer="dwd",
        batch_id="b-1",
        window={"start": "2024-01-02", "end": "2024-01-02"},
        rows=2,
        created_at="2024-01-02T00:00:00Z",
        data=(
            {"symbol": "600519"},
            {"symbol": "000001", "note": "restated"},
        ),
    )
    update_frame = update.to_dataframe()
    assert list(update_frame.columns) == ["symbol", "note"]
    assert pandas.isna(update_frame.loc[0, "note"])
    assert update_frame.loc[1, "note"] == "restated"


def test_websocket_handshake_uses_the_sync_client_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ws extra's synchronous connect context can perform the handshake."""
    ws_client = pytest.importorskip("websockets.sync.client")
    from opendata_client import OpendataClient

    class FakeSocket:
        def __init__(self) -> None:
            self.messages = iter(
                [
                    json.dumps({"type": "auth.ok"}),
                    json.dumps({"type": "subscribe.ok", "replayed": 1}),
                    json.dumps(
                        {
                            "type": "data.update",
                            "domain": "stock_daily",
                            "source": "akshare",
                            "layer": "dwd",
                            "batch_id": "b-1",
                            "window": {"start": "2024-01-02", "end": "2024-01-02"},
                            "rows": 1,
                            "created_at": "2024-01-02T00:00:00Z",
                        }
                    ),
                ]
            )
            self.sent: list[dict[str, object]] = []
            self.closed = False

        def __enter__(self) -> FakeSocket:
            return self

        def __iter__(self) -> FakeSocket:
            return self

        def __next__(self) -> str:
            return next(self.messages)

        def send(self, message: str) -> None:
            self.sent.append(json.loads(message))

        def close(self) -> None:
            self.closed = True

    socket = FakeSocket()
    connect_calls: list[tuple[str, float | None]] = []

    def fake_connect(url: str, *, open_timeout: float | None) -> FakeSocket:
        connect_calls.append((url, open_timeout))
        return socket

    monkeypatch.setattr(ws_client, "connect", fake_connect)

    with (
        OpendataClient("https://api.example.com", api_key="od-test") as client,
        client.subscribe("stock_daily", timeout=3) as session,
    ):
        assert session.acknowledged
        assert session.replayed == 1
        update = next(iter(session))

    assert connect_calls == [("wss://api.example.com/ws/data/subscribe", 3)]
    assert socket.sent == [
        {"action": "auth", "token": "od-test"},
        {
            "action": "subscribe",
            "domain": "stock_daily",
            "layer": "dwd",
            "payload": "meta",
            "symbols": [],
            "since_batch_id": None,
        },
    ]
    assert update.batch_id == "b-1"
    assert socket.closed


@pytest.mark.skipif(not RUN_INSTALL_TEST, reason="set OPENDATA_CLIENT_RUN_INSTALL_TEST=1")
def test_wheel_installs_without_checkout_or_system_site(tmp_path: Path) -> None:
    """Build and exercise minimal/all-extra wheels in isolated target installs."""
    requested_evidence = os.environ.get("OPENDATA_CLIENT_INSTALL_EVIDENCE_DIR")
    evidence_root = (
        Path(requested_evidence).resolve() if requested_evidence else tmp_path / "evidence"
    )
    try:
        evidence_root.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise AssertionError("installation evidence must be outside the repository")
    evidence_root.mkdir(parents=True, exist_ok=False)

    wheel_dir = evidence_root / "wheel"
    wheel_dir.mkdir()
    build_input = evidence_root / "build-input"
    build_input.mkdir()
    shutil.copy2(SDK_ROOT / "pyproject.toml", build_input / "pyproject.toml")
    shutil.copy2(SDK_ROOT / "README.md", build_input / "README.md")
    shutil.copytree(
        SDK_ROOT / "opendata_client",
        build_input / "opendata_client",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "build",
            "dist",
            "*.egg-info",
            ".git",
            ".DS_Store",
        ),
    )
    assert build_input.is_relative_to(evidence_root)
    try:
        build_input.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise AssertionError("wheel build input must be outside the repository")

    build = subprocess.run(  # noqa: S603  # runs the fixed base-Python pip command
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(build_input),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=evidence_root,
        check=True,
        capture_output=True,
        text=True,
    )
    (evidence_root / "build.log").write_text(build.stdout + build.stderr, encoding="utf-8")
    wheels = list(wheel_dir.glob("opendata_client-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

    minimal_target = evidence_root / "minimal"
    all_target = evidence_root / "all"
    outside_cwd = evidence_root / "consumer-cwd"
    outside_cwd.mkdir()
    base_env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"):
        base_env.pop(name, None)
    base_env["PYTHONNOUSERSITE"] = "1"

    def install(target: Path, requirement: str, log_name: str) -> None:
        result = subprocess.run(  # noqa: S603  # runs the fixed base-Python pip command
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--ignore-installed",
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "--target",
                str(target),
                requirement,
            ],
            cwd=outside_cwd,
            env=base_env,
            capture_output=True,
            text=True,
        )
        (evidence_root / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr

    def smoke(target: Path, script: str, log_name: str) -> None:
        env = base_env.copy()
        env["PYTHONPATH"] = str(target)
        result = subprocess.run(  # noqa: S603  # runs a fixed Python smoke script
            [sys.executable, "-S", "-c", script],
            cwd=outside_cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        (evidence_root / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr

    pip_distribution = importlib.metadata.distribution("pip")
    pip_package = Path(pip_distribution.locate_file("pip")).resolve()
    pip_dist_info = next(pip_package.parent.glob("pip-*.dist-info"))

    def pip_check(target: Path, log_name: str) -> None:
        # Put pip itself in the target so -S can check only this install and
        # Python's standard library, without seeing the base environment.
        shutil.copytree(pip_package, target / "pip")
        shutil.copytree(pip_dist_info, target / pip_dist_info.name)
        env = base_env.copy()
        env["PYTHONPATH"] = str(target)
        result = subprocess.run(
            [sys.executable, "-S", "-m", "pip", "check"],
            cwd=outside_cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        (evidence_root / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr

    install(minimal_target, str(wheel), "install-minimal.log")
    pip_check(minimal_target, "pip-check-minimal.log")
    smoke(
        minimal_target,
        """
import importlib.util
import sys

import httpx
from opendata_client import OpendataClient, OpendataClientError

assert not any(
    'site-packages' in path or 'dist-packages' in path for path in sys.path
)
assert importlib.util.find_spec('pandas') is None
assert importlib.util.find_spec('websockets') is None
seen = []

def handler(request):
    seen.append(request)
    return httpx.Response(
        200,
        json={
            'success': True,
            'data': {
                'rows': [],
                'columns': ['trade_date', 'symbol'],
                'page': 1,
                'page_size': 500,
                'count': 0,
            },
        },
    )

with OpendataClient(
    'https://api.example.com',
    api_key='od-test',
    transport=httpx.MockTransport(handler),
) as client:
    page = client.stock_daily(
        ['600519'], start='2024-01-01', fields=('close', 'open')
    )
    assert page.columns == ['trade_date', 'symbol']
    assert seen[0].url.path == '/api/v1/data/equity/stock_daily'
    assert dict(seen[0].url.params)['symbols'] == '600519'
    assert dict(seen[0].url.params)['fields'] == 'close,open'
    try:
        page.to_dataframe()
    except ImportError as exc:
        assert "opendata-client[frames]" in str(exc)
    else:
        raise AssertionError('minimal install unexpectedly has pandas')
    try:
        client.subscribe('stock_daily').__enter__()
    except OpendataClientError as exc:
        assert "opendata-client[ws]" in str(exc)
    else:
        raise AssertionError('minimal install unexpectedly has websockets')
print('minimal isolated REST and optional-dependency checks passed')
""",
        "smoke-minimal.log",
    )

    install(all_target, f"{wheel}[frames,ws]", "install-all.log")
    pip_check(all_target, "pip-check-all.log")
    smoke(
        all_target,
        """
import importlib.util
import json
import sys

from opendata_client import DataUpdate, OpendataClient, Page

assert not any(
    'site-packages' in path or 'dist-packages' in path for path in sys.path
)
assert importlib.util.find_spec('pandas') is not None
assert importlib.util.find_spec('websockets') is not None
columns = Page([], columns=['trade_date', 'symbol']).to_dataframe().columns
assert list(columns) == ['trade_date', 'symbol']
update = DataUpdate(
    'stock_daily',
    'akshare',
    'dwd',
    'b-1',
    {'start': None, 'end': None},
    1,
    '',
    data=({'symbol': '600519', 'close': 1.0},),
)
assert list(update.to_dataframe().columns) == ['symbol', 'close']

from websockets.sync import client as ws_client

class FakeSocket:
    def __init__(self):
        self.messages = iter(
            [
                json.dumps({'type': 'auth.ok'}),
                json.dumps({'type': 'subscribe.ok'}),
                json.dumps(
                    {
                        'type': 'data.update',
                        'domain': 'stock_daily',
                        'source': 'akshare',
                        'layer': 'dwd',
                        'batch_id': 'b-1',
                        'window': {},
                        'rows': 0,
                        'created_at': '',
                    }
                ),
            ]
        )
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.messages)

    def send(self, value):
        self.sent.append(json.loads(value))

    def close(self):
        self.closed = True

socket = FakeSocket()
ws_client.connect = lambda url, *, open_timeout: socket
with OpendataClient('wss://api.example.com', api_key='od-test') as client:
    with client.subscribe('stock_daily') as session:
        update = next(iter(session))
assert update.batch_id == 'b-1' and socket.closed
assert [frame['action'] for frame in socket.sent] == ['auth', 'subscribe']
print('all-extra isolated frame and WebSocket checks passed')
""",
        "smoke-all.log",
    )

    summary = (
        f"wheel={wheel}\nsha256={digest}\n"
        f"build_input={build_input}\n"
        f"minimal_target={minimal_target}\nall_target={all_target}\n"
        f"consumer_cwd={outside_cwd}\n"
        "minimal_pip_check=PASS\nminimal=PASS\n"
        "all_extras_pip_check=PASS\nall_extras=PASS\n"
    )
    (evidence_root / "evidence.txt").write_text(summary, encoding="utf-8")
    print(summary, end="")
