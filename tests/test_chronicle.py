from datasette.app import Datasette
import pytest
import sqlite_utils


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_id", (None, "root", "other"))
async def test_enable_disable_chronicle(actor_id, tmpdir):
    db_path = str(tmpdir / "test.db")
    datasette = Datasette([db_path])
    db = sqlite_utils.Database(db_path)
    db["dogs"].insert_all(
        [
            {"name": "Cleo", "age": 7},
            {"name": "Pancakes", "age": 6},
            {"name": "Stacy", "age": 3},
        ],
        pk="id",
    )

    cookies = {}
    if actor_id:
        cookies = {"ds_actor": datasette.sign({"a": {"id": actor_id}}, "actor")}

    # Fetch table page
    response = await datasette.client.get("/test/dogs", cookies=cookies)
    html = response.text
    fragment = "/-/enable-chronicle/test/dogs"
    if actor_id == "root":
        assert fragment in html
    else:
        assert fragment not in html
        return

    # We are root now - enable chronicle
    cookies["ds_csrftoken"] = response.cookies["ds_csrftoken"]
    response2 = await datasette.client.post(
        "/-/enable-chronicle/test/dogs",
        data={"csrftoken": response.cookies["ds_csrftoken"]},
        cookies=cookies,
    )

    # Should redirect
    assert response2.status_code == 302
    assert response2.headers["location"] == "/test/dogs"
    assert datasette.unsign(response2.cookies["ds_messages"], "messages") == [
        ["row version tracking enabled for dogs", 1]
    ]

    # Table should exist now
    assert db["_chronicle_dogs"].exists()

    # Try out the _since= parameter
    for version, expected in (
        (
            None,
            [
                {"id": 1, "name": "Cleo", "age": 7},
                {"id": 2, "name": "Pancakes", "age": 6},
                {"id": 3, "name": "Stacy", "age": 3},
            ],
        ),
        (
            1,
            [
                {"id": 2, "name": "Pancakes", "age": 6},
                {"id": 3, "name": "Stacy", "age": 3},
            ],
        ),
        (
            2,
            [
                {"id": 3, "name": "Stacy", "age": 3},
            ],
        ),
    ):
        json_response = await datasette.client.get(
            "/test/dogs.json?_shape=array{}".format(
                "&_since={}".format(version) if version else ""
            )
        )
        assert json_response.status_code == 200
        assert json_response.json() == expected

    # Table page should have disable action now
    response4 = await datasette.client.get("/test/dogs", cookies=cookies)
    assert "/-/disable-chronicle/test/dogs" in response4.text

    # Disable chronicle
    response5 = await datasette.client.post(
        "/-/disable-chronicle/test/dogs",
        data={"csrftoken": response.cookies["ds_csrftoken"]},
        cookies=cookies,
    )
    # Should redirect
    assert response5.status_code == 302
    assert response5.headers["location"] == "/test/dogs"
    assert datasette.unsign(response5.cookies["ds_messages"], "messages") == [
        ["row version tracking disabled for dogs", 1]
    ]

    # Chronicle table should be gone
    assert not db["_chronicle_dogs"].exists()


@pytest.mark.asyncio
async def test_upgrades_existing_chronicle_tables_on_startup(tmpdir):
    db_path = str(tmpdir / "test2.db")
    db = sqlite_utils.Database(db_path)

    with db.conn:
        db.conn.executescript(
            """
        CREATE TABLE "dogs" (
            id         INTEGER,
            name      TEXT,
            age      INTEGER
        );
        CREATE TABLE "_chronicle_dogs" (
            id         INTEGER,
            added_ms   INTEGER,
            updated_ms INTEGER,
            version    INTEGER DEFAULT 0,
            deleted    INTEGER DEFAULT 0,
            PRIMARY KEY(id)
        );
        CREATE INDEX "_chronicle_dogs_version"
            ON _chronicle_dogs(version);

        CREATE TRIGGER "_chronicle_dogs_ai"
        AFTER INSERT ON "dogs"
        FOR EACH ROW BEGIN
            INSERT INTO "_chronicle_dogs"(id,added_ms,updated_ms,version,deleted)
            VALUES(NEW.id,111,111,1,0);
        END;

        CREATE TRIGGER "_chronicle_dogs_au"
        AFTER UPDATE ON "dogs"
        FOR EACH ROW BEGIN
            UPDATE "_chronicle_dogs"
                SET updated_ms=222, version=2
            WHERE id=OLD.id;
        END;

        CREATE TRIGGER "_chronicle_dogs_ad"
        AFTER DELETE ON "dogs"
        FOR EACH ROW BEGIN
            UPDATE "_chronicle_dogs"
                SET updated_ms=333, version=3, deleted=1
            WHERE id=OLD.id;
        END;

        INSERT INTO dogs(id,name,age) VALUES(1,'Fido',5);
        """
        )

    assert db["_chronicle_dogs"].exists()
    assert db["_chronicle_dogs"].columns_dict == {
        "added_ms": int,
        "deleted": int,
        "id": int,
        "updated_ms": int,
        "version": int,
    }

    datasette = Datasette([db_path])
    # A hit to any page showing the menus should trigger the upgrade
    await datasette.client.get("/test2/dogs")

    assert db["_chronicle_dogs"].columns_dict == {
        "id": int,
        "__added_ms": int,
        "__deleted": int,
        "__updated_ms": int,
        "__version": int,
    }


