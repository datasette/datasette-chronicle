from datasette import hookimpl, Response, NotFound
from datasette.filters import FilterArguments
from datasette.permissions import Action, PermissionSQL
from datasette.resources import TableResource
from datasette.utils import tilde_encode, tilde_decode
import sqlite_chronicle
import datetime
import urllib.parse

# Keep track of which upgrades have run
upgrade_has_run = set()


async def upgrade_database(datasette, database):
    db = datasette.get_database(database)
    # Check if the _chronicle_ tables exist
    table_names = await db.table_names()
    for table in table_names:
        if table.startswith("_chronicle_"):
            # Upgrade the existing chronicle table
            other_table = table[len("_chronicle_") :]
            await db.execute_write_fn(
                lambda conn: sqlite_chronicle.upgrade_chronicle(conn, other_table)
            )


@hookimpl
def table_actions(datasette, actor, database, table):
    async def inner():
        if database not in upgrade_has_run:
            upgrade_has_run.add(database)
            await upgrade_database(datasette, database)

        if table.startswith("_chronicle_"):
            return []

        # First check if table is enabled or not
        db = datasette.get_database(database)
        view_names = await db.view_names()
        if table in view_names:
            return None
        chronicle_table = "_chronicle_{}".format(table)
        if await db.table_exists(chronicle_table):
            # Table exists — always link to the timeline (no special permission needed
            # beyond being able to view the table, which is implied by reaching here)
            actions = [
                {
                    "href": datasette.urls.path(
                        "/-/chronicle/timeline/{}/{}".format(
                            database, tilde_encode(table)
                        )
                    ),
                    "label": "View chronicle timeline",
                    "description": "Browse a timeline of row-level changes for this table",
                }
            ]
            if await datasette.allowed(
                action="disable-chronicle",
                resource=TableResource(database=database, table=table),
                actor=actor,
            ):
                actions.append(
                    {
                        "href": datasette.urls.path(
                            "/-/disable-chronicle/{}/{}".format(database, table)
                        ),
                        "label": "Disable row version tracking for this table",
                        "description": "Remove the associated triggers and table",
                    }
                )
            return actions
        else:
            # Table doesn't exist, so it's disabled
            if await datasette.allowed(
                action="enable-chronicle",
                resource=TableResource(database=database, table=table),
                actor=actor,
            ):
                if not await db.primary_keys(table):
                    return None
                return [
                    {
                        "href": datasette.urls.path(
                            "/-/enable-chronicle/{}/{}".format(database, table)
                        ),
                        "label": "Enable row version tracking for this table",
                        "description": "Track a version number and added/updated time for each row",
                    }
                ]

    return inner


@hookimpl
def database_actions(datasette, actor, database, request):
    async def inner():
        db = datasette.get_database(database)
        table_names = await db.table_names()
        # Find chronicle tables whose corresponding original table the actor can view
        for t in table_names:
            if not t.startswith("_chronicle_") or t == "_chroniclesnapshots":
                continue
            original = t[len("_chronicle_"):]
            if await datasette.allowed(
                action="view-table",
                resource=TableResource(database=database, table=original),
                actor=actor,
            ):
                return [
                    {
                        "href": datasette.urls.path(
                            "/-/chronicle/timeline/{}".format(database)
                        ),
                        "label": "Chronicle timeline",
                        "description": "Browse a timeline of row-level changes across all tracked tables",
                    }
                ]
        return []

    return inner


@hookimpl
def menu_links(datasette, actor, request):
    async def inner():
        # Check if any database has chronicle tables before adding the menu item
        for db_name in datasette.databases:
            db = datasette.get_database(db_name)
            try:
                table_names = await db.table_names()
            except Exception:
                continue
            if any(
                t.startswith("_chronicle_") and t != "_chroniclesnapshots"
                for t in table_names
            ):
                return [
                    {
                        "href": datasette.urls.path("/-/chronicle/timeline"),
                        "label": "Chronicle timeline",
                    }
                ]
        return []

    return inner


