from datasette.app import Datasette
import pytest
import sqlite_utils
import re
import sqlite_chronicle # For direct chronicle manipulation in Playwright tests
import time # For timestamp comparisons, if necessary


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


@pytest.mark.asyncio
async def test_chronicle_extra_js_urls(tmpdir):
    db_path = str(tmpdir / "test_js.db")
    datasette = Datasette([db_path], metadata={
        "plugins": {
            "datasette-chronicle": {
                "foo": "bar" # Ensure plugin is loaded
            }
        }
    })
    db = sqlite_utils.Database(db_path)
    db["items"].insert_all([{"id": 1, "name": "Item 1"}], pk="id")

    # Enable chronicle for "items" table
    # Need actor and CSRF token
    cookies = {"ds_actor": datasette.sign({"a": {"id": "root"}}, "actor")}
    response_for_csrf = await datasette.client.get("/test_js/items", cookies=cookies)
    assert response_for_csrf.status_code == 200
    csrftoken = response_for_csrf.cookies["ds_csrftoken"]
    cookies["ds_csrftoken"] = csrftoken

    enable_response = await datasette.client.post(
        "/-/enable-chronicle/test_js/items",
        data={"csrftoken": csrftoken},
        cookies=cookies,
    )
    assert enable_response.status_code == 302 # Redirects after enabling

    # Check table page when chronicle is enabled
    response_enabled = await datasette.client.get("/test_js/items", cookies=cookies)
    assert response_enabled.status_code == 200
    assert '<script src="/-/static-plugins/datasette-chronicle/datasette_chronicle.js"></script>' in response_enabled.text

    # Disable chronicle for "items" table
    disable_response = await datasette.client.post(
        "/-/disable-chronicle/test_js/items",
        data={"csrftoken": csrftoken},
        cookies=cookies,
    )
    assert disable_response.status_code == 302

    # Check table page when chronicle is disabled
    response_disabled = await datasette.client.get("/test_js/items", cookies=cookies)
    assert response_disabled.status_code == 200
    assert '<script src="/-/static-plugins/datasette-chronicle/datasette_chronicle.js"></script>' not in response_disabled.text

    # Check a table where chronicle was never enabled
    db["other_items"].insert_all([{"id": 1, "name": "Other Item 1"}], pk="id")
    response_never_enabled = await datasette.client.get("/test_js/other_items", cookies=cookies)
    assert response_never_enabled.status_code == 200
    assert '<script src="/-/static-plugins/datasette-chronicle/datasette_chronicle.js"></script>' not in response_never_enabled.text


@pytest.mark.asyncio
async def test_chronicle_extra_head_html(tmpdir):
    db_path = str(tmpdir / "test_head.db")
    datasette = Datasette([db_path], metadata={
        "plugins": {
            "datasette-chronicle": {
                "foo": "bar" 
            }
        }
    })
    db = sqlite_utils.Database(db_path)
    db["products"].insert_all([{"id": 1, "sku": "PROD001"}], pk="id")

    cookies = {"ds_actor": datasette.sign({"a": {"id": "root"}}, "actor")}
    response_for_csrf = await datasette.client.get("/test_head/products", cookies=cookies)
    csrftoken = response_for_csrf.cookies["ds_csrftoken"]
    cookies["ds_csrftoken"] = csrftoken

    await datasette.client.post(
        "/-/enable-chronicle/test_head/products",
        data={"csrftoken": csrftoken},
        cookies=cookies,
    )

    response_enabled = await datasette.client.get("/test_head/products", cookies=cookies)
    assert response_enabled.status_code == 200
    assert "<style>" in response_enabled.text
    assert ".chronicle-notification-banner" in response_enabled.text

    await datasette.client.post(
        "/-/disable-chronicle/test_head/products",
        data={"csrftoken": csrftoken},
        cookies=cookies,
    )
    response_disabled = await datasette.client.get("/test_head/products", cookies=cookies)
    assert response_disabled.status_code == 200
    assert "<style>" not in response_disabled.text
    assert ".chronicle-notification-banner" not in response_disabled.text


