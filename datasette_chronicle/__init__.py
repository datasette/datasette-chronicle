from datasette import hookimpl, Response
from datasette.filters import FilterArguments
import sqlite_chronicle

try:
    from datasette import Permission
except ImportError:
    # pre-Datasette-1.0
    Permission = None

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
            # Table exists, so it's enabled
            if await datasette.permission_allowed(
                actor, "disable-chronicle", resource=(database, table)
            ):
                return [
                    {
                        "href": datasette.urls.path(
                            "/-/disable-chronicle/{}/{}".format(database, table)
                        ),
                        "label": "Disable row version tracking for this table",
                        "description": "Remove the associated triggers and table",
                    }
                ]
        else:
            # Table doesn't exist, so it's disabled
            if await datasette.permission_allowed(
                actor, "enable-chronicle", resource=(database, table)
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
def register_permissions(datasette):
    if Permission is None:
        return
    return [
        Permission(
            name="enable-chronicle",
            abbr=None,
            description="Enable row version tracking for a table",
            takes_database=True,
            takes_resource=True,
            default=False,
        ),
        Permission(
            name="disable-chronicle",
            abbr=None,
            description="Disable row version tracking for a table",
            takes_database=True,
            takes_resource=True,
            default=False,
        ),
    ]


@hookimpl
def permission_allowed(actor, action):
    if (
        action in ("enable-chronicle", "disable-chronicle")
        and actor
        and actor.get("id") == "root"
    ):
        return True


@hookimpl
def filters_from_request(request, datasette, database, table):
    since = request.args.get("_since")
    if since is None:
        return

    if table.startswith("_chronicle_"):
        return

    async def inner():
        db = datasette.get_database(database)
        chronicle_table = "_chronicle_{}".format(table)
        if not await db.table_exists(chronicle_table):
            # No chronicle table
            return None
        # Get the primary keys
        pks = ", ".join('"{}"'.format(pk) for pk in await db.primary_keys(table))
        extra_where = f'({pks}) in (select {pks} from "{chronicle_table}" where __version > :chronicle_since)'
        return FilterArguments(
            [extra_where],
            {"chronicle_since": since},
            human_descriptions=["modified since version {}".format(since)],
        )

    return inner
