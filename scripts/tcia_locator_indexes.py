"""Additive lookup indexes on existing TCIA metadata tables (no new artifact)."""
PATHDB_URL_PREDICATE = "managed_system = 'tcia_pathdb' AND access_url IS NOT NULL"
PATHDB_URL_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_pnd_pathdb_file_url "
    "ON public_non_dicom_locations(access_url) WHERE " + PATHDB_URL_PREDICATE
)
CONTROLLED_FILE_ID_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_controlled_files_file_id ON controlled_files(file_id)"
)
