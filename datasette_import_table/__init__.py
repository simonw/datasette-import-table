import asyncio
from datasette import hookimpl
from datasette.utils.asgi import Response
import httpx
import sqlite_utils
from urllib.parse import quote_plus


async def import_table(request, datasette):
    mutable_databases = [db for db in datasette.databases.values() if db.is_mutable]
    error = None

    if request.method == "POST":
        post_vars = await request.post_vars()
        url = post_vars.get("url")
        try:
            table_name, rows, pks, total, next_page = await load_first_page(url)
        except Exception as e:
            error = str(e)
        else:
            primary_key = (pks[0] if len(pks) == 1 else pks) or "rowid"

            def start_table(conn):
                db = sqlite_utils.Database(conn)
                with db.conn:
                    db[table_name].insert_all(rows, pk=primary_key)

            database = datasette.get_database(post_vars.get("database"))
            await database.execute_write_fn(start_table, block=True)

            # This is a bit of a mess. My first implementation of this worked
            # by starting a function on the write thread which fetched each
            # page in turn and wrote them to the database synchronously.
            #
            # Problem: the write thread can only run one function at a time -
            # and for a large number of rows this function blocked anyone
            # else from scheduling a write until it had finished.
            #
            # This more complex version instead runs the paginated HTTP gets
            # in an asyncio task, and has that task schedule a write operation
            # for each individual batch of rows that it receives.

            def do_the_rest(url):
                async def inner_async():
                    nonlocal url

                    def row_writer(rows):
                        def inner(conn):
                            db = sqlite_utils.Database(conn)
                            with db.conn:
                                db[table_name].insert_all(rows)

                        return inner

                    while url:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(url)
                            data = response.json()
                            if data.get("rows"):
                                await database.execute_write_fn(
                                    row_writer(data["rows"])
                                )
                            url = data.get("next_url")

                return inner_async()

            if next_page:
                asyncio.ensure_future(do_the_rest(next_page))

            return Response.redirect(
                "/{}/{}?_import_expected_rows={}".format(
                    database.name, quote_plus(table_name), total
                )
            )

    return Response.html(
        await datasette.render_template(
            "datasette_import_table.html",
            {
                "databases": [m.name for m in mutable_databases],
                "error": error,
            },
            request=request,
        )
    )


class LoadError(Exception):
    pass


async def load_first_page(url):
    url = url + ".json?_shape=objects"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        if response.status_code != 200:
            raise LoadError("Bad status code: {}".format(response))
        if not response.headers["content-type"].startswith("application/json"):
            raise LoadError("Bad content type")
        data = response.json()
        if not isinstance(data.get("rows"), list):
            raise LoadError("rows key should be a list")
    return (
        data["table"],
        data["rows"],
        data["primary_keys"],
        data["filtered_table_rows_count"],
        data.get("next_url"),
    )


@hookimpl
def register_routes():
    return [
        (r"^/-/import-table", import_table),
    ]