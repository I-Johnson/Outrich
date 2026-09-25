"""Offline broker-turn regression battery through the same local session evaluator as the UI.

No Gemini or mail calls. Each case starts a fresh mission/session and sends exactly
one broker turn; these are bounded variants of the three supplied private DAT
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
 ('redwood-team-2800','True team 2800','draft','ambiguous shorthand'),
 ('redwood-team-question','True team?','sent','profile question'),
 ('redwood-team-3500','Can do 3500','sent','low offer'),
 ('redwood-team-4000','Can do $4,000 all in','sent','at floor counter'),
 ('redwood-team-driver','True team? 1. send me full drv info 2. Youre name and number','alert','driver request'),
 ('redwood-call','Please give me a call','alert','call request'),
 ('redwood-driver-variant','send me full driver information','alert','driver request'),
 ('redwood-ratecon','I sent the rate con','alert','rate confirmation'),
 ('redwood-accepted','We accept your $4,500','alert','accepted rate'),
 ('redwood-counter','Could you meet me at $4,200?','sent','counter'),
 ('landmark-dedicated','Starting Monday - Looking for a dedicated carrier. 5 loads a day round trip loads. Hook and live empty back. Rate posted. LMK how many you can cover a day','alert','new dedicated service terms'),
 ('landmark-schedule','What are the delivery schedules?','alert','schedule unknown'),
 ('landmark-factoring','Never mind. Factoring denied the load. Sorry','closed','factoring denied'),
 ('landmark-factoring-variant','Factoring was denied','closed','factoring denied'),
 ('landmark-credit','Our factoring company will not approve your credit','alert','credit rejection'),
 ('landmark-five','Need five loads a day','alert','capacity commitment'),
 ('landmark-hook','Hook and drop empty back','alert','equipment/service terms'),
 ('landmark-monday','Pickup Monday, return Monday','alert','schedule terms'),
 ('landmark-phone','Call me to talk about dedicated lanes','alert','call request'),
 ('landmark-covered','Load is covered','closed','unavailable'),
 ('myfreight-first','Hello. Pick 10am-3pm. Delivery appt 9/17 at 10am. Weight- 40,000. 30 pallets of metal and plastic parts. Rate- 4,000.00. Let me know. Thanks Russ','sent','real broker initial quote'),
 ('myfreight-4200','I cannot, maybe I can squeeze it to 4200','sent','broker counter'),
 ('myfreight-cap','I cannot get that high. Max is 4200 on this one.','sent','broker hard cap'),
 ('myfreight-commodity','30 pallets of parts- metal and plastic','alert','commodity only'),
 ('myfreight-book','Let us book it please','alert','booking intent'),
 ('myfreight-capacity','I am getting it over to capacity to get you squared away','alert','carrier onboarding'),
 ('myfreight-highway','Highway setup is done','alert','onboarding'),
 ('myfreight-driver','Could I get the drivers info and ETA?','alert','driver info'),
 ('myfreight-pu','Please advise if driver has made it on site for PU','alert','post-book tracking'),
 ('myfreight-loaded','Please advise once loaded','alert','post-book tracking'),
 ('myfreight-eta','Could I get the ETA for delivery?','alert','post-book tracking'),
 ('myfreight-bol','Please send BOL','alert','document request'),
 ('myfreight-pod','Please send POD','alert','document request'),
 ('myfreight-accept','Ok you got it at $4,200','alert','price accepted'),
 ('myfreight-no','Cannot get to $4,300','alert','hard cap'),
 ('myfreight-pallets','30 pallets, 40,000 lbs, rate is $4,000','sent','weight and rate'),
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
 ('public-wait','Please hold while I get customer approval','alert','customer approval pending'),
 ('public-photos','Send photos after unloading. Rate $4,000','alert','document obligation'),
 ('public-hazmat','Hazmat endorsement required. Rate $4,000','alert','credential requirement'),
 ('public-detention','Detention payment only after signed BOL','alert','new terms'),
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
