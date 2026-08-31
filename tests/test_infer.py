"""
The inference is the product. These tests are mostly about the ways value
overlap lies to you, because every one of them was a real bug first.
"""

import os
import tempfile

import pytest

from graphify.infer import infer, load, save
from graphify.infer.engine import _humanise
from graphify.ingest import open_source

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")


def build(sql, name="t.sql"):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(sql)
    src = open_source(path)
    return src, infer(src)


def rels(model):
    return {f"{r.from_table}.{r.from_column}": f"{r.to_table}.{r.to_column}"
            for r in model.relationships}


# --------------------------------------------------------------------------

LEGACY = open(os.path.join(EXAMPLES, "legacy_core_banking.sql")).read()


def test_finds_keys_in_a_schema_that_declares_none():
    """Eight-character names, no constraints, no naming convention."""
    src, m = build(LEGACY)
    assert rels(m) == {
        "ACCTMST.OWNR_REF": "CSTMSTR.CST_NO",
        "TXNHIST.DR_ACCT": "ACCTMST.ACCT_NO",
        "TXNHIST.MRCH_REF": "MRCHTBL.MRCH_CD",
    }
    assert all(r.tier == "statistical" for r in m.relationships)
    src.close()


def test_one_to_one_satellites_are_folded_into_their_master():
    """A master plus a preferences table is one thing, not two."""
    src, m = build(LEGACY)
    assert "CSTPREF" not in m.entities
    assert m.entities["CSTMSTR"].extended_by == ["CSTPREF"]
    assert m.entities["CSTMSTR"].key == "CST_NO"
    assert m.entities["CSTMSTR"].label == "CST_NM"      # the name, not the key
    src.close()


def test_a_table_of_events_becomes_edges_not_nodes():
    src, m = build(LEGACY)
    assert "TXNHIST" in m.events
    assert "TXNHIST" not in m.entities
    assert m.events["TXNHIST"].timestamp == "POST_TS"
    assert m.events["TXNHIST"].measure == "TXN_AMT"
    src.close()


# --------------------------------------------------------------------------
# the ways value overlap lies

COUNTERS = """
CREATE TABLE reader (reader_id INTEGER, reader_name VARCHAR(40));
CREATE TABLE shelf (shelf_id INTEGER, shelf_name VARCHAR(40));
INSERT INTO reader VALUES (1,'Ada'),(2,'Bo'),(3,'Cy');
INSERT INTO shelf VALUES (1,'Fiction'),(2,'History'),(3,'Poetry'),(4,'Travel');
"""


def test_two_tables_numbered_from_one_are_not_related():
    """
    reader_id 1..3 sits perfectly inside shelf_id 1..4 and means nothing. A key
    repeats; an identity does not. This produced six false links before.
    """
    src, m = build(COUNTERS)
    assert rels(m) == {}
    src.close()


BORROWED = """
CREATE TABLE accounts (account_id INTEGER, opened VARCHAR(20));
CREATE TABLE cards (card_id INTEGER, account_id INTEGER, pan VARCHAR(20));
CREATE TABLE moves (move_id INTEGER, from_account_id INTEGER, to_account_id INTEGER, sent VARCHAR(20));
INSERT INTO accounts VALUES (1,'2020-01-01'),(2,'2020-02-01'),(3,'2020-03-01'),(4,'2020-04-01');
INSERT INTO cards VALUES (10,1,'1111'),(11,2,'2222'),(12,3,'3333');
INSERT INTO moves VALUES (1,1,2,'2026-01-01'),(2,2,3,'2026-01-02'),(3,1,3,'2026-01-03');
"""


def test_a_key_points_at_an_identity_not_at_another_key():
    """
    from_account_id sits inside cards.account_id as happily as inside
    accounts.account_id. The column names the parent's table; that decides it.
    """
    src, m = build(BORROWED)
    r = rels(m)
    assert r["moves.from_account_id"] == "accounts.account_id"
    assert r["moves.to_account_id"] == "accounts.account_id"
    src.close()


