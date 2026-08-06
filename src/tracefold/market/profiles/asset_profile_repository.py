from __future__ import annotations

import time
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.market.profiles.profile_source_ids import GMGN_DEX_PROFILE_PROVIDER
from tracefold.platform.postgres.json_safety import postgres_safe_json, postgres_safe_text

_NON_READY_STATUSES = {"missing", "unsupported", "error"}


class AssetProfileRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def upsert_ready_profile(
        self,
        *,
        asset_id: str,
        provider: str,
        symbol: str | None,
        name: str | None,
        logo_url: str | None,
        banner_url: str | None,
        website_url: str | None,
        twitter_username: str | None,
        twitter_url: str | None,
        telegram_url: str | None,
        gmgn_url: str | None,
        geckoterminal_url: str | None,
        description: str | None,
        raw_payload: dict[str, Any] | None,
        observed_at_ms: int,
        next_refresh_at_ms: int,
    ) -> None:
        updated_at_ms = int(observed_at_ms)

        self.conn.execute(
            """
            INSERT INTO asset_profiles(
              asset_id, provider, status, symbol, name, logo_url, banner_url, website_url,
              twitter_username, twitter_url, telegram_url, gmgn_url, geckoterminal_url,
              description, raw_payload_json, observed_at_ms, next_refresh_at_ms, last_error,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, 'ready', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s
            )
            ON CONFLICT(asset_id, provider) DO UPDATE SET
              status = 'ready',
              symbol = excluded.symbol,
              name = excluded.name,
              logo_url = excluded.logo_url,
              banner_url = excluded.banner_url,
              website_url = excluded.website_url,
              twitter_username = excluded.twitter_username,
              twitter_url = excluded.twitter_url,
              telegram_url = excluded.telegram_url,
              gmgn_url = excluded.gmgn_url,
              geckoterminal_url = excluded.geckoterminal_url,
              description = excluded.description,
              raw_payload_json = excluded.raw_payload_json,
              observed_at_ms = excluded.observed_at_ms,
              next_refresh_at_ms = excluded.next_refresh_at_ms,
              last_error = NULL,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                asset_id,
                _required_text(provider),
                _optional_text(symbol),
                _optional_text(name),
                _optional_text(logo_url),
                _optional_text(banner_url),
                _optional_text(website_url),
                _optional_text(twitter_username),
                _optional_text(twitter_url),
                _optional_text(telegram_url),
                _optional_text(gmgn_url),
                _optional_text(geckoterminal_url),
                _optional_text(description),
                Jsonb(_sanitize_json(raw_payload or {})),
                int(observed_at_ms),
                int(next_refresh_at_ms),
                updated_at_ms,
                updated_at_ms,
            ),
        )

    def upsert_status(
        self,
        *,
        asset_id: str,
        provider: str,
        status: str,
        observed_at_ms: int | None,
        next_refresh_at_ms: int,
        last_error: str | None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in _NON_READY_STATUSES:
            raise ValueError("asset profile status upsert requires a non-ready status")
        updated_at_ms = int(observed_at_ms) if observed_at_ms is not None else _now_ms()

        self.conn.execute(
            """
            INSERT INTO asset_profiles(
              asset_id, provider, status, symbol, name, logo_url, banner_url, website_url,
              twitter_username, twitter_url, telegram_url, gmgn_url, geckoterminal_url,
              description, raw_payload_json, observed_at_ms, next_refresh_at_ms, last_error,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %s, %s, %s, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, %s,
              %s, %s, %s, %s, %s
            )
            ON CONFLICT(asset_id, provider) DO UPDATE SET
              status = excluded.status,
              symbol = NULL,
              name = NULL,
              logo_url = NULL,
              banner_url = NULL,
              website_url = NULL,
              twitter_username = NULL,
              twitter_url = NULL,
              telegram_url = NULL,
              gmgn_url = NULL,
              geckoterminal_url = NULL,
              description = NULL,
              raw_payload_json = excluded.raw_payload_json,
              observed_at_ms = excluded.observed_at_ms,
              next_refresh_at_ms = excluded.next_refresh_at_ms,
              last_error = excluded.last_error,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                asset_id,
                _required_text(provider),
                normalized_status,
                Jsonb(_sanitize_json(raw_payload or {})),
                int(observed_at_ms) if observed_at_ms is not None else None,
                int(next_refresh_at_ms),
                _optional_text(last_error),
                updated_at_ms,
                updated_at_ms,
            ),
        )

    def profiles_for_asset_ids(
        self,
        asset_ids: list[str],
        *,
        provider: str = GMGN_DEX_PROFILE_PROVIDER,
    ) -> dict[str, dict[str, Any]]:
        requested_asset_ids = list(dict.fromkeys(asset_ids))
        if not requested_asset_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT *
            FROM asset_profiles
            WHERE provider = %s AND asset_id = ANY(%s)
            """,
            (_required_text(provider), requested_asset_ids),
        ).fetchall()
        return {str(row["asset_id"]): dict(row) for row in rows}


def _optional_text(value: str | None) -> str | None:
    text = _clean_text(value).strip()
    return text or None


def _required_text(value: str) -> str:
    return _clean_text(value).strip()


def _clean_text(value: Any) -> str:
    return postgres_safe_text(value)


def _sanitize_json(value: Any) -> Any:
    return postgres_safe_json(value)


def _now_ms() -> int:
    return int(time.time() * 1000)