# --- Timeline tests ---

import time
import sqlite_chronicle


async def _setup_timeline_db(tmpdir):
    """Create a test database with two chronicle-enabled tables and fake timestamps."""
    db_path = str(tmpdir / "timeline.db")
    db = sqlite_utils.Database(db_path)

    db["articles"].insert_all(
        [{"id": i, "title": "Article %d" % i, "body": "Body %d" % i} for i in range(1, 6)],
        pk="id",
    )
    db["tags"].insert_all(
        [{"id": i, "name": "Tag %d" % i} for i in range(1, 4)],
        pk="id",
    )

    with db.conn:
        sqlite_chronicle.enable_chronicle(db.conn, "articles")
        sqlite_chronicle.enable_chronicle(db.conn, "tags")

    # Fake timestamps: articles span two days, tags are all today
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000

    # Articles 1-3: yesterday, articles 4-5: today (10+ minutes apart so no clustering)
    for article_id, ts in [
        (1, now_ms - day_ms - 3600_000),
        (2, now_ms - day_ms - 1800_000),
        (3, now_ms - day_ms),
        (4, now_ms - 700_000),   # ~11 min ago
        (5, now_ms - 60_000),    # ~1 min ago
    ]:
        db["_chronicle_articles"].update(
            article_id, {"__added_ms": ts, "__updated_ms": ts, "__version": article_id}
        )

    # Tags all within a 2-minute window (will cluster)
    tag_ts = now_ms - 120_000
    for tag_id in range(1, 4):
        db["_chronicle_tags"].update(
            tag_id,
            {
                "__added_ms": tag_ts + tag_id * 10_000,
                "__updated_ms": tag_ts + tag_id * 10_000,
                "__version": tag_id,
            },
        )

    datasette = Datasette([db_path])
    return datasette, db


@pytest.mark.asyncio
async def test_timeline_no_chronicle_tables(tmpdir):
    """Timeline page shows a helpful message when no chronicle tables exist."""
    db_path = str(tmpdir / "empty.db")
    db = sqlite_utils.Database(db_path)
    db["things"].insert({"id": 1, "name": "thing"}, pk="id")
    datasette = Datasette([db_path])

    response = await datasette.client.get("/-/chronicle/timeline/empty")
    assert response.status_code == 200
    assert "No tables with chronicle enabled" in response.text


@pytest.mark.asyncio
async def test_timeline_shows_day_headings(tmpdir):
    """Timeline groups events under day headings."""
    datasette, db = await _setup_timeline_db(tmpdir)
    response = await datasette.client.get("/-/chronicle/timeline/timeline")
    assert response.status_code == 200
    html = response.text
    # Should have two different day headings (today and yesterday)
    assert html.count("<h2>") >= 2


@pytest.mark.asyncio
async def test_timeline_shows_table_names(tmpdir):
    """Timeline shows table names as links."""
    datasette, db = await _setup_timeline_db(tmpdir)
    response = await datasette.client.get("/-/chronicle/timeline/timeline")
    html = response.text
    assert "articles" in html
    assert "tags" in html