@pytest.mark.asyncio
async def test_chronicle_extra_body_script(tmpdir):
    db_path = str(tmpdir / "test_body.db")
    datasette = Datasette([db_path], metadata={
        "plugins": {
            "datasette-chronicle": {
                "foo": "bar"
            }
        }
    })
    db = sqlite_utils.Database(db_path)
    table_name = "orders"
    db[table_name].insert_all([{"id": 1, "val": "A"}], pk="id")

    cookies = {"ds_actor": datasette.sign({"a": {"id": "root"}}, "actor")}
    response_for_csrf = await datasette.client.get(f"/test_body/{table_name}", cookies=cookies)
    csrftoken = response_for_csrf.cookies["ds_csrftoken"]
    cookies["ds_csrftoken"] = csrftoken

    # Enable chronicle
    await datasette.client.post(
        f"/-/enable-chronicle/test_body/{table_name}",
        data={"csrftoken": csrftoken},
        cookies=cookies,
    )

    # Initial state (after enabling, version should be 1 due to initial insert into chronicle)
    # sqlite-chronicle auto-inserts existing rows into chronicle table with version 1 upon enabling
    response = await datasette.client.get(f"/test_body/{table_name}", cookies=cookies)
    assert response.status_code == 200
    
    # Version 1 from the initial population of _chronicle_orders from existing 'orders' table
    expected_max_version = 1 
    assert f'window.datasette_chronicle_max_version = {expected_max_version};' in response.text
    assert f'window.datasette_chronicle_database_name = "test_body";' in response.text
    assert f'window.datasette_chronicle_table_name = "{table_name}";' in response.text
    
    chronicle_rows = list(db.query(f"SELECT MAX(__version) as max_v FROM _chronicle_{table_name}"))
    assert chronicle_rows[0]["max_v"] == expected_max_version

    # Add more data to increment version
    db[table_name].insert({"id": 2, "val": "B"}) # Version 2
    db[table_name].update(1, {"val": "A_updated"}) # Version 3

    response_updated = await datasette.client.get(f"/test_body/{table_name}", cookies=cookies)
    assert response_updated.status_code == 200
    expected_max_version_updated = 3
    assert f'window.datasette_chronicle_max_version = {expected_max_version_updated};' in response_updated.text
    
    chronicle_rows_updated = list(db.query(f"SELECT MAX(__version) as max_v FROM _chronicle_{table_name}"))
    assert chronicle_rows_updated[0]["max_v"] == expected_max_version_updated

    # Test with an empty chronicle table (enabled but no CUD operations yet on original table)
    empty_table_name = "empty_stuff"
    db[empty_table_name].create({"id": int, "name": str}, pk="id")
    await datasette.client.post(
        f"/-/enable-chronicle/test_body/{empty_table_name}",
        data={"csrftoken": csrftoken}, # Re-use CSRF from a valid page
        cookies=cookies,
    )
    # When chronicle is enabled on an empty table, _chronicle_empty_stuff is created but has 0 rows.
    # Max version should be 0.
    response_empty = await datasette.client.get(f"/test_body/{empty_table_name}", cookies=cookies)
    assert response_empty.status_code == 200
    assert 'window.datasette_chronicle_max_version = 0;' in response_empty.text
    assert f'window.datasette_chronicle_database_name = "test_body";' in response_empty.text
    assert f'window.datasette_chronicle_table_name = "{empty_table_name}";' in response_empty.text

    chronicle_empty_rows = list(db.query(f"SELECT MAX(__version) as max_v FROM _chronicle_{empty_table_name}"))
    # MAX of an empty set is NULL in SQL, which our hook converts to 0.
    assert chronicle_empty_rows[0]["max_v"] is None 

    # Script should not be present if chronicle is not enabled
    no_chron_table = "no_chron_table"
    db[no_chron_table].insert({"id": 1, "val": "X"}, pk="id")
    response_no_chron = await datasette.client.get(f"/test_body/{no_chron_table}", cookies=cookies)
    assert response_no_chron.status_code == 200
    assert 'window.datasette_chronicle_max_version =' not in response_no_chron.text


