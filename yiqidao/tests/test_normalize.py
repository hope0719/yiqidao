"""规范化与解析层测试。全部使用固定测试数据，不联网。

数据来源标注：本文件中的 JSON 片段是**测试数据**，
结构参照 2026-10-10 真实接口返回的字段形状手工构造，
数值为编造，仅用于验证解析逻辑。
"""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from normalize import (  # noqa: E402
    HotelOption,
    TransportOption,
    normalize_hotels,
    normalize_pois,
    normalize_transport,
    parse_datetime,
    parse_minutes,
    parse_money,
)


class TestParseMoney(unittest.TestCase):
    def test_various_formats(self):
        self.assertEqual(parse_money("¥618"), 618.0)
        self.assertEqual(parse_money("￥1,200"), 1200.0)
        self.assertEqual(parse_money("750.00"), 750.0)
        self.assertEqual(parse_money(618), 618.0)
        self.assertEqual(parse_money("23.50"), 23.5)

    def test_missing_is_none_not_zero(self):
        """核心不变式：缺失价格必须是 None，绝不能是 0。"""
        for bad in (None, "", "   ", "暂无报价", [], {}):
            self.assertIsNone(parse_money(bad), f"{bad!r} 应解析为 None")

    def test_zero_is_preserved(self):
        """真实的 0 元（免费）必须保留为 0.0，与缺失区分开。"""
        self.assertEqual(parse_money("¥0"), 0.0)
        self.assertEqual(parse_money("0"), 0.0)


class TestParseMinutes(unittest.TestCase):
    def test_numeric_string(self):
        # 实测接口返回纯数字字符串（分钟）
        self.assertEqual(parse_minutes("185"), 185)
        self.assertEqual(parse_minutes("94"), 94)

    def test_chinese_duration(self):
        # 文档示例中的格式，也要能解析
        self.assertEqual(parse_minutes("140分钟"), 140)
        self.assertEqual(parse_minutes("3小时20分"), 200)
        self.assertEqual(parse_minutes("2小时"), 120)

    def test_missing(self):
        self.assertIsNone(parse_minutes(None))
        self.assertIsNone(parse_minutes(""))
        self.assertIsNone(parse_minutes(""))


class TestParseDatetime(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(
            parse_datetime("2026-10-20 06:55:00"),
            datetime(2026, 10, 20, 6, 55, 0),
        )

    def test_missing(self):
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))
        self.assertIsNone(parse_datetime("not-a-date"))


