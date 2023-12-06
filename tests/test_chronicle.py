from datasette.app import Datasette
import pytest
import sqlite_utils


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_id", (None, "root", "other"))
async def test_enable_disable_chronicle(actor_id, tmpdir):
    db_path = str(tmpdir / "test.db")
    datasette = Datasette([db_path])
    db = sqlite_utils.Database(db_path)
    db["dogs"].insert({"name": "Cleo", "age": 4}, pk="id")

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
        ["Chronicle tracking enabled for dogs", 1]
    ]

    # Table should exist now
    assert db["_chronicle_dogs"].exists()

    # Table page should have disable action now
    response3 = await datasette.client.get("/test/dogs", cookies=cookies)
    assert "/-/disable-chronicle/test/dogs" in response3.text

    # Disable chronicle
    response4 = await datasette.client.post(
        "/-/disable-chronicle/test/dogs",
        data={"csrftoken": response.cookies["ds_csrftoken"]},
        cookies=cookies,
    )
    # Should redirect
    assert response4.status_code == 302
    assert response4.headers["location"] == "/test/dogs"
    assert datasette.unsign(response4.cookies["ds_messages"], "messages") == [
        ["Chronicle tracking disabled for dogs", 1]
    ]

    # Chronicle table should be gone
    assert not db["_chronicle_dogs"].exists()
