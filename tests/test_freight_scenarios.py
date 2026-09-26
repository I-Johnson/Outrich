"""Offline broker-turn regression battery through the same local session evaluator as the UI.

No Gemini or mail calls. Each case starts a fresh mission/session and sends exactly
one broker turn; these are synthetic variants inspired by user-supplied private DAT
threads and public dispatcher call transcripts, not 60 independent real threads.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ['DATABASE_BACKEND'] = 'sqlite'
os.environ['SCHEDULER_ENABLED'] = 'false'
os.environ.setdefault('ENCRYPTION_KEY', 'test-encryption-key')
from app.core.agent_test import create_local_test_session, inject_broker_reply
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso

# (id, broker message, expected decision, issue represented)
CASES = [
 ('thread-a-team-2800','Team available? Quoted 2700','draft','ambiguous shorthand'),
 ('thread-a-team-question','True team?','sent','profile question'),
 ('thread-a-team-3500','Can do 3450','sent','low offer'),
 ('thread-a-team-4000','Can do $4,100 all in','sent','at floor counter'),
 ('thread-a-team-driver','Do you have two drivers? Send their details.','alert','driver request'),
 ('thread-a-call','Please give me a call','alert','call request'),
 ('thread-a-driver-variant','send me full driver information','alert','driver request'),
 ('thread-a-ratecon','I sent the rate con','alert','rate confirmation'),
 ('thread-a-accepted','We accept your $4,500','alert','accepted rate'),
 ('thread-a-counter','Could you meet me at $4,200?','sent','counter'),
 ('thread-b-dedicated','Dedicated lane requires five trips daily; can you commit to a hook and live-empty return?','alert','new dedicated service terms'),
 ('thread-b-schedule','What are the delivery schedules?','draft','schedule question gets manual keep-alive draft'),
 ('thread-b-factoring','The funding provider declined this load. Factoring denied the load.','closed','factoring denied'),
 ('thread-b-factoring-variant','Factoring was denied','closed','factoring denied'),
 ('thread-b-credit','Our factoring company will not approve your credit','alert','credit rejection'),
 ('thread-b-five','Need five loads a day','draft','capacity question gets manual keep-alive draft'),
 ('thread-b-hook','Hook and drop empty back','alert','equipment/service terms'),
 ('thread-b-monday','Pickup Monday, return Monday','draft','schedule terms get manual keep-alive draft'),
 ('thread-b-phone','Call me to talk about dedicated lanes','alert','call request'),
 ('thread-b-covered','Load is covered','closed','unavailable'),
 ('thread-c-first','Pickup window 8 AM to noon. Delivery appointment 9/20 at 9 AM. Weight 38,000 lbs. Twenty pallets of manufactured parts. Rate $4,100.','sent','real broker initial quote'),
 ('thread-c-4200','Can do 4250 on this shipment.','sent','broker counter'),
 ('thread-c-cap','Max is 4250 for this shipment.','sent','broker hard cap'),
 ('thread-c-commodity','Thirty pallets of manufactured components.','draft','commodity details get manual keep-alive draft'),
 ('thread-c-book','Let us book it please','alert','booking intent'),
 ('thread-c-capacity','Our carrier-setup department is reviewing the packet.','draft','onboarding update gets manual keep-alive draft'),
 ('thread-c-highway','Carrier verification portal is complete.','draft','onboarding update gets manual keep-alive draft'),
 ('thread-c-driver','Please send driver details and arrival time.','alert','driver info'),
 ('thread-c-pu','Has the driver checked in at pickup?','draft','post-book question gets manual keep-alive draft'),
 ('thread-c-loaded','Tell us when the trailer is loaded.','draft','post-book question gets manual keep-alive draft'),
 ('thread-c-eta','What is the estimated delivery arrival?','draft','post-book question gets manual keep-alive draft'),
 ('thread-c-bol','Please send BOL','alert','document request'),
 ('thread-c-pod','Please send POD','alert','document request'),
 ('thread-c-accept','Ok you got it at $4,200','alert','price accepted'),
 ('thread-c-no','Cannot get to $4,300','alert','hard cap'),
 ('thread-c-pallets','Thirty pallets weighing 40,000 lbs; all-in rate is $4,000','sent','weight and rate'),
 ('public-twic','Driver must have a TWIC for pickup at the port. Rate $4,000','alert','TWIC credential requirement'),
 ('public-permits','Carrier needs to obtain oversize permits and escorts. Rate $4,000','alert','permit obligation'),
 ('public-self-load','Driver has to operate equipment and self-load it. Rate $4,000','alert','self-load obligation'),
 ('public-ppe','PPE required on site. Rate $4,000','alert','PPE requirement'),
 ('public-multidrop','One pickup and two drops. Rate $4,000','alert','multi-stop'),
 ('public-multipick','Three pickups in Hoopeston, Joliet and Rochelle. Rate $4,000','alert','multi-stop'),
 ('public-temp','Reefer 34F continuous for the entire run. Rate $4,000','alert','temperature obligation'),
 ('public-credit','We have a C credit rating. Rate $4,000','alert','broker credit'),
 ('public-authority','Must have authority for at least 90 days. Rate $4,000','alert','authority requirement'),
 ('public-hometime','Monday pickup, driver needs Sunday home time. Rate $4,000','alert','schedule mismatch'),
 ('public-legal-weight','Load is 45,000 lbs, but your reefer cannot legally take it. Rate $4,000','alert','weight mismatch'),
 ('public-mile-math','Broker says $20/mile for 250 miles on $1,800 total','draft','conflicting amounts'),
 ('public-wait','Please hold while I get customer approval','draft','hold request gets manual keep-alive draft'),
 ('public-photos','Send photos after unloading. Rate $4,000','alert','document obligation'),
 ('public-hazmat','Hazmat endorsement required. Rate $4,000','alert','credential requirement'),
 ('public-detention','Detention payment only after signed BOL','draft','new terms get manual keep-alive draft'),
 ('rate-unambiguous','Rate is $4,000 all in','sent','standard offer'),
 ('rate-separate-miles','1,000 loaded miles, 100 deadhead miles. Rate $4,000 all in','sent','distance/rate extraction'),
 ('rate-per-mile','Rate is $3 per mile','sent','per-mile clarify'),
 ('rate-range','We can do $4,000 or $4,200','draft','multiple rates'),
 ('rate-phone','Call me at 602-555-1212 to discuss $4,000','alert','phone request'),
 ('rate-quoted','Rate is $4,000\nOn Sep 24, 2026, Broker wrote:\nRate is $5,000','sent','quoted history ignored'),
 ('rate-lane','Delivery to Newark, NJ. Rate is $4,000','alert','wrong lane'),
 ('rate-equipment','Requires reefer. Rate is $4,000','alert','wrong equipment'),
 ('rate-covered','Already booked, load no longer available','closed','closed load'),
]
assert len(CASES) == 61

class BrokerScenarioBattery(unittest.TestCase):
    def test_sixty_one_broker_turns(self):
        for name, body, expected, rationale in CASES:
            with self.subTest(name=name, rationale=rationale), tempfile.TemporaryDirectory() as tmp:
                storage = OwnerStore(SQLiteStore(tmp+'/scenario.db'), ADMIN_OWNER_ID)
                storage.init()
                profile_id, mission_id = new_id(), new_id()
                stamp = now_iso()
                storage.insert('freight_truck_profiles', {'id':profile_id,'name':'Test team','current_city':'Phoenix','current_state':'AZ','equipment_type':'dry van','max_weight_lbs':45000,'team_status':'team','shareable_fields':['team_status','equipment_type'],'active':True,'created_at':stamp,'updated_at':stamp})
                storage.insert('freight_missions', {'id':mission_id,'name':'Phoenix to Dallas','truck_profile_id':profile_id,'origin_city':'Phoenix','origin_state':'AZ','equipment_type':'dry van','destinations':[{'kind':'city','label':'Dallas, TX','radius_miles':0}], 'floor_total':3900,'target_total':4500,'maximum_counter_rounds':2,'permissions':{'auto_profile_reply':True,'auto_counter':True,'auto_pass':True},'active':True,'created_at':stamp,'updated_at':stamp})
                with patch('app.core.freight.settings.FREIGHT_AGENT_MODE','rules'):
                    state = create_local_test_session(storage, {'mission_id':mission_id})
                    result = inject_broker_reply(storage, state['load']['id'], body)
                actual = result['decision']['action']
                self.assertEqual(actual, expected, f'{name}: {body!r}: {result["decision"].get("summary") or result["decision"].get("draft",{}).get("body_text")}')
                self.assertEqual(result['state']['transport'],'local')
                if name in {'thread-a-team-2800', 'thread-b-dedicated', 'thread-c-no'}:
                    self.assertEqual(storage.list('freight_negotiation_events', {'thread_id': result['state']['thread']['id']}, order='', limit=10), [])
                    self.assertIsNone(result['state']['load']['current_offer'])
                    if name != 'thread-a-team-2800':
                        self.assertEqual(result['state']['pending_drafts'], [])

    def test_model_misses_are_stopped_by_source_text_guard(self):
        cases = [
            ('Team available? Quoted 2700', 'draft', 'clarify_rate'),
            ('Dedicated lane requires five trips daily; can you commit to a hook and live-empty return?', 'alert', 'operational_terms'),
            ('Cannot get to $4,300', 'alert', 'rate_refused'),
        ]
        for body, action, reason in cases:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                storage = OwnerStore(SQLiteStore(tmp+'/model-safety.db'), ADMIN_OWNER_ID)
                storage.init()
                profile_id, mission_id = new_id(), new_id()
                stamp = now_iso()
                storage.insert('freight_truck_profiles', {'id':profile_id, 'name':'Test team', 'current_city':'Phoenix', 'current_state':'AZ', 'equipment_type':'dry van', 'max_weight_lbs':45000, 'team_status':'team', 'shareable_fields':['team_status','equipment_type'], 'active':True, 'created_at':stamp, 'updated_at':stamp})
                storage.insert('freight_missions', {'id':mission_id, 'name':'Phoenix to Dallas', 'truck_profile_id':profile_id, 'origin_city':'Phoenix', 'origin_state':'AZ', 'equipment_type':'dry van', 'destinations':[{'kind':'city','label':'Dallas, TX','radius_miles':0}], 'floor_total':3900, 'target_total':4500, 'maximum_counter_rounds':2, 'permissions':{'auto_profile_reply':True,'auto_counter':True,'auto_pass':True}, 'active':True, 'created_at':stamp, 'updated_at':stamp})
                model_guess = {'kind':'offer', 'intent':'offer', 'source':'gemini', 'summary':'Model missed the protected nuance.', 'protected':[], 'questions':[], 'offer':2700 if 'Quoted' in body else 4300 if 'Cannot' in body else None, 'rate_per_mile':None, 'ambiguous_offer':False, 'numeric_facts':[], 'origin':None, 'destination':None, 'equipment':None, 'pickup_date':None, 'required_equipment':None, 'suggested_reply':'What is the delivery city and state?'}
                with patch('app.core.freight.settings.FREIGHT_AGENT_MODE','gemini'), patch('app.core.freight.interpret_broker_reply', return_value=model_guess):
                    state = create_local_test_session(storage, {'mission_id':mission_id})
                    result = inject_broker_reply(storage, state['load']['id'], body)
                self.assertEqual(result['decision']['action'], action)
                if action == 'draft':
                    self.assertEqual(result['decision']['draft']['reason'], reason)
                    self.assertTrue(result['decision']['draft']['policy_snapshot']['manual_only'])
                    self.assertIsNone(result['decision']['classification']['offer'])
                else:
                    self.assertIn(reason, result['decision']['classification']['protected'])
                    self.assertEqual(result['state']['pending_drafts'], [])
                self.assertIsNone(result['state']['load']['current_offer'])
                self.assertEqual(storage.list('freight_negotiation_events', {'thread_id':result['state']['thread']['id']}, order='', limit=10), [])