class TestTransportNormalize(unittest.TestCase):
    # 测试数据：字段形状参照真实机票返回，数值编造
    FLIGHT_ITEM = {
        "ticketPrice": "750.00",
        "adultPrice": None,
        "price": None,
        "totalDuration": "185",
        "tags": None,
        "jumpUrl": "https://example.test/book/1",
        "journeys": [{
            "journeyType": "直达",
            "totalDuration": "185",
            "transferDuration": "",
            "segments": [{
                "marketingTransportNo": "FM9537",
                "marketingTransportName": "上航",
                "transportType": "飞机",
                "depCityName": "上海",
                "depStationName": "虹桥国际机场",
                "depDateTime": "2026-10-20 06:55:00",
                "arrCityName": "三亚",
                "arrStationName": "凤凰国际机场",
                "arrDateTime": "2026-10-20 10:00:00",
                "duration": "185",
                "seatClassName": "经济舱",
                "quantity": None,
            }],
        }],
    }

    # 测试数据：火车返回用 price 而非 ticketPrice（实测差异）
    TRAIN_ITEM = {
        "price": "23.50",
        "totalDuration": "94",
        "jumpUrl": "https://example.test/book/2",
        "journeys": [{
            "journeyType": "直达",
            "totalDuration": "94",
            "transferDuration": "",
            "segments": [{
                "marketingTransportNo": "K751",
                "marketingTransportName": "普快",
                "transportType": "火车",
                "depCityName": "上海",
                "depStationName": "上海松江站",
                "depDateTime": "2026-10-20 09:27:00",
                "arrCityName": "杭州",
                "arrStationName": "杭州站",
                "arrDateTime": "2026-10-20 11:01:00",
                "duration": "94",
                "seatClassName": "硬座",
                "quantity": None,
            }],
        }],
    }

    def test_flight_uses_ticket_price(self):
        opts = normalize_transport([self.FLIGHT_ITEM], "flight", "outbound")
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0].price, 750.0)
        self.assertFalse(opts[0].price_is_missing)
        self.assertEqual(opts[0].total_duration_min, 185)
        self.assertEqual(opts[0].transport_summary, "FM9537 上航 直达")

    def test_train_uses_price(self):
        """机票用 ticketPrice、火车用 price，两者都要能取到。"""
        opts = normalize_transport([self.TRAIN_ITEM], "train", "outbound")
        self.assertEqual(len(opts), 1)
        self.assertEqual(opts[0].price, 23.5)

    def test_times_parsed(self):
        opts = normalize_transport([self.FLIGHT_ITEM], "flight", "outbound")
        self.assertEqual(opts[0].dep_time, datetime(2026, 10, 20, 6, 55))
        self.assertEqual(opts[0].arr_time, datetime(2026, 10, 20, 10, 0))
        self.assertEqual(opts[0].dep_station, "虹桥国际机场")
        self.assertEqual(opts[0].arr_station, "凤凰国际机场")

    def test_missing_price_flagged(self):
        item = dict(self.FLIGHT_ITEM)
        item["ticketPrice"] = None
        opts = normalize_transport([item], "flight", "outbound")
        self.assertIsNone(opts[0].price)
        self.assertTrue(opts[0].price_is_missing)

    def test_seats_always_none(self):
        """实测余票字段恒为 null，不得声称库存已确认。"""
        opts = normalize_transport([self.TRAIN_ITEM], "train", "outbound")
        self.assertIsNone(opts[0].seats_remaining)

    def test_transfer_duration(self):
        item = {
            "price": "636.00",
            "totalDuration": "2405",
            "jumpUrl": "https://example.test/book/3",
            "journeys": [{
                "journeyType": "中转",
                "totalDuration": "2405",
                "transferDuration": "380",
                "segments": [
                    {"marketingTransportNo": "D352", "depDateTime": "2026-10-20 06:12:00",
                     "arrDateTime": "2026-10-20 11:19:00", "duration": "307",
                     "depStationName": "上海虹桥", "arrStationName": "汉口"},
                    {"marketingTransportNo": "K457", "depDateTime": "2026-10-20 17:39:00",
                     "arrDateTime": "2026-10-21 22:17:00", "duration": "1718",
                     "depStationName": "武昌", "arrStationName": "三亚"},
                ],
            }],
        }
        opts = normalize_transport([item], "train", "outbound")
        self.assertEqual(opts[0].transfer_duration_min, 380)
        self.assertEqual(opts[0].journey_type, "中转")
        self.assertEqual(len(opts[0].segments), 2)
        self.assertEqual(opts[0].transport_summary, "D352->K457 中转")

    def test_items_without_times_dropped(self):
        """无时刻的条目无法参与共同停留计算，必须被丢弃。"""
        item = {"price": "100", "journeys": [{"segments": [{"marketingTransportNo": "X1"}]}]}
        self.assertEqual(normalize_transport([item], "flight", "outbound"), [])


class TestHotelNormalize(unittest.TestCase):
    def test_search_price_is_unconfirmed_display_price(self):
        """酒店搜索价不能视为已确认整房每晚报价。"""
        items = [{
            "name": "测试酒店",
            "price": "¥618",
            "star": "豪华型",
            "rate": "4.80",
            "address": "测试路1号",
            "interestsPoi": "近西湖",
            "detailUrl": "https://example.test/h/1",
            "mainPic": "https://example.test/p.jpg",
            "shId": "123",
        }]
        hotels = normalize_hotels(items)
        self.assertEqual(hotels[0].display_price, 618.0)
        self.assertIsNone(hotels[0].price_per_night)
        self.assertFalse(hotels[0].quote_confirmed)
        self.assertTrue(hotels[0].price_is_missing)
        self.assertEqual(hotels[0].rate, 4.8)
        self.assertEqual(hotels[0].detail_url, "https://example.test/h/1")

    def test_missing_price(self):
        items = [{"name": "无价酒店", "price": None}]
        hotels = normalize_hotels(items)
        self.assertIsNone(hotels[0].price_per_night)
        self.assertTrue(hotels[0].price_is_missing)

    def test_missing_name_dropped(self):
        self.assertEqual(normalize_hotels([{"price": "¥100"}]), [])


class TestPoiNormalize(unittest.TestCase):
    def test_free_and_priced(self):
        items = [
            {"name": "免费景点", "freePoiStatus": "FREE", "ticketInfo": None,
             "category": "历史古迹", "jumpUrl": "https://example.test/p/1"},
            {"name": "收费景点", "freePoiStatus": "NOT_FREE",
             "ticketInfo": {"price": "¥68", "ticketName": "大门票 成人票"},
             "category": "自然风光"},
        ]
        pois = normalize_pois(items)
        self.assertIsNone(pois[0].ticket_price)
        self.assertEqual(pois[0].free_status, "FREE")
        self.assertEqual(pois[1].ticket_price, 68.0)
        self.assertEqual(pois[1].ticket_name, "大门票 成人票")

    def test_ticket_info_non_dict(self):
        """ticketInfo 可能为 null 或非 dict，不得抛异常。"""
        pois = normalize_pois([{"name": "X", "ticketInfo": "weird"}])
        self.assertIsNone(pois[0].ticket_price)


if __name__ == "__main__":
    unittest.main(verbosity=2)