@hookimpl
def register_routes():
    return [
        (
            r"^/-/enable-chronicle/(?P<database>[^/]+)/(?P<table>[^/]+)$",
            enable_chronicle,
        ),
        (
            r"^/-/disable-chronicle/(?P<database>[^/]+)/(?P<table>[^/]+)$",
            disable_chronicle,
        ),
        (
            r"^/-/chronicle/timeline$",
            chronicle_timeline_index,
        ),
        (
            r"^/-/chronicle/timeline/(?P<database>[^/]+)$",
            chronicle_timeline,
        ),
        (
            r"^/-/chronicle/timeline/(?P<database>[^/]+)/(?P<table>[^/]+)$",
            chronicle_table_timeline,
        ),
    ]


async def enable_chronicle(datasette, request):
    database = request.url_vars["database"]
    table = request.url_vars["table"]
    db = datasette.get_database(database)
    chronicle_table = "_chronicle_{}".format(table)
    if await db.table_exists(chronicle_table):
        # Table exists, so it's already enabled
        datasette.add_message(
            request,
            "row version tracking is already enabled for {}".format(table),
            datasette.WARNING,
        )
        return Response.redirect(datasette.urls.table(database, table))

    # It must have primary keys
    pks = await db.primary_keys(table)
    if not pks:
        datasette.add_message(
            request,
            "Cannot enable row version tracking for {} because it has no primary keys".format(
                table
            ),
            datasette.ERROR,
        )
        return Response.redirect(datasette.urls.table(database, table))

    if request.method == "POST":

        def enable(conn):
            sqlite_chronicle.enable_chronicle(conn, table)

        await db.execute_write_fn(enable)
        datasette.add_message(
            request,
            "row version tracking enabled for {}".format(table),
            datasette.INFO,
        )
        return Response.redirect(datasette.urls.table(database, table))
    else:
        # Show confirmation screen
        return Response.html(
            await datasette.render_template(
                "enable-chronicle.html",
                {
                    "database": database,
                    "table": table,
                    "pks": pks,
                    "action": datasette.urls.path(
                        "/-/enable-chronicle/{}/{}".format(database, table)
                    ),
                },
                request=request,
            )
        )


async def disable_chronicle(datasette, request):
    database = request.url_vars["database"]
    table = request.url_vars["table"]
    db = datasette.get_database(database)
    chronicle_table = "_chronicle_{}".format(table)
    if not await db.table_exists(chronicle_table):
        # Table doesn't exist, so it's disabled
        datasette.add_message(
            request,
            "row version tracking is already disabled for {}".format(table),
            datasette.WARNING,
        )
        return Response.redirect(datasette.urls.table(database, table))

    if request.method == "POST":

        def disable(conn):
            conn.execute('DROP TABLE "{}"'.format(chronicle_table))
            # And remove the triggers
            for trigger in (
                "chronicle_{}_ai".format(table),
                "chronicle_{}_ad".format(table),
                "chronicle_{}_au".format(table),
            ):
                conn.execute('DROP TRIGGER "{}"'.format(trigger))

        await db.execute_write_fn(disable)
        datasette.add_message(
            request,
            "row version tracking disabled for {}".format(table),
            datasette.INFO,
        )
        return Response.redirect(datasette.urls.table(database, table))
    else:
        return Response.html(
            await datasette.render_template(
                "disable-chronicle.html",
                {
                    "database": database,
                    "table": table,
                    "action": datasette.urls.path(
                        "/-/disable-chronicle/{}/{}".format(database, table)
                    ),
                },
                request=request,
            )
        )


@hookimpl
def register_actions(datasette):
    return [
        Action(
            name="enable-chronicle",
            description="Enable row version tracking for a table",
            resource_class=TableResource,
        ),
        Action(
            name="disable-chronicle",
            description="Disable row version tracking for a table",
            resource_class=TableResource,
        ),
    ]


