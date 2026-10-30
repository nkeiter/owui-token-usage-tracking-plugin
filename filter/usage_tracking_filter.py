"""
title: Gettysburg Usage Tracking Filter
author: Beau D'Amore (original), Nathan I Keiter (Gettysburg College fork) nkeiter@gettysburg.edu
date: 2026-10-26
version: 1.5.0
license: MIT
description: Token usage tracking and group-based rate limiting with PostgreSQL persistence.
required_open_webui_version: >= 0.12.4
requirements: psycopg[binary], psycopg-pool
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from fastapi import Request

# PostgreSQL imports
import psycopg
import psycopg_pool

# Set up logging
logger = logging.getLogger("openwebui.filters.usage_tracking")
logger.setLevel(logging.INFO)

PLUGIN_NAME = "Gettysburg Usage Tracking Filter"
TIER_GROUP_PREFIX = "token-limit-"
DEFAULT_GROUP = f"{TIER_GROUP_PREFIX}standard"
DEFAULT_USAGE_TIMEZONE = "America/New_York"

# Adjust the prefix, tier names, quotas, and timezone to match the deployment.
DEFAULT_TIERS = [
    {
        "group_name": f"{TIER_GROUP_PREFIX}enterprise",
        "daily_token_limit": -1,
        "monthly_token_limit": -1,
        "description": "Enterprise tier - unlimited",
    },
    {
        "group_name": f"{TIER_GROUP_PREFIX}pro",
        "daily_token_limit": 1000000,
        "monthly_token_limit": 10000000,
        "description": "Pro tier - 1M/day, 10M/month",
    },
    {
        "group_name": DEFAULT_GROUP,
        "daily_token_limit": 500000,
        "monthly_token_limit": 5000000,
        "description": "Standard tier - 500K/day, 5M/month",
    },
    {
        "group_name": f"{TIER_GROUP_PREFIX}test",
        "daily_token_limit": 1000,
        "monthly_token_limit": 5000,
        "description": "Test tier - 1K/day, 5K/month",
    },
]


class Filter:
    """
    Gettysburg College Usage Tracking Filter for Open WebUI

    Tracks token usage per user and enforces group-based limits.
    Stores only user UUIDs - no PII.

    Usage tiers are native Open WebUI groups, so membership is managed through the
    standard Groups admin UI. On load, any missing configured tier group is created
    in the native `group` table. Per-tier quotas are tracked in the filter's own
    `usage_limits` table because Open WebUI's group UI has no concept of token quotas.

    Flow:
    - inlet(): Check if user is over daily/monthly limit → block if so
    - outlet(): Record token usage from response
    """

    class Valves(BaseModel):
        """Configuration for the usage tracking filter."""

        # Execution Priority - MUST run before other filters
        priority: int = Field(
            default=5,
            description="Filter execution priority. Set lower than filters that should run after the usage check (e.g., 5).",
        )

        # PostgreSQL Configuration
        postgres_host: str = Field(
            default="localhost",
            description="PostgreSQL host reachable from Open WebUI",
        )
        postgres_port: int = Field(default=5432, description="PostgreSQL port")
        postgres_database: str = Field(
            default="open_webui",
            description="PostgreSQL database containing Open WebUI's user and group tables",
        )
        postgres_user: str = Field(
            default="postgres", description="PostgreSQL username"
        )
        postgres_password: str = Field(
            default="", description="PostgreSQL password"
        )

        # Default Group
        default_group: str = Field(
            default=DEFAULT_GROUP,
            description="Default built-in tier; must match a group_name in DEFAULT_TIERS",
        )

        usage_timezone: str = Field(
            default=DEFAULT_USAGE_TIMEZONE,
            description="IANA timezone used for daily/monthly quota windows and resets",
        )

        # Behavior Configuration
        enable_blocking: bool = Field(
            default=True,
            description="Block requests when limits exceeded. If False, only logs warnings.",
        )
        show_usage_status: bool = Field(
            default=True, description="Show usage status messages to users"
        )
        warn_at_percent: int = Field(
            default=80,
            description="Warn users when they reach this percentage of their limit",
        )

        # Admin Bypass
        admin_bypass: bool = Field(
            default=True, description="Allow admin users to bypass limits"
        )

        # Debug
        debug_mode: bool = Field(
            default=False, description="Enable detailed debug logging"
        )

    def __init__(self):
        self.name = PLUGIN_NAME
        self.valves = self.Valves()
        self._pool = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _log(self, message: str, level: str = "info"):
        """Centralized logging"""
        if level == "debug" and not self.valves.debug_mode:
            return
        print(f"[Usage Tracking] [{level.upper()}] {message}", flush=True)
        getattr(logger, level, logger.info)(f"[Usage Tracking] {message}")

    @staticmethod
    def _sql_string_literal(value: str) -> str:
        """Escape a string for use as a SQL string literal."""
        return "'" + value.replace("'", "''") + "'"

    def _get_schema_sql(self) -> str:
        """Return the complete schema SQL for auto-creation"""
        tiers_by_name = {tier["group_name"]: tier for tier in DEFAULT_TIERS}
        default_tier = tiers_by_name.get(self.valves.default_group)
        if default_tier is None:
            raise ValueError("default_group must match a group_name in DEFAULT_TIERS")

        tier_names_sql = ", ".join(
            self._sql_string_literal(name) for name in tiers_by_name
        )
        tier_group_prefix_sql = self._sql_string_literal(TIER_GROUP_PREFIX)
        tier_rows_sql = ",\n            ".join(
            "("
            f"{self._sql_string_literal(tier['group_name'])}, "
            f"{int(tier['daily_token_limit'])}, "
            f"{int(tier['monthly_token_limit'])}, "
            f"{self._sql_string_literal(tier['description'])}"
            ")"
            for tier in DEFAULT_TIERS
        )
        default_group_sql = self._sql_string_literal(self.valves.default_group)
        timezone_sql = self._sql_string_literal(self.valves.usage_timezone)

        return f"""
        -- ============================================================================
        -- USAGE LIMITS: Group-based quota definitions
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS usage_limits (
            group_name VARCHAR(50) PRIMARY KEY,
            daily_token_limit BIGINT NOT NULL DEFAULT {int(default_tier['daily_token_limit'])},
            monthly_token_limit BIGINT NOT NULL DEFAULT {int(default_tier['monthly_token_limit'])},
            rate_limit_rpm INT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            description TEXT
        );

        -- Insert default tiers (skip if already exist)
        INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description)
        VALUES 
            {tier_rows_sql}
        ON CONFLICT (group_name) DO NOTHING;

        -- ============================================================================
        -- USER GROUPS: Maps user UUIDs to their group (no PII stored)
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id VARCHAR(255) PRIMARY KEY,
            group_name VARCHAR(50) NOT NULL REFERENCES usage_limits(group_name) DEFAULT {default_group_sql},
            assigned_at TIMESTAMPTZ DEFAULT NOW(),
            assigned_by VARCHAR(255),
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_name);

        -- ============================================================================
        -- USAGE RECORDS: Token usage log (the main tracking table)
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS usage_records (
            id BIGSERIAL PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT NOW(),
            prompt_tokens INT NOT NULL DEFAULT 0,
            completion_tokens INT NOT NULL DEFAULT 0,
            total_tokens INT NOT NULL DEFAULT 0,
            model_id VARCHAR(255),
            pipeline_id VARCHAR(255),
            chat_id VARCHAR(255),
            request_type VARCHAR(50) DEFAULT 'chat'
        );

        -- Indexes for fast queries
        CREATE INDEX IF NOT EXISTS idx_usage_records_user ON usage_records(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_records_date ON usage_records(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_usage_records_user_date ON usage_records(user_id, recorded_at DESC);
        -- Note: Cannot use DATE(recorded_at) in index - not immutable. Use recorded_at::date in queries instead.

        -- ============================================================================
        -- USER LIMIT OVERRIDES: optional per-user limits (limits only - tier
        -- membership always comes from native Open WebUI groups)
        -- ============================================================================
        CREATE TABLE IF NOT EXISTS user_limit_overrides (
            user_id VARCHAR(255) PRIMARY KEY,
            daily_token_limit BIGINT,
            monthly_token_limit BIGINT,
            notes TEXT,
            updated_by VARCHAR(255),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- ============================================================================
        -- TIER RESOLUTION: the single source of truth for what a user is allowed.
        -- A tier is any native group with the configured prefix that has a row in usage_limits.
        --   * DEFAULT tiers: the groups configured in DEFAULT_TIERS.
        --     A user has at most ONE default tier; if several are present the MOST
        --     RECENTLY ADDED membership wins (ties follow DEFAULT_TIERS order).
        --     No default membership means the configured default_group.
        --   * CUSTOM tiers: every other token-limit-* tier. A user may be in any number.
        -- Limits: a user in one or more custom tiers gets the SUM of the custom tiers'
        -- limits (the default tier is not included in the sum; any custom tier with -1
        -- makes that limit unlimited). A user in no custom tier gets the default tier's
        -- limits. Per-user overrides (user_limit_overrides) still win over both.
        -- This function block is deployed together; keep its BEGIN/END markers intact.
        -- ============================================================================
        -- BEGIN tier-functions
        -- The user's one default (built-in) tier, or NULL if they have none.
        CREATE OR REPLACE FUNCTION resolve_user_tier(p_user_id VARCHAR)
        RETURNS VARCHAR AS $$
            SELECT g.name::VARCHAR
            FROM "group" g
            JOIN group_member gm ON gm.group_id = g.id AND gm.user_id = p_user_id
            JOIN usage_limits ul ON ul.group_name = g.name::VARCHAR
            WHERE g.name IN ({tier_names_sql})
            ORDER BY gm.created_at DESC NULLS LAST,
                COALESCE(array_position(
                    ARRAY[{tier_names_sql}],
                    g.name::TEXT
                ), 99),
                g.name
            LIMIT 1;
        $$ LANGUAGE sql STABLE;

        -- Everything the user is a member of plus the limits that apply (before overrides).
        --   tiers        every tier group the user is in (default tier first); configured default if none
        --   default_tier the explicit default tier, or NULL
        --   custom_tiers the custom tiers the user is in
        --   limit_source 'custom' (sum of custom tiers), 'tier' (default tier) or 'default' (no membership)
        CREATE OR REPLACE FUNCTION resolve_user_limits(p_user_id VARCHAR)
        RETURNS TABLE (
            tiers VARCHAR[],
            default_tier VARCHAR,
            custom_tiers VARCHAR[],
            daily_limit BIGINT,
            monthly_limit BIGINT,
            limit_source VARCHAR
        ) AS $$
            SELECT
                CASE WHEN dt.name IS NULL AND c.n = 0 THEN ARRAY[{default_group_sql}]::VARCHAR[]
                     WHEN dt.name IS NULL THEN c.names
                     ELSE ARRAY[dt.name]::VARCHAR[] || c.names END,
                dt.name,
                c.names,
                (CASE WHEN c.n > 0 THEN c.d ELSE COALESCE(base.daily_token_limit, {int(default_tier['daily_token_limit'])}) END)::BIGINT,
                (CASE WHEN c.n > 0 THEN c.m ELSE COALESCE(base.monthly_token_limit, {int(default_tier['monthly_token_limit'])}) END)::BIGINT,
                (CASE WHEN c.n > 0 THEN 'custom' WHEN dt.name IS NOT NULL THEN 'tier' ELSE 'default' END)::VARCHAR
            FROM (SELECT resolve_user_tier(p_user_id) AS name) dt
            CROSS JOIN LATERAL (
                SELECT COALESCE(array_agg(x.name ORDER BY x.name), ARRAY[]::VARCHAR[]) AS names,
                       COUNT(*) AS n,
                       (CASE WHEN bool_or(x.d = -1) THEN -1 ELSE SUM(x.d) END)::BIGINT AS d,
                       (CASE WHEN bool_or(x.m = -1) THEN -1 ELSE SUM(x.m) END)::BIGINT AS m
                FROM (
                    SELECT g.name::VARCHAR AS name, ul.daily_token_limit AS d, ul.monthly_token_limit AS m
                    FROM "group" g
                    JOIN group_member gm ON gm.group_id = g.id AND gm.user_id = p_user_id
                    JOIN usage_limits ul ON ul.group_name = g.name::VARCHAR
                    WHERE starts_with(g.name, {tier_group_prefix_sql})
                      AND g.name NOT IN ({tier_names_sql})
                ) x
            ) c
            LEFT JOIN usage_limits base ON base.group_name = COALESCE(dt.name, {default_group_sql});
        $$ LANGUAGE sql STABLE;

        -- Get user's current limits and usage in one call. group_name lists every tier
        -- the user is in (comma separated). A row in user_limit_overrides (limits only,
        -- never membership) takes precedence over the tier limits.
        DROP FUNCTION IF EXISTS get_user_usage_status(VARCHAR);
        CREATE FUNCTION get_user_usage_status(p_user_id VARCHAR(255))
        RETURNS TABLE (
            group_name TEXT,
            daily_limit BIGINT,
            monthly_limit BIGINT,
            tokens_today BIGINT,
            tokens_this_month BIGINT,
            is_over_daily BOOLEAN,
            is_over_monthly BOOLEAN,
            limit_source VARCHAR(20)
        ) AS $$
        BEGIN
            RETURN QUERY
            WITH t AS (
                SELECT
                    array_to_string(r.tiers, ', ')::TEXT AS tier,
                    COALESCE(uo.daily_token_limit, r.daily_limit) AS dl,
                    COALESCE(uo.monthly_token_limit, r.monthly_limit) AS ml,
                    (CASE WHEN uo.user_id IS NOT NULL THEN 'override' ELSE r.limit_source END)::VARCHAR(20) AS src
                FROM resolve_user_limits(p_user_id) r
                LEFT JOIN user_limit_overrides uo ON uo.user_id = p_user_id
            ),
            d AS (
                SELECT COALESCE(SUM(ur.total_tokens), 0)::BIGINT AS total
                FROM usage_records ur
                WHERE ur.user_id = p_user_id AND (ur.recorded_at AT TIME ZONE {timezone_sql})::date = (NOW() AT TIME ZONE {timezone_sql})::date
            ),
            m AS (
                SELECT COALESCE(SUM(ur.total_tokens), 0)::BIGINT AS total
                FROM usage_records ur
                WHERE ur.user_id = p_user_id AND ur.recorded_at >= (DATE_TRUNC('month', NOW() AT TIME ZONE {timezone_sql}) AT TIME ZONE {timezone_sql})
            )
            SELECT
                t.tier,
                t.dl::BIGINT,
                t.ml::BIGINT,
                d.total,
                m.total,
                (t.dl <> -1 AND d.total >= t.dl),
                (t.ml <> -1 AND m.total >= t.ml),
                t.src
            FROM t, d, m;
        END;
        $$ LANGUAGE plpgsql;
        -- END tier-functions

        -- ============================================================================
        -- VIEWS: Convenient queries for common operations
        -- ============================================================================

        -- CREATE OR REPLACE VIEW cannot change a column's data type (the timezone
        -- pinning changed usage_month from timestamptz to timestamp), so drop the
        -- views first (dependents before their sources). Views hold no data.
        DROP VIEW IF EXISTS users_near_limit;
        DROP VIEW IF EXISTS usage_summary;
        DROP VIEW IF EXISTS usage_daily;
        DROP VIEW IF EXISTS usage_monthly;

        -- Daily usage per user
        CREATE VIEW usage_daily AS
        SELECT 
            user_id,
            (recorded_at AT TIME ZONE {timezone_sql})::date as usage_date,
            SUM(prompt_tokens) as prompt_tokens,
            SUM(completion_tokens) as completion_tokens,
            SUM(total_tokens) as total_tokens,
            COUNT(*) as request_count
        FROM usage_records
        GROUP BY user_id, (recorded_at AT TIME ZONE {timezone_sql})::date;

        -- Monthly usage per user
        CREATE VIEW usage_monthly AS
        SELECT 
            user_id,
            DATE_TRUNC('month', recorded_at AT TIME ZONE {timezone_sql}) as usage_month,
            SUM(prompt_tokens) as prompt_tokens,
            SUM(completion_tokens) as completion_tokens,
            SUM(total_tokens) as total_tokens,
            COUNT(*) as request_count
        FROM usage_records
        GROUP BY user_id, DATE_TRUNC('month', recorded_at AT TIME ZONE {timezone_sql});

        -- Current usage summary (today + this month). Enumerates users tracked in
        -- the plugin's own usage_records table and derives the tier solely from
        -- native group membership via resolve_user_tier(). Per-user overrides win.
        CREATE VIEW usage_summary AS
        SELECT 
            tracked.user_id,
            array_to_string(r.tiers, ', ') as group_name,
            COALESCE(uo.daily_token_limit, r.daily_limit) as daily_token_limit,
            COALESCE(uo.monthly_token_limit, r.monthly_limit) as monthly_token_limit,
            COALESCE(daily.total_tokens, 0) as tokens_today,
            COALESCE(monthly.total_tokens, 0) as tokens_this_month,
            CASE 
                WHEN COALESCE(uo.daily_token_limit, r.daily_limit) = -1 THEN 100.0
                ELSE ROUND(COALESCE(daily.total_tokens, 0)::NUMERIC / NULLIF(COALESCE(uo.daily_token_limit, r.daily_limit), 0) * 100, 2)
            END as daily_percent_used,
            CASE 
                WHEN COALESCE(uo.monthly_token_limit, r.monthly_limit) = -1 THEN 100.0
                ELSE ROUND(COALESCE(monthly.total_tokens, 0)::NUMERIC / NULLIF(COALESCE(uo.monthly_token_limit, r.monthly_limit), 0) * 100, 2)
            END as monthly_percent_used
        FROM (SELECT DISTINCT user_id FROM usage_records) tracked
        CROSS JOIN LATERAL resolve_user_limits(tracked.user_id) r
        LEFT JOIN user_limit_overrides uo ON uo.user_id = tracked.user_id
        LEFT JOIN (
            SELECT user_id, SUM(total_tokens) as total_tokens
            FROM usage_records
            WHERE (recorded_at AT TIME ZONE {timezone_sql})::date = (NOW() AT TIME ZONE {timezone_sql})::date
            GROUP BY user_id
        ) daily ON tracked.user_id = daily.user_id
        LEFT JOIN (
            SELECT user_id, SUM(total_tokens) as total_tokens
            FROM usage_records
            WHERE recorded_at >= (DATE_TRUNC('month', NOW() AT TIME ZONE {timezone_sql}) AT TIME ZONE {timezone_sql})
            GROUP BY user_id
        ) monthly ON tracked.user_id = monthly.user_id;

        -- Users approaching their limits (>80% used)
        CREATE VIEW users_near_limit AS
        SELECT * FROM usage_summary
        WHERE daily_percent_used > 80 OR monthly_percent_used > 80
        ORDER BY monthly_percent_used DESC, daily_percent_used DESC;

        -- ============================================================================
        -- FUNCTIONS: Helper functions for the filter
        -- ============================================================================

        -- Record usage (simple insert wrapper). Group assignment for new users is
        -- now handled by native Open WebUI group membership (see
        -- get_user_usage_status), so this no longer auto-enrolls into user_groups.
        CREATE OR REPLACE FUNCTION record_usage(
            p_user_id VARCHAR(255),
            p_prompt_tokens INT,
            p_completion_tokens INT,
            p_model_id VARCHAR(255) DEFAULT NULL,
            p_pipeline_id VARCHAR(255) DEFAULT NULL,
            p_chat_id VARCHAR(255) DEFAULT NULL
        ) RETURNS VOID AS $$
        BEGIN
            INSERT INTO usage_records (user_id, prompt_tokens, completion_tokens, total_tokens, model_id, pipeline_id, chat_id)
            VALUES (p_user_id, p_prompt_tokens, p_completion_tokens, p_prompt_tokens + p_completion_tokens, p_model_id, p_pipeline_id, p_chat_id);
        END;
        $$ LANGUAGE plpgsql;

        -- Cleanup old records (run periodically)
        CREATE OR REPLACE FUNCTION cleanup_old_usage_records(days_to_keep INT DEFAULT 90)
        RETURNS INT AS $$
        DECLARE
            deleted_count INT;
        BEGIN
            DELETE FROM usage_records
            WHERE recorded_at < NOW() - (days_to_keep || ' days')::INTERVAL;
            GET DIAGNOSTICS deleted_count = ROW_COUNT;
            RETURN deleted_count;
        END;
        $$ LANGUAGE plpgsql;
        """

    def _ensure_schema(self, conn) -> bool:
        """
        Create tables/indexes if missing and (re)deploy views and functions.
        Table/index DDL uses IF NOT EXISTS and views/functions use CREATE OR
        REPLACE, so this always runs on every init - not just on first install -
        to guarantee the deployed SQL (e.g. tier-resolution logic) never goes
        stale relative to this file after a code change.
        Returns True if the tables were newly created, False if they already existed.
        """
        with conn.cursor() as cur:
            # Check if main table exists (for logging only)
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'usage_limits'
                )
            """)
            tables_existed = cur.fetchone()[0]

            schema_sql = self._get_schema_sql()
            cur.execute(schema_sql)
            conn.commit()

            if not tables_existed:
                self._log("✅ Usage tracking schema created successfully")
            else:
                self._log("Usage tracking schema/views/functions redeployed", "debug")
            return not tables_existed

    def _ensure_native_groups(self, conn):
        """
        Auto-create configured default tiers as native Open WebUI groups if they
        don't already exist, so membership can be managed from the standard Groups
        admin UI instead of a filter-specific table.
        """
        with conn.cursor() as cur:
            cur.execute(
                'SELECT name FROM "group" WHERE name = ANY(%s)',
                ([tier["group_name"] for tier in DEFAULT_TIERS],),
            )
            existing_names = {row[0] for row in cur.fetchall()}

            for tier in DEFAULT_TIERS:
                if tier["group_name"] in existing_names:
                    continue

                now = int(time.time())
                cur.execute(
                    """
                    INSERT INTO "group" (id, user_id, name, description, data, meta, permissions, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        "",
                        tier["group_name"],
                        tier["description"],
                        json.dumps(
                            {
                                "daily_token_limit": tier["daily_token_limit"],
                                "monthly_token_limit": tier["monthly_token_limit"],
                            }
                        ),
                        json.dumps({}),
                        json.dumps({}),
                        now,
                        now,
                    ),
                )
                self._log(f"Created native group '{tier['group_name']}'")
            conn.commit()

    def _enroll_users_without_default_tier(self, conn):
        """
        Put every user who has no default (built-in) tier group into the configured
        default group. Custom tier memberships do not count as a default
        tier. Runs on every plugin load and is a no-op when nobody is missing one.
        """
        tier_names = [tier["group_name"] for tier in DEFAULT_TIERS]
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id FROM "group" WHERE name = %s',
                (self.valves.default_group,),
            )
            row = cur.fetchone()
            if not row:
                self._log("Cannot enroll users in the default tier: group not found", "error")
                return
            default_group_id = row[0]

            now = int(time.time())
            cur.execute(
                """
                INSERT INTO group_member (id, group_id, user_id, created_at, updated_at)
                SELECT gen_random_uuid()::text, %s, u.id, %s, %s
                FROM "user" u
                WHERE NOT EXISTS (
                    SELECT 1 FROM group_member gm
                    JOIN "group" g ON g.id = gm.group_id
                    WHERE gm.user_id = u.id AND g.name = ANY(%s)
                )
                ON CONFLICT (group_id, user_id) DO NOTHING
                """,
                (default_group_id, now, now, tier_names),
            )
            enrolled = cur.rowcount
            conn.commit()
            if enrolled:
                self._log(
                    f"Enrolled {enrolled} user(s) with no default tier into "
                    f"{self.valves.default_group}"
                )

    async def _initialize(self):
        """Initialize once; the lock stops concurrent first prompts from racing the schema setup."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return
            try:
                await asyncio.to_thread(self._connect)
                self._initialized = True
                self._log("Usage tracking initialized successfully")
            except Exception as e:
                self._log(f"Failed to initialize: {e}", "error")
                raise

    def _connect(self):
        """Blocking: create the connection pool and run schema/group setup. Run via to_thread."""
        conn_string = (
            f"postgresql://{self.valves.postgres_user}:"
            f"{quote_plus(self.valves.postgres_password)}@"
            f"{self.valves.postgres_host}:{self.valves.postgres_port}/"
            f"{self.valves.postgres_database}"
        )

        self._log(
            f"Connecting to PostgreSQL at {self.valves.postgres_host}:{self.valves.postgres_port}"
        )

        # Create connection pool
        self._pool = psycopg_pool.ConnectionPool(
            conninfo=conn_string,
            min_size=1,
            max_size=5,
            open=True,
        )

        # Ensure schema exists (auto-create if needed). Each setup step is
        # isolated so a failure in one (e.g. native group tables not being
        # reachable) can't abort the connection pool setup and take the
        # whole filter - including the usage meter - down with it.
        with self._pool.connection() as conn:
            try:
                schema_created = self._ensure_schema(conn)
                if schema_created:
                    self._log("Schema was auto-created on first run")
                else:
                    self._log("Using existing schema", "debug")
            except Exception as e:
                conn.rollback()
                self._log(f"Schema setup failed: {e}", "error")

            # Ensure the native Open WebUI groups for each tier exist
            try:
                self._ensure_native_groups(conn)
            except Exception as e:
                conn.rollback()
                self._log(f"Native tier group setup failed: {e}", "error")

            # Everyone without a default tier joins the configured default group
            try:
                self._enroll_users_without_default_tier(conn)
            except Exception as e:
                conn.rollback()
                self._log(f"Default tier enrollment failed: {e}", "error")

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if n >= size:
                return f"{n/size:.1f}{suffix}"
        return str(n)

    @staticmethod
    def _tier_label(group_name: str) -> str:
        """Label for one tier or a comma separated list of every tier the user is in."""
        names = [
            n.strip().removeprefix(TIER_GROUP_PREFIX)
            for n in (group_name or "").split(",")
            if n.strip()
        ]
        return ", ".join(n.title() for n in names) if names else "Unknown"

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{max(minutes, 1)}m"

    def _get_reset_times(self, cur) -> Dict[str, float]:
        """Seconds until the daily/monthly windows roll over, per the DB clock/timezone
        (the same clock the usage queries use), so messages never disagree with enforcement."""
        timezone_sql = self._sql_string_literal(self.valves.usage_timezone)
        cur.execute(
            f"""
            SELECT
                EXTRACT(EPOCH FROM ((((NOW() AT TIME ZONE {timezone_sql})::date + 1)::timestamp AT TIME ZONE {timezone_sql}) - NOW())),
                EXTRACT(EPOCH FROM (((DATE_TRUNC('month', NOW() AT TIME ZONE {timezone_sql}) + INTERVAL '1 month') AT TIME ZONE {timezone_sql}) - NOW())),
                {timezone_sql}
            """
        )
        daily, monthly, tz = cur.fetchone()
        return {
            "daily_reset_seconds": float(daily),
            "monthly_reset_seconds": float(monthly),
            "timezone": tz,
        }

    def _get_user_status(self, user_id: str) -> Dict[str, Any]:
        """Get user's current usage status from PostgreSQL"""
        try:
            assert self._pool is not None
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM get_user_usage_status(%s)", (user_id,))
                    row = cur.fetchone()
                    resets = self._get_reset_times(cur)

                    if row:
                        return {
                            "group_name": row[0],
                            "daily_limit": row[1],
                            "monthly_limit": row[2],
                            "tokens_today": row[3],
                            "tokens_this_month": row[4],
                            "is_over_daily": row[5],
                            "is_over_monthly": row[6],
                            "limit_source": row[7],
                            **resets,
                        }

            raise RuntimeError("get_user_usage_status returned no row")

        except Exception as e:
            self._log(f"Failed to get user status (failing open): {e}", "error")
            return {
                "group_name": "unknown",
                "daily_limit": -1,
                "monthly_limit": -1,
                "tokens_today": 0,
                "tokens_this_month": 0,
                "is_over_daily": False,
                "is_over_monthly": False,
                "limit_source": "error",
                "daily_reset_seconds": 0.0,
                "monthly_reset_seconds": 0.0,
                "timezone": "",
            }

    def _record_usage(
        self,
        user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        model_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        """Record token usage to PostgreSQL"""
        try:
            assert self._pool is not None
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT record_usage(%s, %s, %s, %s, %s, %s)",
                        (
                            user_id,
                            prompt_tokens,
                            completion_tokens,
                            model_id,
                            pipeline_id,
                            chat_id,
                        ),
                    )
                conn.commit()

            total = prompt_tokens + completion_tokens
            self._log(f"Recorded {total} tokens for user {user_id[:8]}...", "debug")

        except Exception as e:
            self._log(f"Failed to record usage: {e}", "error")

    def _build_limit_message(self, status: Dict[str, Any]) -> Dict[str, str]:
        """Human-readable over-limit text. The daily limit is reported first, but if
        the monthly limit is also exhausted the reset time is the later (monthly) one."""
        if status["is_over_monthly"]:
            limit_type, limit_val, used_val = (
                "monthly",
                status["monthly_limit"],
                status["tokens_this_month"],
            )
            wait = self._fmt_duration(status["monthly_reset_seconds"])
        else:
            limit_type, limit_val, used_val = (
                "daily",
                status["daily_limit"],
                status["tokens_today"],
            )
            wait = self._fmt_duration(status["daily_reset_seconds"])

        message = (
            f"You've reached your {limit_type} usage limit "
            f"({used_val:,} of {limit_val:,} tokens, tier: {self._tier_label(status['group_name'])}). "
            f"New messages are paused and will be available again in about {wait}. "
            f"Contact your administrator if you need a higher limit."
        )
        return {"type": limit_type, "message": message, "wait": wait}

    async def inlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Inlet: Check user's usage against limits before processing.

        When the user is over a limit the request is rejected here by raising,
        BEFORE anything is sent to the model. Open WebUI turns the exception into
        an error banner on the assistant message plus a toast, so nothing is
        injected into the prompt and no tokens are spent.
        """
        self._log("=== INLET START ===", "debug")

        if not __user__ or not __user__.get("id"):
            self._log("No user ID, skipping usage check", "debug")
            return body

        user_id = __user__["id"]
        user_role = __user__.get("role", "user")
        is_admin = self.valves.admin_bypass and user_role == "admin"

        try:
            if not self._initialized:
                await self._initialize()
            status = await asyncio.to_thread(self._get_user_status, user_id)
        except Exception as e:
            self._log(f"Inlet error (failing open): {e}", "error")
            return body

        self._log(
            f"User {user_id[:8]}... tier={status['group_name']} "
            f"({status['limit_source']}): "
            f"{status['tokens_today']}/{status['daily_limit']} today, "
            f"{status['tokens_this_month']}/{status['monthly_limit']} this month, "
            f"over_daily={status['is_over_daily']} over_monthly={status['is_over_monthly']}",
            "debug",
        )

        try:
            if self.valves.show_usage_status and __event_emitter__:
                daily_percent = (
                    0
                    if status["daily_limit"] == -1
                    else status["tokens_today"] / max(status["daily_limit"], 1) * 100
                )
                monthly_percent = (
                    0
                    if status["monthly_limit"] == -1
                    else status["tokens_this_month"]
                    / max(status["monthly_limit"], 1)
                    * 100
                )
                fmt = self._fmt_tokens
                daily_limit = fmt(status["daily_limit"]) if status["daily_limit"] != -1 else "∞"
                monthly_limit = fmt(status["monthly_limit"]) if status["monthly_limit"] != -1 else "∞"
                is_warning = max(daily_percent, monthly_percent) >= self.valves.warn_at_percent
                icon = "⚠️" if is_warning else "📊"

                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": (
                                f"{icon} Token Usage: {fmt(status['tokens_today'])}/{daily_limit} Today "
                                f"({daily_percent:.0f}%) • {fmt(status['tokens_this_month'])}/"
                                f"{monthly_limit} Month ({monthly_percent:.0f}%) • "
                                f"Tier: {self._tier_label(status['group_name'])}"
                            ),
                            "done": True,
                        },
                    }
                )
        except Exception as e:
            self._log(f"Usage status emit failed: {e}", "warning")

        if status["is_over_daily"] or status["is_over_monthly"]:
            if self.valves.enable_blocking and not is_admin:
                info = self._build_limit_message(status)
                self._log(
                    f"BLOCK user {user_id[:8]}...: over {info['type']} limit "
                    f"(tier={status['group_name']})",
                    "info",
                )

                if __event_emitter__:
                    try:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {"type": "error", "content": info["message"]},
                            }
                        )
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": f"❌ {info['type'].title()} limit reached - resets in {info['wait']}",
                                    "done": True,
                                },
                            }
                        )
                    except Exception as e:
                        self._log(f"Block notification emit failed: {e}", "warning")

                # Aborts the turn; raised outside any try/except so it is not swallowed.
                raise Exception(info["message"])
            elif is_admin:
                self._log(f"Admin {user_id[:8]}... over limit, bypassing block", "debug")
            else:
                self._log(f"User {user_id[:8]}... over limit but blocking disabled", "warning")

        self._log("=== INLET COMPLETE ===", "debug")
        return body

    @staticmethod
    def _extract_usage(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find usage for the message produced by THIS turn only. Falling back to an
        older assistant message would re-record (double count) a previous turn's tokens."""
        message_id = body.get("id")
        messages = body.get("messages") or []
        target = None
        if message_id:
            target = next((m for m in messages if m.get("id") == message_id), None)
        if target is None:
            target = next(
                (m for m in reversed(messages) if m.get("role") == "assistant"), None
            )
        if target and target.get("usage"):
            return target["usage"]
        return body.get("usage") or None

    async def outlet(
        self,
        body: Dict[str, Any],
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """
        Outlet: Record token usage from the response
        """
        self._log("=== OUTLET START ===", "debug")

        user_id = (__user__ or {}).get("id")
        if not user_id:
            self._log("No user ID in outlet, skipping", "debug")
            return body

        try:
            if not self._initialized:
                await self._initialize()

            usage = self._extract_usage(body)
            if not usage:
                self._log("No usage data in response", "debug")
                return body

            # Open WebUI normalizes usage to input/output_tokens but keeps the
            # provider's original keys (OpenAI prompt/completion, Ollama eval_count).
            prompt_tokens = int(
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("prompt_eval_count")
                or 0
            )
            completion_tokens = int(
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("eval_count")
                or 0
            )
            if prompt_tokens == 0 and completion_tokens == 0:
                total = int(usage.get("total_tokens") or 0)
                completion_tokens = total

            if prompt_tokens <= 0 and completion_tokens <= 0:
                self._log("Usage found but no tokens to record", "debug")
                return body

            await asyncio.to_thread(
                self._record_usage,
                user_id=user_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model_id=body.get("model"),
                chat_id=body.get("chat_id"),
            )
            self._log(
                f"Recorded {prompt_tokens + completion_tokens} tokens for user {user_id[:8]}...",
                "info",
            )

            status = await asyncio.to_thread(self._get_user_status, user_id)
            daily_percent = (
                0
                if status["daily_limit"] == -1
                else status["tokens_today"] / max(status["daily_limit"], 1) * 100
            )
            monthly_percent = (
                0
                if status["monthly_limit"] == -1
                else status["tokens_this_month"] / max(status["monthly_limit"], 1) * 100
            )
            is_over = status["is_over_daily"] or status["is_over_monthly"]
            is_near = max(daily_percent, monthly_percent) >= self.valves.warn_at_percent

            if (is_over or is_near) and __event_emitter__:
                if is_over:
                    info = self._build_limit_message(status)
                    notif_type, notif_msg = "error", info["message"]
                else:
                    fmt = self._fmt_tokens
                    daily_limit = fmt(status["daily_limit"]) if status["daily_limit"] != -1 else "∞"
                    monthly_limit = fmt(status["monthly_limit"]) if status["monthly_limit"] != -1 else "∞"
                    notif_type = "warning"
                    notif_msg = (
                        f"Approaching usage limit. Today: {fmt(status['tokens_today'])}/{daily_limit} "
                        f"({daily_percent:.0f}%), this month: {fmt(status['tokens_this_month'])}/"
                        f"{monthly_limit} ({monthly_percent:.0f}%)."
                    )
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {"type": notif_type, "content": notif_msg},
                    }
                )
                self._log(f"Sent usage notification to user {user_id[:8]}...", "info")

        except Exception as e:
            self._log(f"Outlet error: {e}", "error")

        self._log("=== OUTLET COMPLETE ===", "debug")
        return body
