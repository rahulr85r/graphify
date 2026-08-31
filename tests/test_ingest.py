"""Dumps in the wild are written by four different tools with four different ideas."""

import os
import tempfile

import pytest

from graphify.ingest import open_source


def write(tmp, name, sql):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(sql)
    return path


PG = """
--
-- PostgreSQL database dump
--
SET statement_timeout = 0;
SET client_encoding = 'UTF8';

CREATE TABLE public."authors" (
    author_id integer NOT NULL,
    "name" text NOT NULL,
    country character varying(2)
);
CREATE TABLE public.books (
    book_id integer NOT NULL,
    title text,
    author_id integer,
    price numeric(10,2)
);
ALTER TABLE ONLY public."authors" ADD CONSTRAINT authors_pkey PRIMARY KEY (author_id);
CREATE INDEX books_author_idx ON public.books USING btree (author_id);
ALTER TABLE ONLY public.books OWNER TO someone;

INSERT INTO public."authors" (author_id, "name", country) VALUES
 (1, 'Ursula K. Le Guin', 'US'), (2, 'Italo Calvino', 'IT'), (3, 'O''Brien, Flann', 'IE');
INSERT INTO public.books (book_id, title, author_id, price) VALUES
 (1, 'The Dispossessed', 1, 12.99), (2, 'Invisible Cities', 2, 10.50),
 (3, 'The Third Policeman', 3, 9.75), (4, 'A Wizard of Earthsea', 1, 8.00);
"""

MYSQL = """
/*!40101 SET NAMES utf8 */;
DROP TABLE IF EXISTS `teams`;
CREATE TABLE `teams` (
  `team_id` int(11) NOT NULL AUTO_INCREMENT,
  `team_name` varchar(80) DEFAULT NULL,
  PRIMARY KEY (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE `players` (
  `player_id` int(11) NOT NULL AUTO_INCREMENT,
  `player_name` varchar(80) DEFAULT NULL,
  `team_id` int(11) DEFAULT NULL,
  PRIMARY KEY (`player_id`),
  KEY `fk_team` (`team_id`)
) ENGINE=InnoDB;
INSERT INTO `teams` VALUES (1,'Rovers'),(2,'City'),(3,'United');
INSERT INTO `players` VALUES (1,'A. Byrne',1),(2,'B. Nolan',1),(3,'C. Walsh',2),(4,'D. Keane',3);
"""

SQLITE_DUMP = """
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE venues (venue_id INTEGER PRIMARY KEY, venue_name TEXT);
CREATE TABLE gigs (
  gig_id INTEGER PRIMARY KEY,
  venue_id INTEGER REFERENCES venues(venue_id),
  gig_date TEXT,
  takings REAL
);
INSERT INTO venues VALUES(1,'Vicar Street');
INSERT INTO venues VALUES(2,'Whelans');
INSERT INTO gigs VALUES(1,1,'2026-03-04',18400.0);
INSERT INTO gigs VALUES(2,2,'2026-03-11',5200.5);
INSERT INTO gigs VALUES(3,1,'2026-04-02',21000.0);
COMMIT;
"""


@pytest.mark.parametrize("name,sql,expect", [
    ("pg.sql", PG, {"authors": 3, "books": 4}),
    ("my.sql", MYSQL, {"teams": 3, "players": 4}),
    ("lite.sql", SQLITE_DUMP, {"venues": 2, "gigs": 3}),
])
def test_dialects_load(name, sql, expect):
    with tempfile.TemporaryDirectory() as tmp:
        src = open_source(write(tmp, name, sql))
        assert {t: m["rows"] for t, m in src.tables.items()} == expect
        src.close()


def test_quoted_apostrophes_survive():
    with tempfile.TemporaryDirectory() as tmp:
        src = open_source(write(tmp, "pg.sql", PG))
        names = [r[0] for r in src.db.execute('SELECT "name" FROM authors ORDER BY author_id')]
        assert "O'Brien, Flann" in names
        src.close()


def test_declared_constraints_are_read():
    with tempfile.TemporaryDirectory() as tmp:
        src = open_source(write(tmp, "lite.sql", SQLITE_DUMP))
        assert src.tables["gigs"]["declared_fks"] == [
            {"column": "venue_id", "table": "venues", "to": "venue_id"}]
        src.close()


def test_a_comment_cannot_swallow_the_statement_after_it():
    """A leading comment line used to glue itself onto the next CREATE TABLE."""
    sql = "-- a note about the extract\n-- and another\n" + SQLITE_DUMP
    with tempfile.TemporaryDirectory() as tmp:
        src = open_source(write(tmp, "c.sql", sql))
        assert set(src.tables) == {"venues", "gigs"}
        src.close()


def test_empty_file_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="Nothing loaded"):
            open_source(write(tmp, "empty.sql", "-- nothing here\n"))