@pytest.mark.asyncio
async def test_timeline_single_row_preview(tmpdir):
    """Single-row events show a preview of the row data."""
    datasette, db = await _setup_timeline_db(tmpdir)
    response = await datasette.client.get("/-/chronicle/timeline/timeline")
    html = response.text
    # Articles 4 and 5 are isolated (no clustering) so should show row preview tables
    assert "Article 4" in html or "Article 5" in html


@pytest.mark.asyncio
async def test_timeline_clustered_rows_show_summary(tmpdir):
    """Multi-row clusters show 'N added' summary instead of individual rows."""
    datasette, db = await _setup_timeline_db(tmpdir)
    response = await datasette.client.get("/-/chronicle/timeline/timeline")
    html = response.text
    # Tags 1-3 are all within 2 minutes -> should cluster into "3 added" summary
    assert "3 added" in html


@pytest.mark.asyncio
async def test_timeline_pagination_cursor(tmpdir):
    """Timeline generates a next-page URL when tables have more rows than the limit."""
    import datasette_chronicle
    # Temporarily lower the limit to force pagination with our small test DB
    original_limit = datasette_chronicle._PER_TABLE_LIMIT
    datasette_chronicle._PER_TABLE_LIMIT = 2

    try:
        datasette, db = await _setup_timeline_db(tmpdir)
        response = await datasette.client.get("/-/chronicle/timeline/timeline")
        html = response.text
        # Articles has 5 rows but limit is 2 -> should show next page link
        assert "Load earlier changes" in html
        assert "cursor_articles=" in html
    finally:
        datasette_chronicle._PER_TABLE_LIMIT = original_limit


@pytest.mark.asyncio
async def test_timeline_helper_functions():
    """Test pure helper functions: _cluster_rows, _truncate, _build_timeline_sql."""
    from datasette_chronicle import _cluster_rows, _truncate, _build_timeline_sql

    # _truncate
    assert _truncate("hello") == "hello"
    assert _truncate("x" * 100) == "x" * 80 + "\u2026"
    assert _truncate(None) == ""
    assert _truncate(42) == "42"

    # _cluster_rows with two tables, events 10 minutes apart within same table
    now = int(time.time() * 1000)
    rows = [
        {"_table": "a", "__version": 10, "__added_ms": now, "__updated_ms": now, "__deleted": 0},
        {"_table": "a", "__version": 9, "__added_ms": now - 60_000, "__updated_ms": now - 60_000, "__deleted": 0},
        # 10 minutes gap -> new cluster
        {"_table": "a", "__version": 5, "__added_ms": now - 700_000, "__updated_ms": now - 700_000, "__deleted": 0},
        {"_table": "b", "__version": 3, "__added_ms": now - 800_000, "__updated_ms": now - 800_000, "__deleted": 0},
    ]
    clusters = _cluster_rows(rows)
    assert len(clusters) == 3
    assert clusters[0]["table"] == "a"
    assert clusters[0]["added"] == 2
    assert clusters[1]["table"] == "a"
    assert clusters[2]["table"] == "b"

    # _build_timeline_sql with no cursors
    sql = _build_timeline_sql(["dogs", "cats"], {})
    assert "_chronicle_dogs" in sql
    assert "_chronicle_cats" in sql
    assert "ORDER BY __updated_ms DESC" in sql

    # _build_timeline_sql with cursor
    sql = _build_timeline_sql(["dogs"], {"dogs": 42})
    assert "__version < 42" in sql


@pytest.mark.asyncio
async def test_timeline_index_redirects_single_database(tmpdir):
    """Index page redirects to the database when there is only one with chronicle."""
    datasette, db = await _setup_timeline_db(tmpdir)
    response = await datasette.client.get("/-/chronicle/timeline")
    # Should redirect to the single database's timeline
    assert response.status_code == 302
    assert "/chronicle/timeline/timeline" in response.headers["location"]


@pytest.mark.asyncio
async def test_timeline_index_no_chronicle(tmpdir):
    """Index page redirects gracefully when no chronicle tables exist."""
    db_path = str(tmpdir / "empty.db")
    db = sqlite_utils.Database(db_path)
    db["things"].insert({"id": 1}, pk="id")
    datasette = Datasette([db_path])
    response = await datasette.client.get("/-/chronicle/timeline")
    # No redirect - shows database list (empty)
    assert response.status_code == 200
