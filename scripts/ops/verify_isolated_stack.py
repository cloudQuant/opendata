"""Verify the isolated C65 application stack behind an explicit apply gate."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import shutil

# subprocess is limited to the fixed, shell-free argv calls below.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ENDPOINT_HOST = "127.0.0.1"
ENDPOINT_PORT = 33566
ENDPOINT = f"http://{ENDPOINT_HOST}:{ENDPOINT_PORT}"
APP_CONTAINER = "opendata-iteration01-c65-app"
APP_IMAGE_TAG = "opendata-iteration01:c65-current"
DB_HOSTS = frozenset({"host.docker.internal", "opendata-iteration01-c65"})
DB_PORT = "33565"
DB_SCHEMAS = {"MYSQL_DATABASE": "opendata", "DATA_MYSQL_DATABASE": "opendata_data"}
PUBLISHED_PORT = "8000/tcp"
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = REPO_ROOT / "frontend"
PLAYWRIGHT_PACKAGE = FRONTEND_ROOT / "node_modules" / "playwright"
HTTP_TIMEOUT_SECONDS = 20
HTTP_BODY_LIMIT = 16 * 1024 * 1024
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(RuntimeError):
    """A safe, reportable verification failure without request data."""

    def __init__(self, error_type: str) -> None:
        """Store only a fixed classification, never a request or subprocess message."""
        super().__init__(error_type)
        self.error_type = error_type


@dataclass(frozen=True)
class ContainerIdentity:
    """The non-secret identity fields accepted from Docker inspect."""

    image_id: str
    source_identity: str


class _AssetCollector(HTMLParser):
    """Collect only module-script and stylesheet references from the home page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.module_scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.has_app_mount = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and values.get("id") == "app":
            self.has_app_mount = True
        if tag == "script" and values.get("type") == "module":
            script = values.get("src")
            if script:
                self.module_scripts.append(script)
        if tag == "link" and "stylesheet" in (values.get("rel") or "").split():
            stylesheet = values.get("href")
            if stylesheet:
                self.stylesheets.append(stylesheet)


def validate_docker_inspect(document: object) -> ContainerIdentity:
    """Fail closed unless one Docker inspect record proves the owned stack target."""
    if not isinstance(document, list) or len(document) != 1:
        raise VerificationError("container_inspect_shape_invalid")
    container = document[0]
    if not isinstance(container, dict):
        raise VerificationError("container_inspect_shape_invalid")
    if container.get("Name") != f"/{APP_CONTAINER}":
        raise VerificationError("container_name_not_owned")

    image_id = container.get("Image")
    if not isinstance(image_id, str) or IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise VerificationError("image_id_invalid")

    config = container.get("Config")
    if not isinstance(config, dict) or config.get("Image") != APP_IMAGE_TAG:
        raise VerificationError("image_reference_not_owned")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or labels.get("codex.task") != "opendata-c65":
        raise VerificationError("task_label_not_owned")
    source_identity = labels.get("codex.source")
    if not isinstance(source_identity, str) or SOURCE_ID_PATTERN.fullmatch(source_identity) is None:
        raise VerificationError("source_identity_missing")

    _validate_environment(config.get("Env"))
    _validate_port_bindings(_mapping(container.get("HostConfig"), "HostConfig").get("PortBindings"))
    network_settings = _mapping(container.get("NetworkSettings"), "NetworkSettings")
    _validate_port_bindings(network_settings.get("Ports"))
    return ContainerIdentity(image_id=image_id, source_identity=source_identity)


