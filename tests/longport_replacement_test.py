# -*- coding: utf-8 -*-
"""
Longport 替代链路测试：
1. LongportFetcher.get_stock_name
2. LongportFetcher.get_main_indices
3. DataFetcherManager.get_main_indices 路由优先级与回退
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from data_provider.base import DataFetcherManager
from data_provider.longport_fetcher import LongportFetcher


class LongportFetcherTestCase(unittest.TestCase):
    def _build_fetcher(self) -> LongportFetcher:
        # 跳过 __init__，避免依赖真实凭证和网络
        fetcher = LongportFetcher.__new__(LongportFetcher)
        fetcher._enabled = True
        fetcher._ctx = Mock()
        fetcher._api = Mock()
        fetcher._name_cache = {}
        fetcher._main_indices = []
        return fetcher

    def test_get_stock_name_supports_cn_hk_us(self) -> None:
        fetcher = self._build_fetcher()
        mapper = {
            "600519.SH": "贵州茅台",
            "700.HK": "腾讯控股",
            "AAPL.US": "苹果",
        }
        fetcher._resolve_name = Mock(side_effect=lambda symbol: mapper.get(symbol, ""))

        self.assertEqual(fetcher.get_stock_name("600519"), "贵州茅台")
        self.assertEqual(fetcher.get_stock_name("HK00700"), "腾讯控股")
        self.assertEqual(fetcher.get_stock_name("AAPL"), "苹果")

    def test_get_stock_name_returns_none_on_api_exception(self) -> None:
        fetcher = self._build_fetcher()
        fetcher._call_with_refresh = Mock(side_effect=Exception("boom"))
        fetcher._ctx.static_info = Mock()

        self.assertIsNone(fetcher.get_stock_name("600519"))

    def test_get_main_indices_skip_partial_failures(self) -> None:
        fetcher = self._build_fetcher()
        fetcher._main_indices = [("000001.SH", "上证指数"), ("BAD.US", "Bad")]

        good_quote = SimpleNamespace(
            last_done=3200.0,
            prev_close=3180.0,
            high=3210.0,
            low=3170.0,
            open=3190.0,
            volume=123456.0,
            turnover=99887766.0,
            change_value=20.0,
            change_rate=0.63,
        )

        def _side_effect(func, symbols):
            symbol = symbols[0]
            if symbol == "000001.SH":
                return [good_quote]
            raise Exception("symbol not found")

        fetcher._call_with_refresh = Mock(side_effect=_side_effect)
        fetcher._ctx.quote = Mock()

        data = fetcher.get_main_indices()
        self.assertIsNotNone(data)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["code"], "000001")
        self.assertEqual(data[0]["name"], "上证指数")
        self.assertEqual(data[0]["current"], 3200.0)


class DataFetcherManagerIndexRoutingTestCase(unittest.TestCase):
    def test_get_main_indices_prefers_longport(self) -> None:
        manager = DataFetcherManager.__new__(DataFetcherManager)

        longport = Mock()
        longport.name = "LongportFetcher"
        longport.get_main_indices.return_value = [{"code": "000001"}]

        akshare = Mock()
        akshare.name = "AkshareFetcher"
        akshare.get_main_indices.return_value = [{"code": "399001"}]

        manager._fetchers = [longport, akshare]

        data = manager.get_main_indices()
        self.assertEqual(data, [{"code": "000001"}])
        akshare.get_main_indices.assert_not_called()

    def test_get_main_indices_fallback_when_longport_empty(self) -> None:
        manager = DataFetcherManager.__new__(DataFetcherManager)

        longport = Mock()
        longport.name = "LongportFetcher"
        longport.get_main_indices.return_value = None

        akshare = Mock()
        akshare.name = "AkshareFetcher"
        akshare.get_main_indices.return_value = [{"code": "399001"}]

        manager._fetchers = [longport, akshare]

        data = manager.get_main_indices()
        self.assertEqual(data, [{"code": "399001"}])
        longport.get_main_indices.assert_called_once()
        akshare.get_main_indices.assert_called_once()


if __name__ == "__main__":
    unittest.main()
