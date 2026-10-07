"""Acceptance checks for Gate5. All writes use isolated temporary databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from app import Store, WebServer, RuleError, business_date, stamp, validate_lend, MESSAGES


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gate5-test-")
        self.now = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)
        self.store = Store(Path(self.temp.name)/"test.db", clock=lambda: self.now)
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, device=1, days=7, purpose="出張"):
        return {"device_id": device, "due_date": (business_date(self.now)+timedelta(days=days)).isoformat(), "purpose": purpose}

    def prepare(self, user=1, device=1):
        return self.store.prepare(user, "LEND", self.payload(device))["request_key"]

    def lend(self, user=1, device=1):
        key = self.prepare(user, device)
        return self.store.commit(user, "LEND", {"request_key": key})

    def sql(self, query, args=()):
        with self.store.connect() as db:
            cur = db.execute(query, args)
            return [dict(r) for r in cur.fetchall()]

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(RuleError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class BusinessTests(Fixture):
    def test_normal_lend_and_return_T01(self):
        result = self.lend()
        self.assertEqual(result["code"], "S-01")
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"], "LENT")
        key = self.store.prepare(1, "RETURN", {"lending_id": result["lending_id"]})["request_key"]
        returned = self.store.commit(1, "RETURN", {"request_key": key})
        self.assertEqual(returned["code"], "S-02")
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"], "AVAILABLE")
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE device_id=1 AND returned_at IS NULL")[0]["n"], 0)

    def test_8_categories_source_messages_match_spec(self):
        spec = (Path(__file__).parent/"Gate5_例外系仕様書.md").read_text(encoding="utf-8")
        for code, message in MESSAGES.items():
            with self.subTest(code=code):
                self.assertIn(message, spec)

    def test_X01_missing_device(self):
        self.error("E-01", self.store.prepare, 1, "LEND", self.payload(999))

    def test_X02_X03_X04_X06_device_conditions(self):
        for device, code in [(3,"E-02"),(4,"E-03"),(10,"E-03"),(21,"E-04"),(22,"E-04")]:
            with self.subTest(device=device):
                self.error(code, self.store.prepare, 1, "LEND", self.payload(device))

    def test_X05_empty_candidates(self):
        self.sql("UPDATE devices SET status='REPAIR' WHERE status='AVAILABLE'")
        self.assertEqual(self.store.dashboard(1)["devices"], [])

    def test_X07_inconsistent_available_and_lent(self):
        self.sql("UPDATE devices SET status='AVAILABLE' WHERE id=3")
        self.error("E-06", self.store.prepare, 1, "LEND", self.payload(3))
        self.error("E-06", self.store.prepare, 3, "RETURN", {"lending_id":1})
        self.sql("UPDATE devices SET status='LENT' WHERE id=1")
        self.error("E-06", self.store.prepare, 1, "LEND", self.payload(1))

    def test_X07_duplicate_legacy_data_quarantined(self):
        # Legacy-stage fixture only: production schema never drops this index.
        self.sql("DROP INDEX one_open_lending_per_device")
        self.sql("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) SELECT device_id,2,lent_at,due_date,purpose FROM lendings WHERE id=1")
        self.error("E-06", self.store.prepare, 1, "LEND", self.payload(3))
        self.error("E-06", self.store.prepare, 3, "RETURN", {"lending_id":1})
        self.assertTrue(next(x for x in self.store.dashboard(3)["loans"] if x["id"]==1)["blocked"])

    def test_X08_holds_exclude_and_block(self):
        candidates={d["id"] for d in self.store.dashboard(1)["devices"]}
        for device in (5,7,9,14,15):
            self.assertNotIn(device,candidates)
            self.error("E-06", self.store.prepare,1,"LEND",self.payload(device))

    def test_X09_X10_nonactive_can_return_not_lend(self):
        for user, loan in [(4,2),(5,3)]:
            self.error("E-07",self.store.prepare,user,"LEND",self.payload())
            key=self.store.prepare(user,"RETURN",{"lending_id":loan})["request_key"]
            self.assertEqual(self.store.commit(user,"RETURN",{"request_key":key})["code"],"S-02")

    def test_X10_employment_rechecked_at_commit(self):
        key=self.prepare()
        self.sql("UPDATE employees SET employment_status='LEAVE' WHERE id=1")
        self.error("E-07",self.store.commit,1,"LEND",{"request_key":key})
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"],"AVAILABLE")

    def test_X11_missing_actor(self):
        self.error("E-08",self.store.prepare,999,"LEND",self.payload())

    def test_X12_limit(self):
        self.lend()
        self.error("E-09",self.store.prepare,1,"LEND",self.payload(2))

    def race(self, actions):
        barrier=threading.Barrier(len(actions))
        def run(fn):
            barrier.wait()
            try:return fn()["code"]
            except RuleError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run,actions))

    def test_X13_two_users_same_device_T02(self):
        k1,k2=self.prepare(1),self.prepare(2)
        result=self.race([lambda:self.store.commit(1,"LEND",{"request_key":k1}),lambda:self.store.commit(2,"LEND",{"request_key":k2})])
        self.assertCountEqual(result,["S-01","E-02"])
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE device_id=1 AND returned_at IS NULL")[0]["n"],1)

    def test_X14_same_user_two_devices_T03(self):
        k1,k2=self.prepare(1,1),self.prepare(1,2)
        result=self.race([lambda:self.store.commit(1,"LEND",{"request_key":k1}),lambda:self.store.commit(1,"LEND",{"request_key":k2})])
        self.assertCountEqual(result,["S-01","E-09"])
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE user_id=1 AND returned_at IS NULL")[0]["n"],1)

    def test_X15_simultaneous_same_key_T04(self):
        key=self.prepare()
        result=self.race([lambda:self.store.commit(1,"LEND",{"request_key":key}) for _ in range(4)])
        self.assertEqual(result,["S-01"]*4)
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE device_id=1")[0]["n"],1)

    def test_X16_X33_durable_receipt_after_restart_T06(self):
        key=self.prepare()
        first=self.store.commit(1,"LEND",{"request_key":key})
        restarted=Store(self.store.path,clock=lambda:self.now+timedelta(days=100))
        self.assertEqual(restarted.result(1,key)["result"],first)
        self.assertEqual(restarted.commit(1,"LEND",{"request_key":key}),first)

    def test_X17_identifiers(self):
        for value in [None,"",0,-1,"a","１",True,1.5,{},[],"9223372036854775808"]:
            with self.subTest(value=value):
                code="E-11" if value is None or value=="" else "E-12"
                self.error(code,self.store.prepare,1,"LEND",self.payload(value))

    def test_X18_X19_dates_required_strict(self):
        for value,code in [("","E-13"),(None,"E-13"),("2026-02-30","E-14"),("2026/09/20","E-14"),("2026-9-20","E-14"),(15,"E-14")]:
            payload=self.payload();payload["due_date"]=value
            self.error(code,self.store.prepare,1,"LEND",payload)

    def test_X20_X21_date_boundaries_T07(self):
        for day in [-1,0,91,3650]:self.error("E-15",self.store.prepare,1,"LEND",self.payload(days=day))
        for day in [1,90]:self.assertIn("request_key",self.store.prepare(1,"LEND",self.payload(days=day)))

    def test_X22_X23_X24_purpose_T08(self):
        for value,code in [("","E-16"),(" \u3000\t\n","E-16"),("あ"*101,"E-17"),("a\nb","E-25"),("a\x00b","E-25"),("a\x85b","E-25")]:
            self.error(code,self.store.prepare,1,"LEND",self.payload(purpose=value))
        for value in ["あ"*100,"😀"*100,"<script>alert(1)</script>","機種依存文字①髙"]:
            request=self.store.prepare(1,"LEND",self.payload(purpose="　"+value+" "))
            self.assertEqual(request["display"]["purpose"],value)

    def test_multiple_field_errors(self):
        error=self.error("E-11",self.store.prepare,1,"LEND",{})
        self.assertEqual(error.fields,{"device_id":"E-11","due_date":"E-13","purpose":"E-16"})

    def test_X25_X36_other_loan_same_generic_message_T10(self):
        for loan in [1,999]:self.error("E-18",self.store.prepare,1,"RETURN",{"lending_id":loan})

    def test_X26_old_return_does_not_change_new_loan_T11(self):
        loan=self.lend()
        key=self.store.prepare(1,"RETURN",{"lending_id":loan["lending_id"]})["request_key"]
        old=self.store.commit(1,"RETURN",{"request_key":key})
        second=self.lend(2,1)
        retry=self.store.prepare(1,"RETURN",{"lending_id":loan["lending_id"]})["request_key"]
        result=self.store.commit(1,"RETURN",{"request_key":retry})
        self.assertEqual(result["code"],"S-03")
        self.assertEqual(result["returned_at"],old["returned_at"])
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"],"LENT")
        self.assertIsNone(self.sql("SELECT returned_at FROM lendings WHERE id=?",(second["lending_id"],))[0]["returned_at"])

    def test_X27_hold_rechecked_at_commit(self):
        key=self.prepare()
        self.sql("INSERT INTO device_holds VALUES(1,'test',?,NULL)",(stamp(self.now),))
        self.error("E-06",self.store.commit,1,"LEND",{"request_key":key})

    def test_X28_X29_overdue_T14(self):
        self.error("E-10",self.store.prepare,3,"LEND",self.payload())
        self.sql("UPDATE lendings SET due_date='2026-09-18' WHERE id=1")
        self.now=datetime(2026,9,18,14,59,tzinfo=timezone.utc)
        self.assertFalse(self.store.dashboard(3)["loans"][0]["overdue"])
        self.now+=timedelta(minutes=1)
        self.assertTrue(self.store.dashboard(3)["loans"][0]["overdue"])
        key=self.store.prepare(3,"RETURN",{"lending_id":1})["request_key"]
        self.assertEqual(self.store.commit(3,"RETURN",{"request_key":key})["code"],"S-02")

    def test_X30_midnight_revalidation_T15(self):
        self.now=datetime(2026,9,18,14,59,tzinfo=timezone.utc)
        key=self.store.prepare(1,"LEND",self.payload(days=1))["request_key"]
        self.now+=timedelta(minutes=2)
        self.error("E-15",self.store.commit,1,"LEND",{"request_key":key})

    def test_X31_lend_second_update_failure_T05(self):
        key=self.prepare()
        self.sql("CREATE TRIGGER fail_device BEFORE UPDATE ON devices BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.error("E-19",self.store.commit,1,"LEND",{"request_key":key})
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE device_id=1")[0]["n"],0)
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"],"AVAILABLE")
        self.assertEqual(self.store.result(1,key)["state"],"READY")

    def test_X32_return_second_update_failure_T05(self):
        loan=self.lend()
        key=self.store.prepare(1,"RETURN",{"lending_id":loan["lending_id"]})["request_key"]
        self.sql("CREATE TRIGGER fail_device BEFORE UPDATE ON devices BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.error("E-19",self.store.commit,1,"RETURN",{"request_key":key})
        self.assertIsNone(self.sql("SELECT returned_at FROM lendings WHERE id=?",(loan["lending_id"],))[0]["returned_at"])
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"],"LENT")
        self.assertEqual(self.store.result(1,key)["state"],"READY")

    def test_receipt_write_failure_rolls_back_everything(self):
        key=self.prepare()
        self.sql("CREATE TRIGGER fail_receipt BEFORE UPDATE ON operation_requests BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.error("E-19",self.store.commit,1,"LEND",{"request_key":key})
        self.assertEqual(self.sql("SELECT status FROM devices WHERE id=1")[0]["status"],"AVAILABLE")
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE device_id=1")[0]["n"],0)

    def test_X32_busy_timeout(self):
        key=self.prepare()
        lock=self.store.connect();lock.execute("BEGIN IMMEDIATE")
        try:
            start=time.monotonic()
            self.error("E-19",self.store.commit,1,"LEND",{"request_key":key})
            self.assertGreaterEqual(time.monotonic()-start,4.5)
        finally:lock.rollback();lock.close()
        self.assertEqual(self.store.result(1,key)["state"],"READY")

    def test_X35_missing_confirmation(self):
        self.error("E-21",self.store.commit,1,"LEND",{})
        self.error("E-21",self.store.commit,1,"LEND",{"request_key":"a"*43})

    def test_X36_X37_X39_server_managed_fields(self):
        key=self.prepare()
        for name,value,code in [("device_id",2,"E-22"),("due_date","2026-10-01","E-22"),("purpose","bad","E-22"),("user_id",2,"E-12"),("status","AVAILABLE","E-24"),("lent_at","2000-01-01","E-12")]:
            self.error(code,self.store.commit,1,"LEND",{"request_key":key,name:value})
        self.assertEqual(self.store.result(1,key)["state"],"READY")

    def test_X38_expiry_exact_and_wrong_operation(self):
        key=self.prepare()
        self.error("E-22",self.store.commit,1,"RETURN",{"request_key":key})
        self.now+=timedelta(minutes=30)
        self.error("E-21",self.store.commit,1,"LEND",{"request_key":key})

    def test_edit_invalidates_old_key(self):
        key=self.prepare();self.store.cancel(1,key)
        self.error("E-21",self.store.commit,1,"LEND",{"request_key":key})
        key2=self.prepare();self.assertNotEqual(key,key2)

    def test_X40_other_person_key(self):
        key=self.prepare()
        self.error("E-23",self.store.commit,2,"LEND",{"request_key":key})
        self.error("E-23",self.store.result,2,key)
        self.error("E-23",self.store.cancel,2,key)

    def test_database_unique_and_foreign_keys(self):
        self.lend()
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) SELECT device_id,2,lent_at,due_date,purpose FROM lendings WHERE device_id=1")
        with self.assertRaises(sqlite3.IntegrityError):self.sql("DELETE FROM devices WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):self.sql("DELETE FROM employees WHERE id=1")


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server=WebServer(("127.0.0.1",0),self.store)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cookie,self.csrf=None,None
        _,body,headers=self.request("GET","/api/bootstrap")
        self.cookie=headers["Set-Cookie"].split(";")[0];self.csrf=body["csrf"]

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        super().tearDown()

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=10)
        h={"Content-Type":"application/json"}
        if self.cookie:h["Cookie"]=self.cookie
        if self.csrf:h["X-CSRF-Token"]=self.csrf
        h.update(headers or {})
        conn.request(method,path,None if body is None else json.dumps(body),h)
        response=conn.getresponse();raw=response.read();status=response.status;out=dict(response.getheaders());conn.close()
        return status,json.loads(raw),out

    def test_X40_csrf_and_origin(self):
        for headers in [{"X-CSRF-Token":"wrong"},{"Origin":"https://attacker.invalid"},{"Host":"evil.invalid"}]:
            status,body,_=self.request("POST","/api/lend/prepare",self.payload(),headers)
            self.assertEqual(status,403);self.assertEqual(body["code"],"E-23")

    def test_X34_session_expiry_and_relogin_receipt(self):
        _,body,_=self.request("POST","/api/lend/prepare",self.payload());key=body["request_key"]
        self.request("POST","/api/lend/commit",{"request_key":key})
        for session in self.server.sessions.values():session["expires"]=0
        status,body,_=self.request("GET","/api/results/"+key)
        self.assertEqual(status,401);self.assertEqual(body["code"],"E-08")
        _,boot,headers=self.request("GET","/api/bootstrap");self.cookie=headers["Set-Cookie"].split(";")[0];self.csrf=boot["csrf"]
        _,body,_=self.request("GET","/api/results/"+key)
        self.assertEqual(body["state"],"SUCCEEDED")

    def test_X39_get_update_no_write(self):
        status,body,_=self.request("GET","/api/lend/commit")
        self.assertEqual(status,405);self.assertEqual(body["code"],"E-24")
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE user_id=1")[0]["n"],0)

    def test_success_query_is_read_only_and_post_is_idempotent(self):
        _,prepared,_=self.request("POST","/api/lend/prepare",self.payload());key=prepared["request_key"]
        self.assertEqual(self.request("GET","/api/results/"+key)[1]["state"],"READY")
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM lendings WHERE user_id=1")[0]["n"],0)
        first=self.request("POST","/api/lend/commit",{"request_key":key})[1]
        second=self.request("POST","/api/lend/commit",{"request_key":key})[1]
        self.assertEqual(first,second)

    def test_invalid_json_shape_and_untrusted_actor(self):
        for body in [[], {**self.payload(),"user_id":2}]:
            status,result,_=self.request("POST","/api/lend/prepare",body)
            self.assertEqual(status,400);self.assertEqual(result["code"],"E-12")


if __name__=="__main__":
    unittest.main(verbosity=2)
