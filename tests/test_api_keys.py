"""Consumer API key tests (A5 prerequisite, design §10.3 / FR-19).

Storage, lifecycle and access rules: only the hash is persisted, the
plaintext appears once, expiry and revocation are fail-closed, and an
API key only reaches the domains its scopes cover.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from opendata.models.api_key import ApiKey, ApiKeyStatus
from opendata.services.api_key_service import (
    KEY_PREFIX,
    ApiKeyError,
    ApiKeyService,
    api_key_pepper,
    generate_key,
    hash_key,
    is_expired,
    key_allows_domain,
    normalize_scope_input,
)
from tests.conftest import get_auth_headers


def _plaintext_of(issued) -> str:
    return issued.plaintext


class TestKeyMaterial:
    def test_plaintext_has_the_prefix_and_enough_entropy(self):
        first, second = generate_key(), generate_key()

        assert first.startswith(KEY_PREFIX)
        assert len(first) >= len(KEY_PREFIX) + 32  # token_urlsafe(32)
        assert first != second

    def test_hash_is_stable_and_peppered(self, monkeypatch):
        from opendata.core import config as config_module

        key = generate_key()
        baseline = hash_key(key)

        assert hash_key(key) == baseline
        assert len(baseline) == 64
        assert key not in baseline  # the plaintext is not recoverable

        monkeypatch.setattr(config_module.settings, "api_key_pepper", "pepper-one")
        assert hash_key(key) != baseline

    def test_pepper_falls_back_to_the_secret(self, monkeypatch):
        from opendata.core import config as config_module

        monkeypatch.setattr(config_module.settings, "api_key_pepper", None)
        assert api_key_pepper() == config_module.settings.secret_key

    @pytest.mark.parametrize(
        ("scopes", "expected"),
        [
            (["stock_daily", "stock_daily", " "], ["stock_daily"]),
            (None, []),
            ([], []),
            (["*"], ["*"]),
        ],
    )
    def test_scope_normalization(self, scopes, expected):
        assert normalize_scope_input(scopes) == expected

    def test_scope_matching_is_fail_closed(self):
        assert key_allows_domain(["stock_daily"], "stock_daily")
        assert key_allows_domain(["*"], "anything")
        assert not key_allows_domain([], "stock_daily")
        assert not key_allows_domain(["index_constituent"], "stock_daily")


class TestExpiryRule:
    def test_no_expiry_never_expires(self):
        record = ApiKey(key_hash="h", name="n", owner_user_id=1, scopes=[], rate_limit=1)

        assert not is_expired(record)

    def test_past_expiry_is_expired(self):
        record = ApiKey(
            key_hash="h",
            name="n",
            owner_user_id=1,
            scopes=[],
            rate_limit=1,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        assert is_expired(record)

    def test_naive_timestamp_from_mysql_is_handled(self):
        record = ApiKey(
            key_hash="h",
            name="n",
            owner_user_id=1,
            scopes=[],
            rate_limit=1,
            expires_at=datetime(2020, 1, 1, 12, 0),  # naive, as MySQL returns it
        )

        assert is_expired(record)


class TestApiKeyService:
    @pytest.fixture
    async def owner(self, test_db):
        from opendata.models.user import User

        user = User(
            username="key-owner",
            email="key-owner@example.com",
            hashed_password="x",
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)
        return user

    async def test_issue_stores_only_the_hash(self, test_db, owner):
        issued = await ApiKeyService(test_db).issue(
            owner=owner, name="backtrader_web", scopes=["stock_daily"]
        )

        stored = issued.record
        assert stored.key_hash == hash_key(issued.plaintext)
        assert issued.plaintext not in stored.key_hash
        assert stored.scopes == ["stock_daily"]
        assert stored.status is ApiKeyStatus.ACTIVE
        assert stored.rate_limit > 0

    async def test_issue_rejects_empty_name_and_bad_limit(self, test_db, owner):
        service = ApiKeyService(test_db)

        with pytest.raises(ApiKeyError, match="needs a name"):
            await service.issue(owner=owner, name="  ")
        with pytest.raises(ApiKeyError, match="rate_limit must be positive"):
            await service.issue(owner=owner, name="x", rate_limit=0)

    async def test_authenticate_accepts_the_plaintext_and_touches_last_used(self, test_db, owner):
        service = ApiKeyService(test_db)
        issued = await service.issue(owner=owner, name="touch", scopes=["stock_daily"])

        assert issued.record.last_used_at is None
        record = await service.authenticate(_plaintext_of(issued))

        assert record is not None
        assert record.id == issued.record.id
        assert record.last_used_at is not None

    async def test_authenticate_rejects_unknown_and_empty(self, test_db, owner):
        service = ApiKeyService(test_db)

        assert await service.authenticate("od-not-a-real-key") is None
        with pytest.raises(ApiKeyError, match="empty API key"):
            await service.authenticate("   ")

    async def test_revoked_key_stops_authenticating(self, test_db, owner):
        service = ApiKeyService(test_db)
        issued = await service.issue(owner=owner, name="revoke-me", scopes=["*"])

        await service.revoke(issued.record)

        assert await service.authenticate(_plaintext_of(issued)) is None
        with pytest.raises(ApiKeyError, match="already revoked"):
            await service.revoke(issued.record)

    async def test_expired_key_stops_authenticating(self, test_db, owner):
        service = ApiKeyService(test_db)
        issued = await service.issue(
            owner=owner,
            name="expiring",
            scopes=["*"],
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        assert await service.authenticate(_plaintext_of(issued)) is None

    async def test_rotate_revokes_the_old_key_and_keeps_the_settings(self, test_db, owner):
        service = ApiKeyService(test_db)
        issued = await service.issue(
            owner=owner, name="rotating", scopes=["stock_daily", "index_constituent"], rate_limit=7
        )

        replacement = await service.rotate(issued.record)

        assert replacement.plaintext != issued.plaintext
        assert await service.authenticate(_plaintext_of(issued)) is None
        assert await service.authenticate(replacement.plaintext) is not None
        assert replacement.record.scopes == ["index_constituent", "stock_daily"]
        assert replacement.record.rate_limit == 7
        assert replacement.record.rotated_at is not None
        assert issued.record.rotated_at is not None
        assert issued.record.status is ApiKeyStatus.REVOKED

    async def test_rotate_refuses_a_revoked_key(self, test_db, owner):
        service = ApiKeyService(test_db)
        issued = await service.issue(owner=owner, name="dead", scopes=["*"])
        await service.revoke(issued.record)

        with pytest.raises(ApiKeyError, match="cannot be rotated"):
            await service.rotate(issued.record)

    async def test_list_keys_scopes_to_the_owner(self, test_db, owner):
        from opendata.models.user import User

        other = User(
            username="other-owner",
            email="other-owner@example.com",
            hashed_password="x",
            is_active=True,
        )
        test_db.add(other)
        await test_db.commit()
        await test_db.refresh(other)
        service = ApiKeyService(test_db)
        await service.issue(owner=owner, name="mine", scopes=["*"])
        await service.issue(owner=other, name="theirs", scopes=["*"])

        mine = await service.list_keys(owner)
        everyone = await service.list_keys()

        assert [record.name for record in mine] == ["mine"]
        assert {"mine", "theirs"}.issubset({record.name for record in everyone})


class TestApiKeyEndpoints:
    async def _create(self, client, token, **payload):
        body = {"name": "consumer", "scopes": ["stock_daily"], **payload}
        response = await client.post("/api/v1/keys/", json=body, headers=get_auth_headers(token))
        return response

    async def test_creation_returns_the_plaintext_once(self, test_client, test_user_token):
        response = await self._create(test_client, test_user_token)

        assert response.status_code == 201
        payload = response.json()
        assert payload["key"].startswith(KEY_PREFIX)
        assert payload["api_key"]["scopes"] == ["stock_daily"]
        assert "key_hash" not in payload["api_key"]

        listed = await test_client.get("/api/v1/keys/", headers=get_auth_headers(test_user_token))
        assert listed.status_code == 200
        body = listed.json()
        assert [entry["name"] for entry in body] == ["consumer"]
        assert "key" not in body[0]
        assert "key_hash" not in body[0]

    async def test_listing_is_owner_scoped(self, test_client, test_user_token, test_admin_token):
        await self._create(test_client, test_user_token, name="user-key")

        as_admin = await test_client.get(
            "/api/v1/keys/", headers=get_auth_headers(test_admin_token)
        )
        as_user = await test_client.get("/api/v1/keys/", headers=get_auth_headers(test_user_token))

        assert as_admin.status_code == 200
        assert len(as_admin.json()) >= 1  # admin sees everyone
        assert [entry["name"] for entry in as_user.json()] == ["user-key"]

    async def test_validation_and_auth_are_enforced(self, test_client, test_user_token):
        anonymous = await test_client.get("/api/v1/keys/")
        unnamed = await self._create(test_client, test_user_token, name="")
        bad_limit = await self._create(test_client, test_user_token, rate_limit=0)

        assert anonymous.status_code == 401
        assert unnamed.status_code == 422  # pydantic min_length
        assert bad_limit.status_code == 422  # pydantic gt=0

    async def test_revoke_and_rotate_lifecycle(self, test_client, test_user_token):
        created = (await self._create(test_client, test_user_token)).json()
        key_id = created["api_key"]["id"]

        rotated = await test_client.post(
            f"/api/v1/keys/{key_id}/rotate", headers=get_auth_headers(test_user_token)
        )
        assert rotated.status_code == 200
        assert rotated.json()["key"] != created["key"]
        new_id = rotated.json()["api_key"]["id"]
        assert new_id != key_id

        rotated_away = await test_client.post(
            f"/api/v1/keys/{key_id}/revoke", headers=get_auth_headers(test_user_token)
        )
        revoked = await test_client.post(
            f"/api/v1/keys/{new_id}/revoke", headers=get_auth_headers(test_user_token)
        )
        again = await test_client.post(
            f"/api/v1/keys/{new_id}/revoke", headers=get_auth_headers(test_user_token)
        )

        assert rotated_away.status_code == 400  # rotation already revoked the old key
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert again.status_code == 400

    async def test_another_user_cannot_manage_someone_elses_key(
        self, test_client, test_user_token, test_db
    ):
        from opendata.models.user import User

        created = (await self._create(test_client, test_user_token)).json()
        intruder = User(
            username="intruder",
            email="intruder@example.com",
            hashed_password="x",
            is_active=True,
        )
        test_db.add(intruder)
        await test_db.commit()
        await test_db.refresh(intruder)
        body = {"name": "intruder", "scopes": ["*"], "owner_user_id": intruder.id}

        stolen = await test_client.post(
            "/api/v1/keys/", json=body, headers=get_auth_headers(test_user_token)
        )
        revoke = await test_client.post(
            f"/api/v1/keys/{created['api_key']['id']}/revoke",
            headers=get_auth_headers(test_user_token),
        )
        missing = await test_client.post(
            "/api/v1/keys/999999/revoke", headers=get_auth_headers(test_user_token)
        )

        assert stolen.status_code == 403  # non-admin cannot target another owner
        assert revoke.status_code == 200  # own key is fine
        assert missing.status_code == 404


class TestApiKeyAuthentication:
    async def _key(self, client, token, scopes: list[str]) -> str:
        response = await client.post(
            "/api/v1/keys/",
            json={"name": "consumer", "scopes": scopes},
            headers=get_auth_headers(token),
        )
        assert response.status_code == 201
        return response.json()["key"]

    async def test_bearer_and_header_forms_both_authenticate(self, test_client, test_user_token):
        key = await self._key(test_client, test_user_token, ["*"])

        via_bearer = await test_client.get(
            "/api/v1/data/catalog", headers={"Authorization": f"Bearer {key}"}
        )
        via_header = await test_client.get("/api/v1/data/catalog", headers={"X-API-Key": key})

        assert via_bearer.status_code == 200
        assert via_header.status_code == 200

    async def test_jwt_still_works(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/catalog", headers=get_auth_headers(test_user_token)
        )

        assert response.status_code == 200

    async def test_bad_key_is_rejected_after_a_uniform_delay(self, test_client, monkeypatch):
        from opendata.api import dependencies as dependencies_module

        monkeypatch.setattr(dependencies_module.settings, "api_key_failure_delay_seconds", 0.0)
        response = await test_client.get("/api/v1/data/catalog", headers={"X-API-Key": "od-nope"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid API key"

    async def test_revoked_key_cannot_read_data(self, test_client, test_user_token):
        created = (
            await test_client.post(
                "/api/v1/keys/",
                json={"name": "doomed", "scopes": ["*"]},
                headers=get_auth_headers(test_user_token),
            )
        ).json()
        key = created["key"]
        await test_client.post(
            f"/api/v1/keys/{created['api_key']['id']}/revoke",
            headers=get_auth_headers(test_user_token),
        )

        response = await test_client.get("/api/v1/data/catalog", headers={"X-API-Key": key})

        assert response.status_code == 401

    async def test_scopes_limit_the_reachable_domains(self, test_client, test_user_token):
        from opendata.data.providers import register_providers

        register_providers()  # ASGITransport does not run the lifespan
        key = await self._key(test_client, test_user_token, ["index_constituent"])

        allowed = await test_client.get(
            "/api/v1/data/index/index_constituent",
            params={"symbols": "000300"},
            headers={"X-API-Key": key},
        )
        denied = await test_client.get(
            "/api/v1/data/stock/daily",
            params={"symbols": "600519"},
            headers={"X-API-Key": key},
        )
        catalog = await test_client.get("/api/v1/data/catalog", headers={"X-API-Key": key})
        listed = {entry["domain"] for entry in catalog.json()["data"]["domains"]}

        assert denied.status_code == 403
        assert listed == {"index_constituent"}  # the catalog hides the rest
        assert allowed.status_code != 403  # past the scope gate

    async def test_empty_scopes_deny_every_domain(self, test_client, test_user_token):
        key = await self._key(test_client, test_user_token, [])

        response = await test_client.get(
            "/api/v1/data/stock/daily", params={"symbols": "600519"}, headers={"X-API-Key": key}
        )
        catalog = await test_client.get("/api/v1/data/catalog", headers={"X-API-Key": key})

        assert response.status_code == 403
        assert catalog.json()["data"]["domains"] == []


@pytest.mark.e2e
class TestApiKeysAgainstMysql:
    @pytest.fixture
    def main_db(self):
        from sqlalchemy import create_engine, pool

        from opendata.core.config import settings

        engine = create_engine(settings.database_url_sync, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"main database unreachable: {type(exc).__name__}")
        yield engine
        engine.dispose()

    def test_table_exists_with_the_expected_columns(self, main_db):
        from sqlalchemy import inspect

        with main_db.connect() as connection:
            inspector = inspect(connection)
            columns = {column["name"] for column in inspector.get_columns("api_keys")}
            indexes = {index["name"] for index in inspector.get_indexes("api_keys")}

        assert {
            "key_hash",
            "name",
            "owner_user_id",
            "scopes",
            "rate_limit",
            "status",
            "last_used_at",
            "expires_at",
            "rotated_at",
        }.issubset(columns)
        assert "ix_api_keys_key_hash" in indexes
        assert "ix_api_keys_owner_user_id" in indexes

    def test_hash_is_unique_at_the_database_level(self, main_db):
        statement = text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'api_keys' "
            "AND index_name = 'ix_api_keys_key_hash' AND non_unique = 0"
        )
        with main_db.connect() as connection:
            unique_indexes = connection.execute(statement).scalar()

        assert unique_indexes and unique_indexes > 0


@pytest.mark.e2e
class TestApiKeyRoundTripOnMysql:
    """The whole flow against the real main database (no ORM shortcuts)."""

    async def test_issue_authenticate_revoke_round_trip(self):
        from sqlalchemy import create_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from opendata.core.config import settings

        engine = create_engine(settings.database_url_sync, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"main database unreachable: {type(exc).__name__}")
        engine.dispose()

        async_engine = create_async_engine(settings.database_url, poolclass=NullPool)
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        async with session_maker() as session:
            from opendata.models.user import User

            user = User(
                username="a5-round-trip",
                email="a5-round-trip@example.com",
                hashed_password="x",
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            try:
                service = ApiKeyService(session)
                issued = await service.issue(owner=user, name="round-trip", scopes=["stock_daily"])
                found = await session.execute(
                    select(ApiKey).where(ApiKey.key_hash == hash_key(issued.plaintext))
                )
                assert found.scalar_one().scopes == ["stock_daily"]
                assert (await service.authenticate(issued.plaintext)) is not None
                await service.revoke(issued.record)
                assert (await service.authenticate(issued.plaintext)) is None
            finally:
                await session.execute(
                    text("DELETE FROM api_keys WHERE owner_user_id = :owner"),
                    {"owner": user.id},
                )
                await session.execute(
                    text("DELETE FROM users WHERE id = :owner"), {"owner": user.id}
                )
                await session.commit()
        await async_engine.dispose()
