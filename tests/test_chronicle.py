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