OVERLAP = """
CREATE TABLE ledger (ledger_id INTEGER, note VARCHAR(30));
CREATE TABLE moves (move_id INTEGER, note VARCHAR(30));
INSERT INTO ledger VALUES (1,'a'),(2,'b'),(3,'c'),(4,'d'),(5,'e'),(6,'f'),(7,'g');
INSERT INTO moves VALUES (1,'a'),(2,'b'),(3,'c'),(4,'d'),(5,'e'),(6,'f');
"""


def test_almost_the_same_is_not_the_same_thing():
    """
    Two counters, one to 6 and one to 7, each sit almost entirely inside the
    other. Folding those together used to delete a whole table.
    """
    src, m = build(OVERLAP)
    assert set(m.entities) == {"ledger", "moves"}
    src.close()


TEXTY_DATES = """
CREATE TABLE person (person_id INTEGER, person_name VARCHAR(40));
CREATE TABLE place (place_id INTEGER, place_name VARCHAR(40));
CREATE TABLE visit (visit_id INTEGER, person_id INTEGER, place_id INTEGER,
                    happened_at VARCHAR(40), spend DECIMAL(10,2));
INSERT INTO person VALUES (1,'Ada'),(2,'Bo'),(3,'Cy');
INSERT INTO place VALUES (1,'Cafe'),(2,'Library');
INSERT INTO visit VALUES (1,1,1,'2026-01-02 09:00:00',4.50),(2,1,2,'2026-01-03 10:00:00',0.0),
                         (3,2,1,'2026-01-04 11:00:00',3.25),(4,3,2,'2026-01-05 12:00:00',1.00);
"""


def test_a_timestamp_stored_as_text_is_still_a_timestamp():
    """
    Dumps type everything as VARCHAR. A timestamp then looks like free text with
    one distinct value per row, which is exactly what the label picker wants --
    and a table with a label is not an event.
    """
    src, m = build(TEXTY_DATES)
    assert "visit" in m.events
    assert m.events["visit"].timestamp == "happened_at"
    assert m.events["visit"].measure == "spend"
    src.close()


# --------------------------------------------------------------------------

def test_declared_constraints_win_and_arrive_verified():
    sql = """
    CREATE TABLE owner (owner_id INTEGER PRIMARY KEY, owner_name TEXT);
    CREATE TABLE pet (pet_id INTEGER PRIMARY KEY, pet_name TEXT,
                      owner_id INTEGER REFERENCES owner(owner_id));
    INSERT INTO owner VALUES (1,'Ada'),(2,'Bo');
    INSERT INTO pet VALUES (1,'Rex',1),(2,'Tam',1),(3,'Mo',2);
    """
    src, m = build(sql)
    r = [x for x in m.relationships if x.from_table == "pet"][0]
    assert r.tier == "declared" and r.verified and r.confidence == 1.0
    src.close()


def test_hubs_are_marked_stop_not_the_subject_of_the_schema():
    src = open_source(os.path.join(EXAMPLES, "card_platform.sql"))
    m = infer(src)
    stopping = {t for t, e in m.entities.items() if e.stop}
    assert "merchants" in stopping
    assert "customers" not in stopping        # the thing the schema is about
    src.close()


def test_your_edits_survive_a_rerun():
    src, m = build(LEGACY)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.yaml")
        m.entities["CSTMSTR"].display_name = "Customer"
        for r in m.relationships:
            r.verified = True
        save(m, path)

        _, again = build(LEGACY)
        again.adopt_decisions_from(load(path))
        assert again.entities["CSTMSTR"].display_name == "Customer"
        assert all(r.verified for r in again.relationships)
    src.close()


@pytest.mark.parametrize("raw,pretty", [
    ("accounts", "Account"),
    ("customers", "Customer"),
    ("tbl_merchants", "Merchant"),
    ("CustomerAccounts", "Customer Account"),
    ("ACCTMST", "ACCTMST"),        # no word boundaries to find: leave it alone
])
def test_names_are_tidied_only_when_there_is_structure_to_read(raw, pretty):
    assert _humanise(raw) == pretty
