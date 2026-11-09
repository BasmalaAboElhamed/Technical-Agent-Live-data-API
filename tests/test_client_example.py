import httpx
import pytest

from examples.egx_client import EgxApiUnavailable, EgxClient, is_usable


def make(handler):
    return EgxClient("http://egx.test", transport=httpx.MockTransport(handler))


def test_iter_quotes_follows_pagination():
    def handler(req):
        off = int(req.url.params["offset"])
        data = [{"symbol": f"S{i}"} for i in range(off, min(off + 2, 5))]
        return httpx.Response(200, json={"data": data, "pagination": {"has_more": off + 2 < 5}})
    assert [q["symbol"] for q in make(handler).iter_quotes(page_size=2)] == ["S0", "S1", "S2", "S3", "S4"]


def test_connection_error_raises_instead_of_returning_data():
    def handler(req): raise httpx.ConnectError("refused")
    with pytest.raises(EgxApiUnavailable):
        make(handler).get_all_quotes()


def test_unknown_symbol_is_keyerror():
    c = make(lambda r: httpx.Response(404, json={"error": {"code": "symbol_not_found", "message": "Unknown symbol"}}))
    with pytest.raises(KeyError):
        c.get_quote("NOPE")


def test_missing_price_is_never_usable_and_zero_is_not_missing():
    assert not is_usable({"last_price": None, "data_status": "fresh"})
    assert not is_usable({"last_price": 10, "data_status": "stale"})
    assert is_usable({"last_price": 10, "data_status": "stale"}, allow_stale=True)
    assert is_usable({"last_price": 0.0, "data_status": "fresh"})
    assert not is_usable({"last_price": 10, "data_status": "unavailable"}, allow_stale=True)
