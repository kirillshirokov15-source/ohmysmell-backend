from urllib.parse import quote

import pytest

from app.services.buying_catalog_search import parse_catalog_query
from tests.test_buying import workspace, login, supplier, preview, confirm


@pytest.mark.parametrize("raw,includes,excludes", [
    ("Marv Str", ("marv", "str"), ()),
    ("marvis 200мл -100мл -150ml -250 мл", ("marvis", "200мл"), ("100мл","150ml","250 мл")),
    ('marvis 200 мл -"100 мл" -"150 ml"', ("marvis","200 мл"), ("100 мл","150 ml")),
    ("-100", (), ("100",)),
])
def test_query_syntax(raw, includes, excludes):
    terms = parse_catalog_query(raw)
    assert terms.include == includes
    assert terms.exclude == excludes


def test_real_buying_api_short_prefix_and_minus_volume(workspace):
    c, _, _ = workspace
    login(c)
    sid = supplier(c)
    rows = [["Name","Price"], ["SYNTHETIC Marvis Classic 100 ml","10"],
        ["SYNTHETIC Marvis Classic 150мл","11"],
        ["SYNTHETIC Marvis Classic 200 ml","12"],
        ["SYNTHETIC Marvis Classic 250 ml","14"],
        ["SYNTHETIC Chanel Rose 200 ml","20"]]
    assert confirm(c, sid, preview(c, sid, rows)).status_code == 200

    def found(q):
        response = c.get("/buying/catalog?q=" + quote(q, safe=""))
        assert response.status_code == 200, response.text
        return response.json()

    assert found("marv")["total"] == 4
    assert found("marv cla")["total"] == 4
    assert found("marv 200мл")["total"] == 1
    assert found("marv 200 мл -100 мл -150мл -250мл")["total"] == 1
    assert found('marv -"100 ml" -"150 ml" -"250 ml"')["total"] == 1
    assert found("-100мл")["total"] == 4
    assert found("200 мл")["total"] == 2
    assert found("%")["total"] == 0
    assert found("_")["total"] == 0
    assert found("marv nonexistent")["total"] == 0
    assert found("marv 200мл")["products"][0]["name"].endswith("200 ml")


@pytest.mark.parametrize("q", ['"unterminated', 'x ' * 50])
def test_bad_query_is_rejected(workspace,q):
    c,_,_ = workspace
    login(c)
    result=c.get("/buying/catalog?q="+quote(q, safe=""))
    assert result.status_code == 422
