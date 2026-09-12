"""Finding things in the business's own data from a workflow: what is fetched, what the customer is
shown as a list, what is stored - and the walls that keep one customer's orders away from another."""
from __future__ import annotations

import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import menus
from app.services.workflow import engine, schema
from app.services.workflow import lookup as data_lookup
from app.services.workflow.validate import validate_graph
from tests.flow import MEHTA, SHREE, UNKNOWN

H = {"X-Admin-Key": "test-admin"}

ROWS = [{"so_no": "45240", "po_no": "PO-8801", "fg_item_code": "FG-2001", "real_status": "Printing",
         "customer_name": "Mehta Foods"},
        {"so_no": "45239", "po_no": "", "fg_item_code": "FG-2002", "real_status": "Dispatched",
         "customer_name": "Mehta Foods"}]


def doc() -> dict:
    """Find this customer's orders, let them pick one, then show its status."""
    return {"schema": 1, "start": "find", "settings": {"multilingual": False}, "nodes": [
        {"id": "find", "type": "data", "title": "Their orders", "x": 0, "y": 0, "source": "orders", "find": "all",
         "group": "so", "sort": "newest", "limit": 10, "store": "orders", "save": {"newest_so": "so_no"},
         "list_line": "SO {so_no} - {real_status}"},
        {"id": "pick", "type": "question", "title": "Which order", "x": 0, "y": 0,
         "text": {"en": "You have {orders_count} orders. Which one?"}, "store": "order",
         "input": {"kind": "data_list", "from": "orders", "title_field": "so_no",
                   "description_field": "real_status", "button_text": {"en": "Choose"}}},
        {"id": "show", "type": "end", "title": "Status", "x": 0, "y": 0,
         "text": {"en": "SO {order}: {order_status}"}},
        {"id": "none", "type": "end", "title": "No orders", "x": 0, "y": 0,
         "text": {"en": "You have no orders with us yet."}}],
        "edges": [{"id": "e1", "from": "find", "port": "found", "to": "pick"},
                  {"id": "e2", "from": "find", "port": "none", "to": "none"},
                  {"id": "e3", "from": "pick", "port": "next", "to": "show"},
                  {"id": "e4", "from": "pick", "port": "empty", "to": "none"}]}


def stub_rows(rows):
    async def data(spec, state):
        return list(rows)
    return data


async def fresh(rule, value):
    """The order looked up again when the customer picks it - the status has moved on since."""
    return {"value": value, "status": "Dispatched today", "so": value, "po": "PO-8801", "items": "FG-2001",
            "count": "1"}


async def gone(rule, value):
    return None


async def start(rows=ROWS, **kw):
    return await engine.start(schema.parse(doc()), data=stub_rows(rows), **kw)


# ---------------- what may be read at all ----------------
async def test_a_workflow_only_ever_sees_this_customers_own_orders(db):
    mine = await data_lookup.find_rows(db, SHREE, {"source": "orders", "find": "all"})
    theirs = await data_lookup.find_rows(db, MEHTA, {"source": "orders", "find": "all"})
    assert mine and theirs
    assert {r["customer_name"] for r in mine} == {"Shree Packaging Pvt Ltd"}
    assert {r["so_no"] for r in mine}.isdisjoint({r["so_no"] for r in theirs})


async def test_the_internal_connection_status_can_never_come_back(db):
    rows = await data_lookup.find_rows(db, SHREE, {"source": "orders", "find": "all"})
    assert rows and all("connection_status" not in r for r in rows)
    assert "CONN" not in json.dumps(rows)  # the dummy data's internal status is CONN-PLANT-A-OK


async def test_a_number_that_is_not_a_customer_finds_nothing(db):
    assert await data_lookup.find_rows(db, UNKNOWN, {"source": "orders", "find": "all"}) is None
    assert await data_lookup.find_rows(db, "", {"source": "orders", "find": "all"}) is None


async def test_finding_by_a_number_filtering_and_ordering(db):
    async def find(**spec):
        return await data_lookup.find_rows(db, SHREE, {"source": "orders", "find": "all", **spec})

    one = await data_lookup.find_rows(db, SHREE, {"source": "orders", "find": "so", "match": "SO 45231"})
    assert [r["so_no"] for r in one] == ["45231"]
    by_po = await data_lookup.find_rows(db, SHREE, {"source": "orders", "find": "po", "match": "PO-7781"})
    assert by_po and by_po[0]["so_no"] == "45231"

    newest = await find(sort="newest")
    assert [r["so_no"] for r in newest] == sorted((r["so_no"] for r in newest), key=int, reverse=True)
    assert [r["so_no"] for r in await find(sort="oldest")] == [r["so_no"] for r in reversed(newest)]

    dispatched = await find(filter=[{"field": "real_status", "op": "contains", "value": "dispatch"}])
    assert dispatched and all("dispatch" in r["real_status"].casefold() for r in dispatched)
    assert await find(filter=[{"field": "so_no", "op": "eq", "value": "no-such-order"}]) == []

    lines = await find(group="line")
    assert len(lines) >= len(newest)  # one row per order line, not per order


