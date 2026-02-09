# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import patch

from bot.commands.position import PositionCommand


class _StubDB:
    def get_latest_data(self, code, days=1):
        return []


def _build_quote(price, pre_close, change_pct, change_amount):
    return SimpleNamespace(
        price=price,
        pre_close=pre_close,
        change_pct=change_pct,
        change_amount=change_amount,
        open_price=None,
        price_session="regular",
    )


def test_closed_market_uses_derived_prev_close_when_realtime_prev_close_is_off():
    cmd = PositionCommand()
    quote = _build_quote(
        price=1.2280,
        pre_close=1.2190,   # 偏小涨幅(~0.74%)
        change_pct=3.54,    # 正确当日涨幅
        change_amount=0.009,
    )
    expected_pre_close = 1.2280 / (1.0 + 3.54 / 100.0)

    with patch("bot.commands.position.get_db", return_value=_StubDB()), \
        patch("bot.commands.position.DataFetcherManager") as mock_manager_cls, \
        patch.object(PositionCommand, "_is_market_open_now", return_value=False), \
        patch.object(PositionCommand, "_normalize_prev_close", side_effect=lambda **kwargs: kwargs["pre_close"]):
        mock_manager_cls.return_value.get_realtime_quote.return_value = quote
        snapshots = cmd._fetch_quote_snapshots(["159813"], {"159813": "CN"})

    result = snapshots["159813"]
    assert result["pre_close"] is not None
    assert abs(result["pre_close"] - expected_pre_close) < 1e-6
    assert result["change_amount"] is not None
    day_pct = result["change_amount"] / result["pre_close"] * 100.0
    assert abs(day_pct - 3.54) < 0.01


def test_realtime_prev_close_kept_when_consistent_with_change_pct():
    cmd = PositionCommand()
    quote = _build_quote(
        price=10.35,
        pre_close=10.0,
        change_pct=3.5,
        change_amount=0.35,
    )

    with patch("bot.commands.position.get_db", return_value=_StubDB()), \
        patch("bot.commands.position.DataFetcherManager") as mock_manager_cls, \
        patch.object(PositionCommand, "_is_market_open_now", return_value=False), \
        patch.object(PositionCommand, "_normalize_prev_close", side_effect=lambda **kwargs: kwargs["pre_close"]):
        mock_manager_cls.return_value.get_realtime_quote.return_value = quote
        snapshots = cmd._fetch_quote_snapshots(["600000"], {"600000": "CN"})

    result = snapshots["600000"]
    assert result["pre_close"] is not None
    assert abs(result["pre_close"] - 10.0) < 1e-6
    assert result["change_amount"] is not None
    day_pct = result["change_amount"] / result["pre_close"] * 100.0
    assert abs(day_pct - 3.5) < 0.01
