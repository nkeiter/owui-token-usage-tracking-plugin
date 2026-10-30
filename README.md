# Gettysburg Usage Tracking Filter for Open WebUI

This project is Gettysburg College's fork of Beau D'Amore's MIT-licensed
[Open WebUI Usage Tracking Filter](https://github.com/beaudamore/openwebui-usage-tracking-filter).
Fork developer: Nathan I Keiter.
It tracks token usage in PostgreSQL, applies daily and monthly limits, and uses
native Open WebUI groups to determine each user's tier. Deployment-specific
settings are configurable so other organizations can adapt the plugin to meet
thier specific needs.

The plugin metadata declares Open WebUI `>= 0.12.4` and the dependencies
`psycopg[binary]` and `psycopg-pool`.

The plugin has been tested with the Open WebUI version requirement declared in
its metadata (`required_open_webui_version`). Open WebUI will not run a plugin
when the installed version is lower than that requirement. If you need to try
the plugin on a different deployment version, you may manually change
`required_open_webui_version` in the plugin metadata to match your version, but
do so at your own risk; compatibility with that version is not guaranteed.

## Fork changes

- Uses native Open WebUI group memberships (`group` and `group_member`) to
  determine tier; it does not use the filter's legacy `user_groups` table for
  tier assignment.
- Creates missing native groups listed in `DEFAULT_TIERS`. On initialization
  (the first prompt after an Open WebUI restart or filter update), it enrolls
  each existing user without a built-in tier group in the configured default
  group. Custom tier memberships do not count as a built-in tier. Users created
  later with no tier membership resolve to the configured default tier until
  the next initialization.
- **One default tier, any number of custom tiers.** The groups in
  `DEFAULT_TIERS` are *default* tiers: a user has one (if several are present the most recently
  added membership wins; ties follow their order in `DEFAULT_TIERS`) and
  no default membership and no custom tiers means `DEFAULT_GROUP`. Every other
  group whose name starts with `TIER_GROUP_PREFIX` is a *custom* tier and a
  user may be in any number of them. A user in custom tiers is allowed the
  **sum** of their daily and monthly limits (the default tier is not included;
  any custom tier with `-1` makes that limit unlimited for that period).
  Example: two custom tiers of 500K/day, 5M/month give 1M/day, 10M/month.
  A per-user override still wins over everything.
- The status line above the prompt lists every tier the user is in
  (for example, `Tier: Pro, Department-Project`; every word of each tier name
  is capitalized).
- Version 1.4.0 changed `resolve_user_tier()` to return only the default tier
  and added `resolve_user_limits()`; `get_user_usage_status()` now returns a
  comma-separated `group_name`.
- Supports custom tiers: any native group named with the configured
  `TIER_GROUP_PREFIX` that has a
  `usage_limits` row is a tier. The tier lookup is generic, so custom tiers
  need no filter changes. Create and delete them with the management options
  below.
- Supports per-user daily and monthly limit overrides without changing group
  membership.
- Checks limits before a prompt is sent to a model, records response usage, and
  handles limit messaging in Open WebUI rather than adding messages to the
  model prompt.
- Runs synchronous PostgreSQL work via `asyncio.to_thread` and serializes first
  initialization to avoid blocking the event loop and duplicate schema setup.
- Displays token usage with K/M/B units and the user's tier. Finished status
  messages use Open WebUI's non-shimmer styling.

## Tiers and limits

| Native Open WebUI group | Daily limit | Monthly limit |
| --- | ---: | ---: |
| `token-limit-enterprise` | Unlimited | Unlimited |
| `token-limit-pro` | 1,000,000 tokens | 10,000,000 tokens |
| `token-limit-standard` | 500,000 tokens | 5,000,000 tokens |
| `token-limit-test` | 1,000 tokens | 5,000 tokens |

Daily and monthly usage windows use the configured IANA timezone (the default
is `America/New_York`). Tier quotas are stored in the filter's `usage_limits`
table. The filter seeds missing rows but does not overwrite existing values;
to change a tier's quota for an existing deployment, update its row in
`usage_limits`.

The default tiers above are configured in `DEFAULT_TIERS`. **Custom tiers**
can be added: a custom tier is a native Open WebUI group named
`<TIER_GROUP_PREFIX><name>` (the prefix keeps it apart from ordinary groups)
plus a `usage_limits` row.
Deleting a custom tier returns its members (who are in no other tier) to the
configured default; members of other tiers keep them. Updated filter code must
be loaded into Open WebUI before custom tiers are enforced; older versions ignore them, and because the filter
redeploys the tier SQL functions on its first prompt after each Open WebUI
restart, an old filter will revert the database function and custom tiers
with it. The management console can update the database function itself
(banner / automatic on tier create and bulk add) but cannot load the filter
into Open WebUI, so keep the pasted filter current.

## Install and configure

1. In Open WebUI, open the Admin Panel's Functions area, create a Filter, and
   paste in [`filter/usage_tracking_filter.py`](filter/usage_tracking_filter.py).
2. Save and enable it. Set its priority lower than filters that should run
   after it (the default is `5`).
3. Configure the PostgreSQL valves to connect to the PostgreSQL database used
   by Open WebUI. The filter needs access to Open WebUI's native `user`, `group`,
   and `group_member` tables, as well as permission to create and maintain its
   usage tables, views, and functions.
4. Review the behavior valves: `enable_blocking` (default enabled),
   `show_usage_status` (default enabled), `warn_at_percent` (default `80`),
   `admin_bypass` (default enabled), and `debug_mode` (default disabled).

The PostgreSQL connection valve defaults are generic placeholders, not
production credentials. Set the correct host, database, username, and password
for your deployment; never commit real credentials.

On first prompt, the filter initializes its schema and native tier groups.
Schema setup is repeated on initialization so its views and functions stay in
sync with the filter code. Existing tier limits are preserved.

## Tier management and overrides

Manage tier membership through Open WebUI's native Groups interface, or through
an administrative console that edits those native group memberships. Add a
user to the intended configured tier group. Replace their default tier by
moving them between built-in groups; custom tier groups can simply be added
(their limits sum). If a user belongs to several built-in groups, the newest
membership is their default tier.

The optional `user_limit_overrides` table changes a user's daily/monthly quota
only; it does not assign or replace their tier. The legacy `user_groups` table
is retained in the schema for compatibility, but this filter does not use it
to resolve tier membership.

### Management options

1. **PostgreSQL client (default):** Manage native tier-group membership and
   adjust tier limits or per-user overrides with PostgreSQL queries. This is
   the built-in management approach and requires appropriate database access.
2. **Open WebUI Groups admin UI:** Manage membership in the built-in default
   token-limit groups through the standard Groups admin UI; this plugin uses
   native Open WebUI groups to determine each user's tier. Custom tier groups
   using the `token-limit-` prefix can also be managed there, but creating a
   prefixed group in the UI alone does not give it usage limits. A custom tier
   must also have a corresponding row in the plugin's `usage_limits` table,
   with its daily and monthly limits configured. Create and configure custom
   tiers through the database or one of the management tools below.
3. **`owui_token_usage_admin.py` CLI (improved):** Provides commands for assigning a
   user to exactly one tier, managing limits and overrides, and reviewing
   usage. It is currently run from an AWS container shell. Its commands are:
   `list-limits` (show tier limits and overrides), `list-members` (show tier
   members and optionally their usage details), `set-group-limit`,
   `set-all-limits`, `assign-user` (a built-in tier replaces the default tier; a custom tier is
   added), `remove-user` (remove from one tier), `create-tier` and `delete-tier` (custom
   tiers using the configured prefix; members return to the configured default),
   `add-users` (bulk-add
   by `--email`, `--user-id` or any-layout `--csv` (header optional), with `--dry-run`; users
   that do not exist are created first), `import-tiers` (create several
   custom tiers with limits and members from one CSV, with a summary and
   `--dry-run`), `set-user-limit`, `clear-user-limit`, `show-user`,
   `audit-tiers` (find users in several or no default tier groups), and `usage-report`
   (report usage against quotas). This CLI is maintained separately and is not
   part of this filter repository.
4. **Usage management console (advanced):** A graphical interface for tier
   membership, limits, overrides, and reports, hosted as a small AWS Lambda
   website. Its pages include a dashboard; user search, tier assignment and
   override management; tier management (edit limits, **Add New** custom
   tiers, **Add Multiple** tiers from one CSV with a preview, delete custom
   tiers, and bulk **Add Users** to any tier by search or any-layout CSV
   upload, creating missing users with role `user` for SSO sign-in);
   date-range and optional per-user reports with charts and CSV export; and a
   tier audit for users in several or no default tier groups. Navigate with
   `/#dashboard`, `/#users`, `/#tiers`, `/#reports`, and `/#tier-audit`. The
   console is maintained separately and is not part of this filter repository.

The CLI and console currently require AWS hosting/integration, AWS Secrets
Manager access for the database connection secret, and environment-variable
configuration. The CLI can be adapted to run outside AWS with appropriate
changes to its database-connection and secret configuration.

## Behavior and reporting

### Limit resolution

- **No assigned tier:** A user with no tier membership resolves to
  `default_group` and gets that tier's limits. During initialization, existing
  users without any built-in tier membership are enrolled in that group. A user
  with only custom tiers still gets the custom-tier limits described below;
  initialization also enrolls them in the default group for consistent group
  administration.
- **One default tier:** A user in one group from `DEFAULT_TIERS` gets that
  tier's daily and monthly limits, unless they also belong to one or more
  custom tiers. If the user belongs to multiple default tiers, the most recently
  added membership wins; ties follow the order of `DEFAULT_TIERS`.
- **Multiple custom tiers:** A user's custom-tier daily limits are summed, and
  their custom-tier monthly limits are summed separately. The default-tier
  limits are not added to these totals. A `-1` limit in any custom tier makes
  the corresponding combined limit unlimited. Per-user overrides take
  precedence over the resolved tier limits.

Custom tiers are native groups whose names start with `TIER_GROUP_PREFIX` and
have a corresponding row in `usage_limits`. A custom group without that row
does not contribute to the user's limits.

- Before a request, the filter checks the user's current usage and emits a
  finished status line such as `Token Usage: 5.0K/500.0K Today (1%) •
  6.7K/5.0M Month (0%) • Tier: Standard`.
- If blocking is enabled and the user has reached a daily or monthly limit,
  the filter emits a notification and rejects the request before it reaches
  the model. Admins bypass blocking when `admin_bypass` is enabled. Disabling
  blocking leaves usage tracking active and logs that the limit was exceeded.
- After a response, the filter extracts token usage from Open WebUI's normalized
  `input_tokens`/`output_tokens` fields or supported provider-specific fields,
  records it, and sends an approaching-limit or over-limit notification when
  appropriate.
- Usage views include `usage_daily`, `usage_monthly`, `usage_summary`, and
  `users_near_limit`. They report token totals and are useful for administrative
  reporting.
- Usage records include the Open WebUI user UUID, timestamps, token counts,
  model ID, and chat ID. Although the schema has a pipeline ID column, this
  filter currently does not populate it. The filter does not store prompt or
  response content, user names, or email addresses.
- Usage records are not automatically purged. An administrator can remove old
  records by running `SELECT cleanup_old_usage_records(90);` with an appropriate
  database account; the argument is the retention period in days.

Database failures are logged. The filter is designed to fail open when it
cannot retrieve a user's status, so operators should monitor the Open WebUI
logs and database connectivity to ensure limits are being enforced.

## License and attribution

This fork is distributed under the MIT License. The LICENSE file retains
Beau D'Amore's original copyright notice and adds a separate notice for Nathan
I Keiter's copyrightable contributions to this fork. Copyright is not claimed
over the original author's work.

## Configuration reference

The following module-level values in
[`filter/usage_tracking_filter.py`](filter/usage_tracking_filter.py) are intended
for deployment customization:

| Variable | Purpose |
| --- | --- |
| `PLUGIN_NAME` | Name shown for the filter in Open WebUI. |
| `TIER_GROUP_PREFIX` | Prefix that identifies native Open WebUI groups as custom usage tiers. |
| `DEFAULT_GROUP` | Built-in tier used when a user has no default-tier membership; must match a `group_name` in `DEFAULT_TIERS`. |
| `DEFAULT_USAGE_TIMEZONE` | Default IANA timezone for daily/monthly quota windows; can be overridden by the `usage_timezone` valve. |
| `DEFAULT_TIERS` | Built-in tier definitions and their `group_name`, daily and monthly token limits, and descriptions. The list order also breaks ties between memberships added at the same time. |

`DEFAULT_TIERS` group names should be unique. For a deployment with an
existing database, changing tier names does not rename existing Open WebUI
groups or `usage_limits` rows; migrate those records and memberships as needed.
Existing quota rows are intentionally not overwritten by startup seeding.

The following Open WebUI valves can be set for each filter installation:

| Valve | Purpose |
| --- | --- |
| `priority` | Filter execution order; set lower than filters that should run after the usage check. |
| `postgres_host` | PostgreSQL host reachable from Open WebUI. |
| `postgres_port` | PostgreSQL server port. |
| `postgres_database` | Database containing Open WebUI's native user/group tables and the plugin's usage schema. |
| `postgres_user` | PostgreSQL account used by the filter. |
| `postgres_password` | Password for that account; configure securely in Open WebUI and do not commit credentials. |
| `default_group` | Built-in tier used when no default tier is assigned; must match a `group_name` in `DEFAULT_TIERS`. |
| `usage_timezone` | IANA timezone used for daily/monthly accounting and reset times. |
| `enable_blocking` | Whether requests at/over a limit are rejected; disabling it retains metering and warnings. |
| `show_usage_status` | Whether to show the user's usage status above the prompt. |
| `warn_at_percent` | Usage percentage at which near-limit warnings begin. |
| `admin_bypass` | Whether users with the Open WebUI admin role bypass limit blocking. |
| `debug_mode` | Whether debug-level filter messages are emitted. |

## Disclaimer

The software is provided “AS IS,” without warranty of any kind, as stated in
the MIT License. To the fullest extent permitted by applicable law, Gettysburg
College, its trustees, officers, employees, and contributors disclaim liability
for claims, damages, or other liability arising from, out of, or in connection
with the software or its use. Use and deploy the plugin at your own risk;
operators are responsible for validating its behavior, security, database
permissions, and compliance with their policies. This notice supplements but
does not replace the MIT License. Have counsel review the disclaimer for your
jurisdiction if a specific legal outcome is required.