async def test_their_own_record_in_the_customer_list(db):
    got = await data_lookup.find_rows(db, MEHTA, {"source": "customer", "find": "all"})
    assert got == [{"customer_code": "C002", "customer_name": "Mehta Foods"}]


# ---------------- showing the rows, and picking one ----------------
async def test_the_rows_are_drawn_as_one_legal_whatsapp_list():
    turn = await start()
    assert [m.text for m in turn.messages] == ["You have 2 orders. Which one?"]
    options = turn.messages[0].options
    assert menus.validate(options) == []
    assert [o.title for o in options.items] == ["45240", "45239"]
    assert [o.description for o in options.items] == ["Printing", "Dispatched"]
    assert options.button_text == "Choose"


async def test_what_a_data_step_leaves_for_later_steps():
    turn = await start()
    assert turn.state.vars["orders_count"] == "2"
    assert turn.state.vars["orders_list"] == "SO 45240 - Printing\nSO 45239 - Dispatched"
    assert turn.state.vars["newest_so"] == "45240"


async def test_picking_a_row_checks_it_against_the_data_again():
    first = await start()
    turn = await engine.advance(schema.parse(doc()), first.state, "45240", data=stub_rows(ROWS), lookup=fresh)
    assert [m.text for m in turn.messages] == ["SO 45240: Dispatched today"]
    assert turn.state.vars["order"] == "45240"
    assert turn.state.vars["order_status"] == "Dispatched today"  # fresh, not the status in the list
    assert turn.state.vars["order_real_status"] == "Dispatched today"
    assert turn.state.vars["order_fg_item_code"] == "FG-2001"  # and the row's own fields


async def test_an_item_row_is_checked_by_its_item_code_not_its_order():
    """A list of items shows one line each, so picking one must ask the data about that item."""
    asked: list[tuple] = []

    async def spy(rule, value):
        asked.append((rule.get("source"), value))
        return {"value": value, "status": "Printing", "so": "45240", "po": "", "items": value, "count": "1"}

    by_item = doc()
    by_item["nodes"][1]["input"]["title_field"] = "fg_item_code"
    graph = schema.parse(by_item)
    first = await engine.start(graph, data=stub_rows(ROWS))
    turn = await engine.advance(graph, first.state, "FG-2001", data=stub_rows(ROWS), lookup=spy)
    assert asked == [("fg", "FG-2001")]
    assert turn.state.vars["order_status"] == "Printing"


async def test_typing_the_order_number_works_as_well_as_tapping():
    first = await start()
    turn = await engine.advance(schema.parse(doc()), first.state, "SO 45240", data=stub_rows(ROWS), lookup=fresh)
    assert [m.text for m in turn.messages] == ["SO 45240: Dispatched today"]


async def test_an_order_that_is_no_longer_theirs_is_refused():
    first = await start()
    turn = await engine.advance(schema.parse(doc()), first.state, "45240", data=stub_rows(ROWS), lookup=gone)
    assert turn.stopped == "not_understood"  # asked again, never answered with someone else's order
    assert "order" not in turn.state.vars


async def test_something_else_entirely_asks_again_or_takes_anything_else():
    first = await start()
    turn = await engine.advance(schema.parse(doc()), first.state, "what are your prices",
                                data=stub_rows(ROWS), lookup=fresh)
    assert turn.stopped == "not_understood"

    with_default = doc()
    with_default["edges"].append({"id": "e5", "from": "pick", "port": "default", "to": "none"})
    graph = schema.parse(with_default)
    began = await engine.start(graph, data=stub_rows(ROWS))
    turn = await engine.advance(graph, began.state, "what are your prices", data=stub_rows(ROWS), lookup=fresh)
    assert [m.text for m in turn.messages] == ["You have no orders with us yet."]


async def test_nothing_found_takes_its_own_exit_and_asks_nothing():
    turn = await start(rows=[])
    assert [m.text for m in turn.messages] == ["You have no orders with us yet."]
    assert turn.stopped == "end" and turn.state.vars["orders_count"] == "0"


async def test_more_rows_than_whatsapp_shows_are_cut_and_the_customer_is_told():
    many = [{"so_no": str(45200 + i), "real_status": "In production", "po_no": "", "fg_item_code": f"FG-{i}",
             "customer_name": "Mehta Foods"} for i in range(14)]
    turn = await start(rows=many)
    options = turn.messages[0].options
    assert len(options.items) == menus.LIST_ROWS_MAX and menus.validate(options) == []
    assert options.footer == menus.label("more_hint", "en")
    assert turn.state.vars["orders_count"] == "14"  # the true total, not what fitted


