import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from activity_overlay import parse_service_ends, with_service_ends


@dataclass(frozen=True)
class Batch:
    time_us: int
    sequence: int
    operations: tuple
    profile: str
    active_service_ids: tuple


@dataclass(frozen=True)
class Scenario:
    batches: tuple
    services: tuple
    added_services: tuple


class ActivityEndTests(unittest.TestCase):
    def test_expiry_coinciding_with_event_and_later_native_allowlist(self):
        services = tuple(SimpleNamespace(service_id=s, registered_at_us=0) for s in ("owner", "waiting"))
        native_add = SimpleNamespace(service_id="addition", registered_at_us=12_000_000)
        def allowed(*ids):
            return {"kind":"traffic_set", "active_service_ids":list(ids)}
        event = {"kind":"official_event", "event_id":"original"}
        scenario = Scenario((
            Batch(0,0,(allowed("owner","waiting"),),"official",("owner","waiting")),
            Batch(10_000_000,1,(event,),"official",("owner","waiting")),
            Batch(12_000_000,2,(allowed("owner","waiting","addition"),),"official",("owner","waiting","addition")),
        ),services,(native_add,))
        self.assertIs(with_service_ends(scenario,{}),scenario)
        result=with_service_ends(scenario,{"owner":10,"addition":15})
        self.assertEqual([b.time_us for b in result.batches],[0,10_000_000,12_000_000,15_000_000])
        self.assertEqual(result.batches[1].operations[0],event)
        self.assertEqual(result.batches[1].operations[1]["stop_new_packets_service_ids"],["owner"])
        self.assertEqual(result.batches[2].active_service_ids,("addition","waiting"))
        self.assertEqual(result.batches[2].operations[-1]["activate_service_ids"],["addition"])
        self.assertEqual(result.batches[-1].active_service_ids,("waiting",))
        self.assertEqual(scenario.batches[1].active_service_ids,("owner","waiting"))

    def test_validate_cutoff_against_selected_service_registration_and_window(self):
        specs={"request":SimpleNamespace(registered_at_us=3_000_000)}
        self.assertEqual(parse_service_ends(["request=4"],specs,set(specs),2,10),{"request":4})
        for values in (["request=3"],["request=10"],["other=4"],["request=4","request=5"],["request=4.5"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_service_ends(values,specs,set(specs),2,10)
