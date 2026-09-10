import os
import tempfile
import unittest
import uuid
from pathlib import Path

SANDBOX = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = SANDBOX.name
os.environ["APP_ENV"] = "test"

from backend.db import Database
from backend.schema import COLUMNS
from backend.security import AppError


class PersonRelationMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.sqlite3"
        self.db = Database("sqlite:///" + str(self.path))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def legacy_schema(self):
        self.db.execute(
            "CREATE TABLE clients (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, created_by TEXT, deleted_at TEXT)"
        )
        self.db.execute(
            "CREATE TABLE persons (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, created_by TEXT, deleted_at TEXT, "
            "client_id TEXT REFERENCES clients(id), last_name_ru TEXT)"
        )
        self.db.execute("CREATE INDEX ix_persons_client_id ON persons(client_id)")

    def add_client(self):
        client_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO clients (id,created_at,updated_at) VALUES (?,?,?)",
            (client_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        return client_id

    def add_person(self, client_id, deleted_at=None):
        person_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO persons (id,created_at,updated_at,deleted_at,client_id,last_name_ru) "
            "VALUES (?,?,?,?,?,?)",
            (person_id,"2026-01-02T00:00:00+00:00","2026-01-02T00:00:00+00:00",deleted_at,client_id,"synthetic-encrypted-placeholder"),
        )
        return person_id

    def test_backfills_unknown_relation_then_drops_legacy_column(self):
        self.legacy_schema(); client_id=self.add_client(); person_id=self.add_person(client_id)
        self.db.migrate()
        self.assertNotIn("client_id",self.db.table_columns("persons")); self.assertIn("person_id",self.db.table_columns("clients"))
        relation=self.db.find("client_person_relations","client_id=? AND person_id=?",(client_id,person_id))
        self.assertEqual(relation["relation_type"],"unknown"); self.assertEqual(relation["status"],"review_required")
        self.assertEqual(relation["source"],"legacy_person_client_id"); self.assertEqual(relation["is_primary"],0)
        self.assertIsNone(self.db.execute("SELECT person_id FROM clients WHERE id=?",(client_id,)).fetchone()[0])
        event=self.db.find("data_migration_events","entity_id=?",(person_id,))
        self.assertEqual(event["outcome"],"backfilled_review_required"); self.assertEqual(event["details"]["semantic_status"],"unknown_requires_review")

    def test_migration_is_idempotent(self):
        self.legacy_schema(); client_id=self.add_client(); person_id=self.add_person(client_id)
        self.db.migrate(); self.db.migrate()
        self.assertEqual(len(self.db.all("client_person_relations","client_id=? AND person_id=?",(client_id,person_id))),1)
        self.assertEqual(len(self.db.all("data_migration_events","entity_id=?",(person_id,))),1)

    def test_existing_relation_is_preserved_without_unknown_duplicate(self):
        self.legacy_schema(); client_id=self.add_client(); person_id=self.add_person(client_id)
        self.db.execute("CREATE TABLE client_person_relations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT, deleted_at TEXT, client_id TEXT, person_id TEXT, relation_type TEXT, is_primary BIGINT, status TEXT, valid_from TEXT, valid_to TEXT)")
        relation_id=str(uuid.uuid4())
        self.db.execute("INSERT INTO client_person_relations (id,created_at,updated_at,client_id,person_id,relation_type,is_primary,status,valid_from) VALUES (?,?,?,?,?,?,?,?,?)",(relation_id,"2026-01-03T00:00:00+00:00","2026-01-03T00:00:00+00:00",client_id,person_id,"traveler",0,"active","2026-01-03T00:00:00+00:00"))
        self.db.migrate()
        rows=self.db.all("client_person_relations","client_id=? AND person_id=?",(client_id,person_id))
        self.assertEqual([(row["id"],row["relation_type"]) for row in rows],[(relation_id,"traveler")])
        self.assertEqual(self.db.find("data_migration_events","entity_id=?",(person_id,))["outcome"],"already_linked")

    def test_multiple_people_do_not_invent_primary_person(self):
        self.legacy_schema(); client_id=self.add_client(); people=[self.add_person(client_id),self.add_person(client_id)]
        self.db.migrate(); rows=self.db.all("client_person_relations")
        self.assertEqual(len(rows),2); self.assertTrue(all(row["is_primary"]==0 for row in rows))
        self.assertIsNone(self.db.execute("SELECT person_id FROM clients WHERE id=?",(client_id,)).fetchone()[0])
        self.assertEqual({row["person_id"] for row in rows},set(people))

    def test_orphan_stops_and_rolls_back_before_column_drop(self):
        self.legacy_schema(); self.db.execute("PRAGMA foreign_keys=OFF")
        orphan_person=self.add_person(str(uuid.uuid4())); self.db.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(AppError) as error:self.db.migrate()
        self.assertIn("client_id",self.db.table_columns("persons")); self.assertEqual(error.exception.details,{"person_id":orphan_person})

    def test_fresh_schema_rejects_person_client_id(self):
        self.db.migrate(); self.assertNotIn("client_id",COLUMNS["persons"])
        with self.assertRaises(AppError):self.db.insert("persons",{"client_id":str(uuid.uuid4()),"last_name_ru":"Синтетиков","first_name_ru":"Тест","birth_date":"2000-01-01"})

    def test_migration_evidence_is_immutable(self):
        self.legacy_schema(); self.add_person(self.add_client()); self.db.migrate()
        with self.assertRaises(Exception):self.db.execute("DELETE FROM data_migration_events")

if __name__ == "__main__":unittest.main()