async def test_the_rows_survive_the_trip_through_the_browser_and_a_hostile_one_is_capped():
    turn = await start()
    again = engine.RunState.from_dict(json.loads(json.dumps(turn.state.to_dict())))
    assert again.data["orders"]["rows"] == turn.state.data["orders"]["rows"]

    hostile = engine.RunState.from_dict({"data": {"a": "not a set", "b": {"rows": [{"x": "y" * 500}] * 50},
                                                  "c": {"rows": []}, "d": {"rows": []}}})
    # only the first two entries are even looked at, and the one that is not a set is dropped
    assert list(hostile.data) == ["b"]
    assert len(hostile.data["b"]["rows"]) == schema.DATA_ROWS_MAX
    assert len(hostile.data["b"]["rows"][0]["x"]) == schema.DATA_VALUE_MAX


# ---------------- what may be published ----------------
def test_a_workflow_that_uses_its_data_well_is_publishable():
    assert [i.message for i in validate_graph(doc()) if i.level == "fail"] == []


@pytest.mark.parametrize("break_it, expect", [
    (lambda d: d["nodes"][0].update(filter=[{"field": "connection_status", "op": "eq", "value": "x"}]),
     "Connection Status"),
    (lambda d: d["nodes"][0].update(save={"Not A Name": "so_no"}), "not a usable answer name"),
    (lambda d: d["nodes"][0].update(save={"first": "secret_margin"}), "which is not one of the fields"),
    (lambda d: d["nodes"][0].update(store=""), "has no name for what it finds"),
    (lambda d: d["nodes"][0].update(limit=40), "WhatsApp shows at most"),
    (lambda d: d["nodes"][0].update(find="so", match=""), "does not say which one"),
    (lambda d: d["nodes"][1]["input"].update(from_="x") or d["nodes"][1]["input"].update({"from": "ghost"}),
     "no Find-in-your-data step is called that"),
    (lambda d: d["nodes"][1]["input"].update({"title_field": ""}), "does not say what each row should read"),
    (lambda d: d["edges"].remove(d["edges"][1]), '"Nothing found"'),
    (lambda d: d["edges"].remove(d["edges"][3]), '"Nothing to show"'),
])
def test_the_checks_catch_what_would_break_a_conversation(break_it, expect):
    bad = doc()
    break_it(bad)
    fails = [i.message for i in validate_graph(bad) if i.level == "fail"]
    assert any(expect in m for m in fails), fails


def test_a_list_question_that_can_be_reached_without_its_data_step_is_only_a_warning():
    skippable = doc()
    skippable["start"] = "pick"
    got = validate_graph(skippable)
    assert not [i for i in got if i.level == "fail" and "without passing" in i.message]
    assert any("without passing" in i.message for i in got if i.level == "warn")


# ---------------- through the API ----------------
async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_the_editor_is_offered_the_owners_own_column_names():
    async with await _client() as c:
        got = (await c.get("/admin/api/workflows/data-fields", headers=H)).json()
    orders = next(s for s in got["sources"] if s["value"] == "orders")
    labels = {f["name"]: f["label"] for f in orders["fields"]}
    assert labels["real_status"] == "Real Status (PPC)" and labels["so_no"] == "SO No"
    assert "connection_status" not in labels
    assert "Connection Status" not in json.dumps(got)
    statuses = next(f for f in orders["fields"] if f["name"] == "real_status")["values"]
    assert "Dispatched" in statuses  # real statuses, so a filter can be picked rather than typed


async def test_the_test_chat_lists_a_real_customers_own_orders():
    async with await _client() as c:
        key = (await c.post("/admin/api/workflows", json={"title": "Order picker"}, headers=H)).json()["key"]
        assert (await c.put(f"/admin/api/workflows/{key}/draft", json={"doc": doc()}, headers=H)).status_code == 200
        first = (await c.post(f"/admin/api/workflows/{key}/simulate", json={"phone": MEHTA}, headers=H)).json()
        titles = [o["title"] for o in first["messages"][0]["options"]["items"]]
        picked = (await c.post(f"/admin/api/workflows/{key}/simulate",
                               json={"phone": MEHTA, "text": titles[0], "state": first["state"]}, headers=H)).json()
    assert "45240" in titles
    assert picked["messages"][0]["text"].startswith(f"SO {titles[0]}: ")
    assert "CONN" not in json.dumps(first) and "CONN" not in json.dumps(picked)


async def test_another_customers_order_number_is_not_accepted():
    """Typed, not tapped: Mehta's own list, someone else's SO number."""
    async with await _client() as c:
        key = (await c.post("/admin/api/workflows", json={"title": "Order picker 2"}, headers=H)).json()["key"]
        await c.put(f"/admin/api/workflows/{key}/draft", json={"doc": doc()}, headers=H)
        first = (await c.post(f"/admin/api/workflows/{key}/simulate", json={"phone": MEHTA}, headers=H)).json()
        got = (await c.post(f"/admin/api/workflows/{key}/simulate",
                            json={"phone": MEHTA, "text": "45231", "state": first["state"]}, headers=H)).json()
    assert got["stopped"] == "not_understood"  # 45231 is Shree's order, not theirs
    assert "In Production" not in json.dumps(got)
