"""Resolver index creation must preserve existing full/compact artifact layouts."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import tcia_public_non_dicom_metadata as public
from scripts import tcia_controlled_access_metadata as controlled
from scripts import tcia_v2_audit as audit
from scripts.tcia_locator_indexes import PATHDB_URL_INDEX_SQL


class LocatorIndexTests(unittest.TestCase):
    def test_fresh_and_existing_index_install_is_idempotent(self):
        with closing(sqlite3.connect(':memory:')) as c:
            c.executescript(public.SCHEMA)
            c.execute(PATHDB_URL_INDEX_SQL)
            self.assertIsNotNone(c.execute("select name from sqlite_master where name='idx_pnd_pathdb_file_url'").fetchone())
            self.assertIn('public_non_dicom_locations',c.execute("select sql from sqlite_master where name='idx_pnd_pathdb_file_url'").fetchone()[0])
        with closing(sqlite3.connect(':memory:')) as c:
            c.executescript(controlled.SCHEMA_SQL)
            self.assertIsNotNone(c.execute("select name from sqlite_master where name='idx_controlled_files_file_id'").fetchone())

    def test_url_index_survives_compact_research_projection(self):
        with tempfile.TemporaryDirectory() as d:
            source,research=Path(d)/'source.sqlite',Path(d)/'research.sqlite'
            with closing(sqlite3.connect(source)) as c,c:
                c.executescript(public.SCHEMA)
                public.insert_vocab(c)
                c.execute("INSERT INTO artifact_meta VALUES('schema_version','9')")
            audit.create_research_projection(source,research,artifact='public_non_dicom')
            with closing(sqlite3.connect(research)) as c:
                self.assertNotIn('source_url',{r[1] for r in c.execute('pragma table_info(public_non_dicom_assets)')})
                plan=c.execute("EXPLAIN QUERY PLAN SELECT asset_id FROM public_non_dicom_locations WHERE managed_system='tcia_pathdb' AND access_url IS NOT NULL AND access_url=?",('http://example.org/test.tif',)).fetchall()
                self.assertIn('idx_pnd_pathdb_file_url',str(plan))
                self.assertEqual(c.execute('pragma foreign_key_check').fetchall(),[])
