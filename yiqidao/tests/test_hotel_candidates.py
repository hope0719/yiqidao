"""Regression coverage for shared accommodation mistaken for a private-room option."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from normalize import HotelOption
from orchestrator import _pick_hotel, _room_candidates, plan_trip
from request_io import request_from_dict
from flyai_client import FlyAIClient
from render import render_outcome


def hotel(name, score, price):
    return HotelOption.from_raw({'name': name, 'rate': score, 'price': price})


class HotelCandidateTests(unittest.TestCase):
    def test_shared_accommodation_is_excluded_from_main_and_alternatives(self):
        options = [hotel('漫岛青年公寓', 5, 10), hotel('青旅床位', 5, 20),
                   hotel('City Youth Apartment', 5, 10), hotel('City Hostel', 5, 10),
                   hotel('胶囊酒店', 5, 25), hotel('普通酒店A', 4, 300), hotel('普通酒店B', 4.5, 500)]
        self.assertEqual([h.name for h in _room_candidates(options)], ['普通酒店B', '普通酒店A'])
        self.assertEqual(_pick_hotel(options, None).name, '普通酒店B')
        self.assertFalse(_pick_hotel(options, None).quote_confirmed)

    def test_all_shared_results_do_not_fall_back_to_bed_price(self):
        self.assertIsNone(_pick_hotel([hotel('青年公寓', 5, 10), hotel('太空舱', 4, 20)], None))
        self.assertEqual(_room_candidates([]), [])

    def test_low_price_alone_does_not_exclude_or_confirm_a_hotel(self):
        option = hotel('普通酒店', None, 10)
        self.assertEqual(_room_candidates([option]), [option])
        self.assertIsNone(option.price_per_night)
        self.assertFalse(option.quote_confirmed)

    def test_user_verified_private_room_quote_can_override_name_heuristic(self):
        quote = HotelOption.from_confirmed_quote({'hotel_name': '青年公寓独立双人房',
            'room_price_per_night': 200, 'max_guests_per_room': 2, 'source': '用户核实'})
        self.assertEqual(_room_candidates([quote]), [quote])

    def test_all_shared_live_shaped_results_keep_transport_and_missing_budget(self):
        # Recorded fixture data is used only for regression, never as current availability.
        data = json.loads((ROOT / 'examples/offline_replay.json').read_text())
        data.pop('hotel_quote')
        client = FlyAIClient(mode='fixture', fixture_dir=ROOT / 'examples/fixtures')
        original = client.query_cached
        def query(endpoint, **params):
            result = original(endpoint, **params)
            if endpoint == 'search-hotel':
                result.items = [{'name': '漫岛青年公寓', 'price': 10, 'rate': 5}]
            return result
        client.query_cached = query
        req = request_from_dict(data)
        outcome = plan_trip(req, client)
        self.assertEqual(outcome.status, 'needs_confirmation')
        self.assertEqual(outcome.plans[0].team_known_cost, 2400)
        self.assertIsNone(outcome.plans[0].chosen_hotel)
        self.assertEqual(outcome.plans[0].hotels, [])
        self.assertTrue(all(m.budget_state == 'unknown' for m in outcome.plans[0].members))
        self.assertIn('请补查整房住宿', render_outcome(outcome, req))
