"""Replay harness: recorded broker conversations run through the real evaluator.

Every broker turn must produce a visible outcome. Fixtures live in
tests/fixtures/replay/*.json and pin the expected action/state/alerts per turn.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-freight-replay-test.db"
os.environ["FREIGHT_AGENT_MODE"] = "rules"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core.freight_replay import load_fixtures, run_replay, validate_replay
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"


class FreightReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/replay.db'), ADMIN_OWNER_ID)
        self.storage.init()
        stamp = now_iso()
        self.profile_id, self.mission_id = new_id(), new_id()
        self.storage.insert('freight_truck_profiles', {'id': self.profile_id, 'name': 'Test team', 'current_city': 'Phoenix', 'current_state': 'AZ', 'equipment_type': 'dry van', 'max_weight_lbs': 45000, 'team_status': 'team', 'shareable_fields': ['team_status', 'equipment_type'], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': self.mission_id, 'name': 'Phoenix to Dallas', 'truck_profile_id': self.profile_id, 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'equipment_type': 'dry van', 'destinations': [{'kind': 'city', 'label': 'Dallas, TX', 'radius_miles': 0}], 'floor_total': 3900, 'target_total': 4500, 'maximum_counter_rounds': 2, 'permissions': {'auto_profile_reply': True, 'auto_counter': True, 'auto_pass': True}, 'active': True, 'created_at': stamp, 'updated_at': stamp})

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_replay_fixtures(self):
        fixtures = load_fixtures(FIXTURE_DIR)
        self.assertGreaterEqual(len(fixtures), 5, "expected replay fixtures on disk")
        for fixture in fixtures:
            fixture["mission_id"] = self.mission_id
            with self.subTest(fixture=fixture["name"]):
                with patch('app.core.freight.settings.FREIGHT_AGENT_MODE', 'rules'):
                    replay = run_replay(self.storage, fixture)
                violations = validate_replay(replay)
                self.assertEqual(violations, [], f"{fixture['name']}: " + "; ".join(violations))

    def test_acceptance_records_booking_snapshot(self):
        fixture = next(f for f in load_fixtures(FIXTURE_DIR) if "accepted price" in f["name"])
        fixture["mission_id"] = self.mission_id
        with patch('app.core.freight.settings.FREIGHT_AGENT_MODE', 'rules'):
            replay = run_replay(self.storage, fixture)
        bookings = self.storage.list('freight_bookings', order='', limit=10)
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0]['agreed_rate'], 4200.0)
        self.assertEqual(bookings[0]['status'], 'agreed')
        self.assertTrue(bookings[0]['snapshot']['agreed_at'])