@hookimpl
def permission_resources_sql(datasette, actor, action):
    if action not in ("enable-chronicle", "disable-chronicle"):
        return None
    if not actor or actor.get("id") != "root":
        return None
    # Root user is allowed on any table for these actions
    return PermissionSQL(
        sql="SELECT NULL AS parent, NULL AS child, 1 AS allow, 'root' AS reason",
        params={},
        source="datasette-chronicle",
    )


def _parse_version_range(value):
    """Parse a 'MIN-MAX' version range string. Returns (min, max) ints or None."""
    if value is None:
        return None
    parts = value.split("-", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


@hookimpl
def filters_from_request(request, datasette, database, table):
    since = request.args.get("_since")
    added_range = request.args.get("_chronicle_added_range")
    updated_range = request.args.get("_chronicle_updated_range")

    if since is None and added_range is None and updated_range is None:
        return

    if table.startswith("_chronicle_"):
        return

    async def inner():
        db = datasette.get_database(database)
        chronicle_table = "_chronicle_{}".format(table)
        if not await db.table_exists(chronicle_table):
            return None
        pks = ", ".join('"{}"'.format(pk) for pk in await db.primary_keys(table))
        ct = chronicle_table.replace('"', '""')

        extra_wheres = []
        params = {}
        descriptions = []

        if since is not None:
            extra_wheres.append(
                f'({pks}) in (select {pks} from "{ct}" where __version > :chronicle_since)'
            )
            params["chronicle_since"] = since
            descriptions.append("modified since version {}".format(since))

        parsed_added = _parse_version_range(added_range)
        if parsed_added is not None:
            v_min, v_max = parsed_added
            extra_wheres.append(
                f'({pks}) in (select {pks} from "{ct}"'
                ' where __version between :chronicle_added_min and :chronicle_added_max'
                ' and __added_ms = __updated_ms)'
            )
            params["chronicle_added_min"] = v_min
            params["chronicle_added_max"] = v_max
            descriptions.append("added in versions {}-{}".format(v_min, v_max))

        parsed_updated = _parse_version_range(updated_range)
        if parsed_updated is not None:
            v_min, v_max = parsed_updated
            extra_wheres.append(
                f'({pks}) in (select {pks} from "{ct}"'
                ' where __version between :chronicle_updated_min and :chronicle_updated_max'
                ' and __added_ms != __updated_ms and __deleted = 0)'
            )
            params["chronicle_updated_min"] = v_min
            params["chronicle_updated_max"] = v_max
            descriptions.append("updated in versions {}-{}".format(v_min, v_max))

        if not extra_wheres:
            return None
        return FilterArguments(extra_wheres, params, human_descriptions=descriptions)

    return inner


# --- Timeline ---

_CLUSTER_MS = 5 * 60 * 1000  # 5 minutes: changes within this window are grouped
_PER_TABLE_LIMIT = 1001       # fetch this many rows per table; 1001 detects overflow
_PREVIEW_COLS = 10            # max columns shown in single-row preview
_TRUNCATE_LEN = 80            # truncate long string values to this length


def _build_timeline_sql(tables, version_cursors, table_pks=None):
    """
    Return a SQL string that UNIONs the most recent _PER_TABLE_LIMIT rows from
    each chronicle table, sorted newest-first by __updated_ms.

    version_cursors is a dict of {table_name: max_version_exclusive} used for
    pagination: only rows with __version < cursor are included for that table.

    table_pks is a dict of {table_name: [pk_col, ...]}. When provided, each
    row includes a _pk_json column containing a JSON object of PK name->value,
    which is used for single-row inflation without a second chronicle query.
    """
    parts = []
    for table in tables:
        cursor_clause = ""
        if table in version_cursors:
            # safe: version_cursors values are always integers (validated on parse)
            cursor_clause = "AND __version < %d" % version_cursors[table]
        escaped = table.replace('"', '""')
        # Build a JSON object of PK column name->value pairs
        pks = (table_pks or {}).get(table, [])
        if pks:
            pk_args = ", ".join(
                "'%s', \"%s\"" % (pk.replace("'", "''"), pk.replace('"', '""'))
                for pk in pks
            )
            pk_json_expr = "json_object(%s)" % pk_args
        else:
            pk_json_expr = "json('{}')"
        parts.append(
            'SELECT "%(t)s" AS _table,'
            " __version, __added_ms, __updated_ms, __deleted,"
            " %(pk_json)s AS _pk_json"
            ' FROM "_chronicle_%(t)s"'
            " WHERE 1=1 %(cursor)s"
            " ORDER BY __version DESC"
            " LIMIT %(limit)d"
            % dict(
                t=escaped,
                cursor=cursor_clause,
                pk_json=pk_json_expr,
                limit=_PER_TABLE_LIMIT,
            )
        )
    union = " UNION ALL ".join("SELECT * FROM (%s)" % p for p in parts)
    return "SELECT * FROM (%s) ORDER BY __updated_ms DESC" % union


def _cluster_rows(rows):
    """
    Group a time-sorted (newest-first) list of chronicle rows into clusters.

    A cluster is a run of consecutive rows that share the same table name and
    whose __updated_ms values are all within _CLUSTER_MS of each other.
    Returns a list of cluster dicts (newest-first).
    """
    clusters = []
    current = None
    for row in rows:
        table = row["_table"]
        ts = row["__updated_ms"]
        is_added = row["__added_ms"] == row["__updated_ms"]
        is_deleted = bool(row["__deleted"])

        if (
            current is None
            or current["table"] != table
            or current["newest_ms"] - ts > _CLUSTER_MS
        ):
            current = {
                "table": table,
                "newest_ms": ts,
                "oldest_ms": ts,
                "rows": [],
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "min_version": row["__version"],
                "max_version": row["__version"],
                "added_min_version": None,
                "added_max_version": None,
                "updated_min_version": None,
                "updated_max_version": None,
            }
            clusters.append(current)

        current["oldest_ms"] = ts
        v = row["__version"]
        current["min_version"] = min(current["min_version"], v)
        current["max_version"] = max(current["max_version"], v)
        current["rows"].append(dict(row))
        if is_deleted:
            current["deleted"] += 1
        elif is_added:
            current["added"] += 1
            current["added_min_version"] = min(current["added_min_version"], v) if current["added_min_version"] is not None else v
            current["added_max_version"] = max(current["added_max_version"], v) if current["added_max_version"] is not None else v
        else:
            current["updated"] += 1
            current["updated_min_version"] = min(current["updated_min_version"], v) if current["updated_min_version"] is not None else v
            current["updated_max_version"] = max(current["updated_max_version"], v) if current["updated_max_version"] is not None else v

    return clusters


def _format_time(ts_ms):
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%H:%M UTC")


def _format_day(ts_ms):
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%A, %-d %B %Y")


def _day_key(ts_ms):
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%Y-%m-%d")


def _truncate(value, length=_TRUNCATE_LEN):
    s = str(value) if value is not None else ""
    if len(s) > length:
        return s[:length] + "\u2026"
    return s


async def _inflate_row(db, table, pks, pk_values):
    """Fetch the actual row from the original table by primary key."""
    where = " AND ".join('"%(pk)s" = ?' % {"pk": pk.replace('"', '""')} for pk in pks)
    escaped = table.replace('"', '""')
    sql = 'SELECT * FROM "%s" WHERE %s LIMIT 1' % (escaped, where)
    results = await db.execute(sql, pk_values)
    rows = results.rows
    if not rows:
        return None, None
    columns = [d[0] for d in results.description]
    return columns, rows[0]


async def chronicle_timeline_index(datasette, request):
    """
    Landing page: list every database that has at least one chronicle-enabled
    table. If there is only one such database redirect straight to it.
    """
    databases_with_chronicle = []
    for db_name in datasette.databases:
        db = datasette.get_database(db_name)
        try:
            table_names = await db.table_names()
        except Exception:
            continue
        has_chronicle = any(
            t.startswith("_chronicle_") and t != "_chroniclesnapshots"
            for t in table_names
        )
        if has_chronicle:
            databases_with_chronicle.append(db_name)

    if len(databases_with_chronicle) == 1:
        return Response.redirect(
            datasette.urls.path(
                "/-/chronicle/timeline/%s" % databases_with_chronicle[0]
            )
        )

    return Response.html(
        await datasette.render_template(
            "chronicle-timeline-index.html",
            {"databases": databases_with_chronicle},
            request=request,
        )
    )


async def chronicle_timeline(datasette, request):
    database = request.url_vars["database"]
    db = datasette.get_database(database)

    # Run upgrade once per database (same as table_actions hook)
    if database not in upgrade_has_run:
        upgrade_has_run.add(database)
        await upgrade_database(datasette, database)

    # Find all chronicle-enabled tables
    table_names = await db.table_names()
    chronicle_tables = sorted(
        t[len("_chronicle_"):]
        for t in table_names
        if t.startswith("_chronicle_") and t != "_chroniclesnapshots"
    )

    if not chronicle_tables:
        return Response.html(
            await datasette.render_template(
                "chronicle-timeline.html",
                {
                    "database": database,
                    "chronicle_tables": [],
                    "days": [],
                    "has_more": False,
                    "next_page_url": None,
                },
                request=request,
            )
        )

    # Parse per-table version cursors from query string
    # URL params: ?cursor_TABLENAME=VERSION
    version_cursors = {}
    for table in chronicle_tables:
        param = "cursor_" + table
        raw = request.args.get(param)
        if raw is not None:
            try:
                version_cursors[table] = int(raw)
            except ValueError:
                pass

    # Tables exhausted on a previous page are tracked via ?exhausted=table1,table2
    exhausted_param = request.args.get("exhausted", "")
    exhausted_tables = set(exhausted_param.split(",")) if exhausted_param else set()

    # Only query tables that are not exhausted
    active_tables = [t for t in chronicle_tables if t not in exhausted_tables]

    if not active_tables:
        days_out = []
        has_more = False
        next_page_url = None
    else:
        # Cache primary keys per table before building SQL (needed for _pk_json)
        import json as _json
        from collections import Counter
        table_pks = {}
        for table in active_tables:
            table_pks[table] = await db.primary_keys(table)

        sql = _build_timeline_sql(active_tables, version_cursors, table_pks)
        result = await db.execute(sql)
        col_names = [d[0] for d in result.description]
        raw_rows = [dict(zip(col_names, row)) for row in result.rows]

        # Detect which tables returned a full page (meaning there's more)
        table_counts = Counter(r["_table"] for r in raw_rows)
        tables_with_more = {t for t, c in table_counts.items() if c == _PER_TABLE_LIMIT}
        # Tables that returned fewer rows are now exhausted for subsequent pages
        newly_exhausted = {t for t in active_tables if t not in tables_with_more}

        # Build next-page cursors: min version seen per table that has more
        next_cursors = {}
        for r in raw_rows:
            t = r["_table"]
            if t in tables_with_more:
                if t not in next_cursors or r["__version"] < next_cursors[t]:
                    next_cursors[t] = r["__version"]

        # Cluster the combined rows into timeline events
        clusters = _cluster_rows(raw_rows)

        events = []
        for cluster in clusters:
            table = cluster["table"]
            count = len(cluster["rows"])
            time_label = _format_time(cluster["newest_ms"])
            event = {
                "table": table,
                "count": count,
                "time_label": time_label,
                "newest_ms": cluster["newest_ms"],
                "added": cluster["added"],
                "updated": cluster["updated"],
                "deleted": cluster["deleted"],
                "is_new": cluster["added"] > 0 and cluster["updated"] == 0 and cluster["deleted"] == 0,
                "added_version_range": (
                    "{}-{}".format(cluster["added_min_version"], cluster["added_max_version"])
                    if cluster["added_min_version"] is not None else None
                ),
                "updated_version_range": (
                    "{}-{}".format(cluster["updated_min_version"], cluster["updated_max_version"])
                    if cluster["updated_min_version"] is not None else None
                ),
                "row": None,
                "row_columns": None,
                "row_values": None,
                "row_url": None,
            }

            if count == 1:
                row = cluster["rows"][0]
                event["deleted"] = bool(row["__deleted"])
                event["is_new"] = row["__added_ms"] == row["__updated_ms"]
                pks = table_pks.get(table, [])
                # Extract PK values from the _pk_json column embedded in the UNION result
                pk_json_str = row.get("_pk_json")
                if pks and pk_json_str:
                    try:
                        pk_map = _json.loads(pk_json_str)
                        pk_values = [pk_map.get(pk) for pk in pks]
                    except (ValueError, TypeError):
                        pk_values = None
                    if pk_values and all(v is not None for v in pk_values):
                        if not event["deleted"]:
                            pk_path = ",".join(tilde_encode(str(v)) for v in pk_values)
                            event["row_url"] = datasette.urls.table(database, table) + "/" + pk_path
                        columns, inflated = await _inflate_row(db, table, pks, pk_values)
                        if inflated is not None:
                            # Show up to _PREVIEW_COLS columns; always show PKs first
                            pk_set = set(pks)
                            other_cols = [c for c in columns if c not in pk_set]
                            display_cols = pks + other_cols[: max(0, _PREVIEW_COLS - len(pks))]
                            col_indices = [columns.index(c) for c in display_cols]
                            event["row"] = inflated
                            event["row_columns"] = display_cols
                            event["row_values"] = [_truncate(inflated[i]) for i in col_indices]

            events.append(event)

        # Group events by day (newest-first)
        seen_days = []
        day_events = {}
        for event in events:
            dk = _day_key(event["newest_ms"])
            if dk not in day_events:
                seen_days.append(dk)
                day_events[dk] = []
            day_events[dk].append(event)

        days_out = [
            (_format_day(day_events[dk][0]["newest_ms"]), day_events[dk])
            for dk in seen_days
        ]

        # Build next page URL
        has_more = bool(tables_with_more)
        if has_more:
            params = {}
            for t, v in next_cursors.items():
                params["cursor_" + t] = str(v)
            all_exhausted = exhausted_tables | newly_exhausted
            if all_exhausted:
                params["exhausted"] = ",".join(sorted(all_exhausted))
            next_page_url = request.path + "?" + urllib.parse.urlencode(params)
        else:
            next_page_url = None

    return Response.html(
        await datasette.render_template(
            "chronicle-timeline.html",
            {
                "database": database,
                "chronicle_tables": chronicle_tables,
                "days": days_out,
                "has_more": has_more,
                "next_page_url": next_page_url,
            },
            request=request,
        )
    )


async def chronicle_table_timeline(datasette, request):
    database = request.url_vars["database"]
    table = tilde_decode(request.url_vars["table"])
    db = datasette.get_database(database)

    if database not in upgrade_has_run:
        upgrade_has_run.add(database)
        await upgrade_database(datasette, database)

    chronicle_table = "_chronicle_{}".format(table)
    if not await db.table_exists(chronicle_table):
        raise NotFound("No chronicle table found for {}".format(table))

    pks = await db.primary_keys(table)

    # Parse cursor from ?cursor=VERSION
    version_cursor = None
    raw = request.args.get("cursor")
    if raw is not None:
        try:
            version_cursor = int(raw)
        except ValueError:
            pass

    # Build single-table query
    import json as _json
    escaped = table.replace('"', '""')
    cursor_clause = ""
    if version_cursor is not None:
        cursor_clause = "AND __version < %d" % version_cursor

    if pks:
        pk_args = ", ".join(
            "'%s', \"%s\"" % (pk.replace("'", "''"), pk.replace('"', '""'))
            for pk in pks
        )
        pk_json_expr = "json_object(%s)" % pk_args
    else:
        pk_json_expr = "json('{}')"

    sql = (
        'SELECT __version, __added_ms, __updated_ms, __deleted, %(pk_json)s AS _pk_json'
        ' FROM "_chronicle_%(t)s"'
        ' WHERE 1=1 %(cursor)s'
        ' ORDER BY __version DESC'
        ' LIMIT %(limit)d'
        % dict(t=escaped, cursor=cursor_clause, pk_json=pk_json_expr, limit=_PER_TABLE_LIMIT)
    )
    result = await db.execute(sql)
    col_names = [d[0] for d in result.description]
    raw_rows = [
        dict(_table=table, **dict(zip(col_names, row)))
        for row in result.rows
    ]

    has_more = len(raw_rows) == _PER_TABLE_LIMIT
    next_cursor = None
    if has_more:
        next_cursor = min(r["__version"] for r in raw_rows)

    # Cluster into events
    clusters = _cluster_rows(raw_rows)

    events = []
    for cluster in clusters:
        count = len(cluster["rows"])
        time_label = _format_time(cluster["newest_ms"])
        event = {
            "table": table,
            "count": count,
            "time_label": time_label,
            "newest_ms": cluster["newest_ms"],
            "added": cluster["added"],
            "updated": cluster["updated"],
            "deleted": cluster["deleted"],
            "is_new": cluster["added"] > 0 and cluster["updated"] == 0 and cluster["deleted"] == 0,
            "added_version_range": (
                "{}-{}".format(cluster["added_min_version"], cluster["added_max_version"])
                if cluster["added_min_version"] is not None else None
            ),
            "updated_version_range": (
                "{}-{}".format(cluster["updated_min_version"], cluster["updated_max_version"])
                if cluster["updated_min_version"] is not None else None
            ),
            "row": None,
            "row_columns": None,
            "row_values": None,
            "row_url": None,
        }

        if count == 1:
            row = cluster["rows"][0]
            event["deleted"] = bool(row["__deleted"])
            event["is_new"] = row["__added_ms"] == row["__updated_ms"]
            pk_json_str = row.get("_pk_json")
            if pks and pk_json_str:
                try:
                    pk_map = _json.loads(pk_json_str)
                    pk_values = [pk_map.get(pk) for pk in pks]
                except (ValueError, TypeError):
                    pk_values = None
                if pk_values and all(v is not None for v in pk_values):
                    if not event["deleted"]:
                        pk_path = ",".join(tilde_encode(str(v)) for v in pk_values)
                        event["row_url"] = datasette.urls.table(database, table) + "/" + pk_path
                    columns, inflated = await _inflate_row(db, table, pks, pk_values)
                    if inflated is not None:
                        pk_set = set(pks)
                        other_cols = [c for c in columns if c not in pk_set]
                        display_cols = pks + other_cols[: max(0, _PREVIEW_COLS - len(pks))]
                        col_indices = [columns.index(c) for c in display_cols]
                        event["row"] = inflated
                        event["row_columns"] = display_cols
                        event["row_values"] = [_truncate(inflated[i]) for i in col_indices]

        events.append(event)

    # Group by day
    seen_days = []
    day_events = {}
    for event in events:
        dk = _day_key(event["newest_ms"])
        if dk not in day_events:
            seen_days.append(dk)
            day_events[dk] = []
        day_events[dk].append(event)

    days_out = [
        (_format_day(day_events[dk][0]["newest_ms"]), day_events[dk])
        for dk in seen_days
    ]

    next_page_url = None
    if has_more and next_cursor is not None:
        next_page_url = request.path + "?cursor=%d" % next_cursor

    return Response.html(
        await datasette.render_template(
            "chronicle-table-timeline.html",
            {
                "database": database,
                "table": table,
                "days": days_out,
                "has_more": has_more,
                "next_page_url": next_page_url,
            },
            request=request,
        )
    )