@pytest.mark.asyncio
async def test_chronicle_notification_banner_behavior(datasette_port, page, tmpdir):
    db_path = str(tmpdir / "test_playwright.db")
    # We need to initialize Datasette instance ourselves to get its URL for Playwright
    ds = Datasette([db_path], settings={"base_url": f"http://localhost:{datasette_port}"})
    await ds.invoke_startup() # Ensure plugins are loaded, including datasette-chronicle
    
    db = sqlite_utils.Database(db_path)
    table_name = "log_entries"

    # Initial table setup
    db[table_name].create(
        {"id": int, "message": str, "ts": float},
        pk="id",
    )
    # Enable chronicle BEFORE inserting initial data, so initial inserts get versioned.
    # sqlite-chronicle's enable_chronicle will also copy existing data into chronicle table.
    # For a clean test, enable first, then insert.
    db.conn.execute("VACUUM;") # Ensure connection is fresh before direct manipulation
    sqlite_chronicle.enable_chronicle(db.conn, table_name)

    # Insert initial data - this will create versions 1, 2, 3
    db[table_name].insert_all([
        {"id": 1, "message": "First log", "ts": time.time()},
        {"id": 2, "message": "Second log", "ts": time.time() + 1},
        {"id": 3, "message": "Third log", "ts": time.time() + 2},
    ])
    
    initial_max_version = 3
    chronicle_table_name = f"_chronicle_{table_name}"
    # Verify chronicle table state
    assert db[chronicle_table_name].count == initial_max_version
    max_v_rows = list(db.query(f"SELECT MAX(__version) AS max_v FROM {chronicle_table_name}"))
    assert max_v_rows[0]["max_v"] == initial_max_version

    table_url = ds.urls.table("test_playwright", table_name)

    # --- Scenario 1: First visit ---
    await page.goto(table_url)
    
    # Banner should NOT be visible on first visit
    await page.wait_for_selector(".chronicle-notification-banner", state="hidden")

    # Check localStorage
    ls_key = f"chronicle_last_seen_info_test_playwright_{table_name}"
    stored_info_s1 = await page.evaluate(f"localStorage.getItem('{ls_key}')")
    assert stored_info_s1 is not None
    stored_info_s1_data = json.loads(stored_info_s1)
    assert stored_info_s1_data["version"] == initial_max_version
    assert "timestamp" in stored_info_s1_data

    # --- Scenario 2: Subsequent visit, no changes ---
    await page.goto(table_url) # Reload page
    await page.wait_for_selector(".chronicle-notification-banner", state="hidden")
    
    stored_info_s2 = await page.evaluate(f"localStorage.getItem('{ls_key}')")
    assert stored_info_s2 is not None
    stored_info_s2_data = json.loads(stored_info_s2)
    assert stored_info_s2_data["version"] == initial_max_version # Version should be the same

    # --- Scenario 3: Subsequent visit, with changes ---
    # Add more data to the table
    db[table_name].insert_all([
        {"id": 4, "message": "Fourth log", "ts": time.time() + 3}, # Version 4
        {"id": 5, "message": "Fifth log", "ts": time.time() + 4},   # Version 5
    ])
    new_max_version = 5
    assert db[chronicle_table_name].count == new_max_version
    max_v_rows_s3 = list(db.query(f"SELECT MAX(__version) AS max_v FROM {chronicle_table_name}"))
    assert max_v_rows_s3[0]["max_v"] == new_max_version
    
    await page.goto(table_url) # Reload page

    # Banner SHOULD be visible now
    banner = await page.wait_for_selector(".chronicle-notification-banner")
    assert banner is not None
    banner_text = await banner.text_content()
    
    # 2 new rows (5 - 3 = 2)
    # The exact "time since" text can be flaky, so check for the core message part
    assert "2 row(s) updated" in banner_text 
    # Example: "2 row(s) updated since your last visit recently." or "... X day(s) ago."

    # Check localStorage updated
    stored_info_s3 = await page.evaluate(f"localStorage.getItem('{ls_key}')")
    assert stored_info_s3 is not None
    stored_info_s3_data = json.loads(stored_info_s3)
    assert stored_info_s3_data["version"] == new_max_version
    assert stored_info_s3_data["timestamp"] > stored_info_s1_data["timestamp"]
    
    # --- Scenario 4: Visit again after seeing changes, banner should be gone ---
    await page.goto(table_url)
    await page.wait_for_selector(".chronicle-notification-banner", state="hidden")
    
    # LocalStorage version should remain new_max_version
    stored_info_s4 = await page.evaluate(f"localStorage.getItem('{ls_key}')")
    assert stored_info_s4 is not None
    stored_info_s4_data = json.loads(stored_info_s4)
    assert stored_info_s4_data["version"] == new_max_version
    
    await ds.client.session.close() # Close datasette client