def _mapping(value: object, error_type: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{error_type.lower()}_invalid")
    return value


def _validate_environment(environment: object) -> None:
    required = {
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_DATABASE",
        "DATA_MYSQL_HOST",
        "DATA_MYSQL_PORT",
        "DATA_MYSQL_DATABASE",
    }
    if not isinstance(environment, list):
        raise VerificationError("database_environment_missing")
    selected: dict[str, str] = {}
    for entry in environment:
        if not isinstance(entry, str):
            continue
        key, separator, value = entry.partition("=")
        if key in required:
            if not separator or key in selected:
                raise VerificationError("database_environment_invalid")
            selected[key] = value
    if set(selected) != required:
        raise VerificationError("database_environment_missing")
    for host_key in ("MYSQL_HOST", "DATA_MYSQL_HOST"):
        if selected[host_key] not in DB_HOSTS:
            raise VerificationError("database_host_not_owned")
    for port_key in ("MYSQL_PORT", "DATA_MYSQL_PORT"):
        if selected[port_key] != DB_PORT:
            raise VerificationError("database_port_not_owned")
    for database_key, expected in DB_SCHEMAS.items():
        if selected[database_key] != expected:
            raise VerificationError("database_schema_not_isolated")


def _validate_port_bindings(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {PUBLISHED_PORT}:
        raise VerificationError("published_ports_not_isolated")
    bindings = value.get(PUBLISHED_PORT)
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise VerificationError("published_ports_not_isolated")
    binding = bindings[0]
    if not isinstance(binding, dict):
        raise VerificationError("published_port_invalid")
    if binding.get("HostIp") != ENDPOINT_HOST or binding.get("HostPort") != str(ENDPOINT_PORT):
        raise VerificationError("published_port_not_loopback_owned")


def _base_report(mode: str) -> dict[str, object]:
    return {
        "date": date.today().isoformat(),
        "mode": mode,
        "source": None,
        "endpoint": ENDPOINT,
        "health": None,
        "static": None,
        "frontend_login": None,
        "userdb_write_isolated": False,
        "error": None,
    }


def _inspect_owned_container() -> ContainerIdentity:
    docker = shutil.which("docker")
    if docker is None:
        raise VerificationError("docker_unavailable")
    try:
        # Fixed argv inspects only the owned container; shell=False is the default.
        result = subprocess.run(  # noqa: S603  # nosec B603
            [docker, "inspect", APP_CONTAINER],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise VerificationError("docker_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerificationError("docker_inspect_timeout") from exc
    if result.returncode != 0:
        raise VerificationError("docker_inspect_failed")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError("docker_inspect_invalid_json") from exc
    return validate_docker_inspect(document)


def _http_request(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or ".." in Path(parsed.path).parts
    ):
        raise VerificationError("request_path_not_local")
    connection = http.client.HTTPConnection(
        ENDPOINT_HOST,
        ENDPOINT_PORT,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        response_body = response.read(HTTP_BODY_LIMIT + 1)
        if len(response_body) > HTTP_BODY_LIMIT:
            raise VerificationError("http_response_too_large")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return response.status, content_type, response_body
    except (OSError, http.client.HTTPException) as exc:
        raise VerificationError("endpoint_request_failed") from exc
    finally:
        connection.close()


def _verify_health(report: dict[str, object]) -> None:
    status, content_type, body = _http_request("/health")
    if status != 200 or content_type != "application/json":
        raise VerificationError("health_response_invalid")
    try:
        health = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("health_response_invalid") from exc
    if not isinstance(health, dict):
        raise VerificationError("health_response_invalid")
    checks = health.get("checks")
    if (
        health.get("status") != "healthy"
        or not isinstance(checks, dict)
        or checks.get("database") != "connected"
    ):
        raise VerificationError("health_not_healthy")
    report["health"] = {"status": "healthy", "database": "connected"}


def _asset_path(reference: str) -> str:
    parsed = urlsplit(reference)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/assets/")
        or parsed.query
        or parsed.fragment
        or ".." in Path(parsed.path).parts
    ):
        raise VerificationError("static_asset_reference_not_local")
    return parsed.path


def _verify_static(report: dict[str, object]) -> None:
    status, content_type, body = _http_request("/")
    if status != 200 or content_type != "text/html":
        raise VerificationError("static_home_invalid")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("static_home_invalid") from exc
    collector = _AssetCollector()
    collector.feed(html)
    if not collector.has_app_mount or not collector.module_scripts or not collector.stylesheets:
        raise VerificationError("static_asset_references_missing")
    assets = [(_asset_path(path), "javascript") for path in collector.module_scripts]
    assets.extend((_asset_path(path), "css") for path in collector.stylesheets)
    for asset_path, expected_type in assets:
        asset_status, asset_content_type, _ = _http_request(asset_path)
        if asset_status != 200:
            raise VerificationError("static_asset_fetch_failed")
        if expected_type == "javascript" and "javascript" not in asset_content_type:
            raise VerificationError("static_asset_content_type_invalid")
        if expected_type == "css" and asset_content_type != "text/css":
            raise VerificationError("static_asset_content_type_invalid")
    report["static"] = {
        "home_http_200": True,
        "assets_valid": True,
        "asset_count": len(assets),
    }


def _new_test_user() -> dict[str, str]:
    username = f"c65_{secrets.token_hex(12)}"
    password = f"A1{secrets.token_urlsafe(30)}"
    return {
        "email": f"{username}@example.org",
        "username": username,
        "password": password,
    }


def _register_user(user: dict[str, str]) -> None:
    payload = json.dumps(
        {
            "email": user["email"],
            "password": user["password"],
            "password_confirm": user["password"],
        }
    ).encode("utf-8")
    status, content_type, body = _http_request(
        "/api/v1/auth/register",
        method="POST",
        body=payload,
        headers={"Content-Type": "application/json"},
    )
    if status != 201 or content_type != "application/json":
        raise VerificationError("isolated_registration_failed")
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("isolated_registration_failed") from exc
    data = result.get("data") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or not isinstance(data, dict)
        or not isinstance(data.get("user_id"), int)
        or data.get("email") != user["email"]
    ):
        raise VerificationError("isolated_registration_failed")


_PLAYWRIGHT_PROGRAM = r"""
const fs = require('node:fs');
const { chromium } = require('playwright');
const ENDPOINT = 'http://127.0.0.1:33566';

function fail(code) {
  const error = new Error(code);
  error.safeType = code;
  throw error;
}

function checkedLocation(page, path, title) {
  const url = new URL(page.url());
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' ||
      url.port !== '33566' || url.pathname !== path || url.search || url.hash) {
    fail('UnexpectedBrowserLocation');
  }
  return page.title().then((actualTitle) => {
    if (actualTitle !== title) fail('UnexpectedPageTitle');
    return { url: url.origin + url.pathname, title: actualTitle };
  });
}

(async () => {
  let browser;
  let context;
  try {
    const credentials = JSON.parse(fs.readFileSync(0, 'utf8'));
    browser = await chromium.launch({ headless: true });
    context = await browser.newContext();
    await context.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.port === '33566') {
        await route.continue();
      } else {
        await route.abort();
      }
    });
    const page = await context.newPage();
    await page.goto(ENDPOINT + '/login', { waitUntil: 'domcontentloaded' });
    await page.locator('input[type="email"]').first().fill(credentials.email);
    await page.locator('input[type="password"]').first().fill(credentials.password);
    const loginResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === 'POST' && url.pathname === '/api/v1/auth/login';
    }, { timeout: 45000 });
    await page.getByRole('button', { name: '登录', exact: true }).click();
    const loginResponse = await loginResponsePromise;
    if (loginResponse.status() !== 200) fail('LoginRejected');
    await page.waitForURL((url) => url.pathname === '/', { timeout: 45000 });
    await page.getByRole('heading', { name: '数据概览', exact: true })
      .waitFor({ state: 'visible' });
    const userElement = page.locator('.user-dropdown .username');
    await userElement.waitFor({ state: 'visible' });
    const dashboardIdentityMatches = (await userElement.innerText()).trim() === credentials.email;
    if (!dashboardIdentityMatches) fail('DashboardIdentityNotVisible');
    const dashboard = await checkedLocation(page, '/', '首页 - opendata');
    dashboard.authenticated_element = '.user-dropdown .username';
    dashboard.authenticated_element_visible = true;

    await page.getByRole('menuitem', { name: '数据目录', exact: true }).click();
    await page.waitForURL((url) => url.pathname === '/data', { timeout: 45000 });
    await page.getByRole('heading', { name: '数据目录', exact: true })
      .waitFor({ state: 'visible' });
    const catalogIdentityMatches = (await userElement.innerText()).trim() === credentials.email;
    if (!catalogIdentityMatches) fail('CatalogIdentityNotVisible');
    const catalog = await checkedLocation(page, '/data', '数据目录 - opendata');
    catalog.authenticated_element = '.user-dropdown .username';
    catalog.authenticated_element_visible = true;
    process.stdout.write(JSON.stringify({ ok: true, dashboard, catalog }));
  } catch (error) {
    const safeType = error && typeof error.safeType === 'string'
      ? error.safeType
      : error && typeof error.name === 'string' ? error.name : 'BrowserError';
    process.stdout.write(JSON.stringify({ ok: false, error_type: safeType }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
})();
"""


def _playwright_environment() -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", "")}
    for key in ("HOME", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _verify_browser_login(user: dict[str, str]) -> dict[str, object]:
    node = shutil.which("node")
    if node is None or not PLAYWRIGHT_PACKAGE.is_dir():
        raise VerificationError("playwright_runtime_missing")
    try:
        # Fixed Node code and argv run with shell=False by default; only generated test credentials
        # reach stdin.
        result = subprocess.run(  # noqa: S603  # nosec B603
            [node, "-e", _PLAYWRIGHT_PROGRAM],
            cwd=FRONTEND_ROOT,
            env=_playwright_environment(),
            input=json.dumps({"email": user["email"], "password": user["password"]}),
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError("playwright_timeout") from exc
    except OSError as exc:
        raise VerificationError("playwright_launch_failed") from exc
    try:
        result_payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError("playwright_result_invalid") from exc
    if (
        result.returncode != 0
        or not isinstance(result_payload, dict)
        or result_payload.get("ok") is not True
    ):
        raise VerificationError("playwright_flow_failed")
    dashboard = result_payload.get("dashboard")
    catalog = result_payload.get("catalog")
    if not isinstance(dashboard, dict) or not isinstance(catalog, dict):
        raise VerificationError("playwright_result_invalid")
    expected = (
        (dashboard, f"{ENDPOINT}/", "首页 - opendata"),
        (catalog, f"{ENDPOINT}/data", "数据目录 - opendata"),
    )
    for page, expected_url, expected_title in expected:
        if (
            page.get("url") != expected_url
            or page.get("title") != expected_title
            or page.get("authenticated_element") != ".user-dropdown .username"
            or page.get("authenticated_element_visible") is not True
        ):
            raise VerificationError("playwright_result_invalid")
    return {"ok": True, "dashboard": dashboard, "catalog": catalog}


def _apply_verification(report: dict[str, object]) -> None:
    identity = _inspect_owned_container()
    report["source"] = {
        "image_id": identity.image_id,
        "source_identity": identity.source_identity,
    }
    _verify_health(report)
    _verify_static(report)
    user = _new_test_user()
    _register_user(user)
    report["userdb_write_isolated"] = True
    report["frontend_login"] = _verify_browser_login(user)


def _write_report(path: Path, report: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    open_descriptor: int | None = descriptor
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            open_descriptor = None
            json.dump(report, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
    except Exception:
        if open_descriptor is not None:
            os.close(open_descriptor)
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Run a dry plan by default; perform bounded verification only with --apply."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="inspect and exercise the owned stack")
    parser.add_argument("--output", type=Path, help="new sanitized JSON report file (mode 0600)")
    args = parser.parse_args(argv)
    if args.apply and args.output is None:
        parser.error("--apply requires --output so the sanitized evidence path is explicit")

    report = _base_report("apply" if args.apply else "dry-run")
    if args.apply:
        try:
            _apply_verification(report)
        except VerificationError as exc:
            report["error"] = {"type": exc.error_type}
        except Exception as exc:  # Never echo exception messages that may contain request data.
            report["error"] = {"type": type(exc).__name__}
    if args.output is not None:
        try:
            _write_report(args.output, report)
        except OSError:
            report["error"] = {"type": "report_write_failed"}
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["error"] is not None else 0


if __name__ == "__main__":
    sys.exit(main())
