"""Apply finite evidence-bound participant mappings, never global ID heuristics."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DEFAULT_REFERENCE = Path(__file__).resolve().parents[1] / 'references/reviewed_participant_identity_v1.json'


def merge_alias(conn, alias_key, canonical_key):
    """Move all membership references without changing raw IDs or asset IDs."""
    for table in ('participant_identifiers', 'participant_assets',
                  'participant_clinical_values', 'participant_identity_evidence'):
        conn.execute(f'UPDATE {table} SET participant_key=? WHERE participant_key=?',
                     (canonical_key, alias_key))
    for column in ('analysis_result_participant_key', 'source_collection_participant_key'):
        conn.execute(f'UPDATE participant_source_links SET {column}=? WHERE {column}=?',
                     (canonical_key, alias_key))
    for column in ('left_participant_key', 'right_participant_key'):
        conn.execute(f'UPDATE participant_equivalences SET {column}=? WHERE {column}=?',
                     (canonical_key, alias_key))
    conn.execute('DELETE FROM participants WHERE participant_key=?', (alias_key,))


def apply_cbis_ddsm_aliases(conn, clinical_db, *, api):
    """Apply CBIS's documented case IDs only to patients in official source rows.

    TCIA explicitly documents multiple DICOM PatientIDs per participant. This
    dataset-specific grammar is never applied to other collections or bare IDs.
    """
    counts = {'cbis_ddsm_aliases': 0}
    if not clinical_db.is_file():
        return counts
    with closing(sqlite3.connect(f'file:{clinical_db}?mode=ro', uri=True)) as source:
        if not api.table_exists(source, 'clinical_rows'):
            return counts
        owners = {}
        for subject_id, table, payload, digest in source.execute(
            "SELECT subject_id,table_name,row_json,row_sha256 FROM clinical_rows "
            "WHERE short_title='CBIS-DDSM'"):
            if table not in {
                f'cbis_ddsm_{kind}_case_description_{split}_set'
                for kind in ('calc', 'mass') for split in ('train', 'test')
            }:
                continue
            row = json.loads(payload)
            # Require the source's own patient field, not an inferred owner.
            if row.get('patient_id') != subject_id or not re.fullmatch(r'P_\d{5}', subject_id):
                raise RuntimeError('CBIS-DDSM clinical patient identity mismatch')
            if not any(str(row.get(field, '')).startswith(
                (f'Calc-Test_{subject_id}_', f'Calc-Training_{subject_id}_',
                 f'Mass-Test_{subject_id}_', f'Mass-Training_{subject_id}_'))
                for field in ('image_file_path', 'cropped_image_file_path', 'roi_mask_file_path')):
                raise RuntimeError('CBIS-DDSM source row lacks its patient-specific image path')
            owners.setdefault(subject_id, []).append(digest)
    pattern = re.compile(
        r'(?:(?:Calc|Mass)-(?:Test|Training)_)?(P_\d{5})_'
        r'(?:LEFT|RIGHT)_(?:CC|MLO)(?:_\d+)?(?:\.dcm)?')
    rows = conn.execute("SELECT participant_key,display_participant_id FROM participants "
                        "WHERE dataset_type='Collection' AND short_title='CBIS-DDSM'").fetchall()
    for alias_key, raw in rows:
        match = pattern.fullmatch(raw)
        if not match or match[1] not in owners:
            continue
        canonical_key = api.participant_key('Collection', 'CBIS-DDSM', match[1])
        if not conn.execute('SELECT 1 FROM participants WHERE participant_key=?',
                            (canonical_key,)).fetchone():
            continue
        merge_alias(conn, alias_key, canonical_key)
        method = 'official_cbis_ddsm_case_identifier_crosswalk'
        conn.execute("UPDATE participants SET within_dataset_identity_status='resolved',"
                     "identity_resolution_method=? WHERE participant_key=?", (method, canonical_key))
        conn.execute("INSERT OR REPLACE INTO participant_identity_evidence VALUES "
                     "(?,?,'within_dataset',?,'resolved','high',?,?)",
                     (api.stable_id('identity_evidence', method, alias_key), canonical_key, method,
                      'TCIA documents multiple scan-specific PatientIDs per CBIS-DDSM participant.',
                      api.json_dumps({'source_url': 'https://www.cancerimagingarchive.net/collection/cbis-ddsm/',
                                      'raw_identifier': raw, 'participant_id': match[1],
                                      'source_row_sha256': sorted(set(owners[match[1]]))})))
        counts['cbis_ddsm_aliases'] += 1
    return counts


def load_reference(path=DEFAULT_REFERENCE):
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('records'), list):
        raise ValueError('Unsupported reviewed participant identity input')
    seen = set()
    alias_targets = {}
    for row in data['records']:
        kind = row['kind']
        if kind not in {'source_link', 'replace_source_links', 'within_dataset_alias', 'collection_equivalence'}:
            raise ValueError('Unknown reviewed identity operation')
        for side in ('left', 'right'):
            endpoint = row[side]
            if endpoint['dataset_type'] not in {'Collection', 'Analysis Result'} or not endpoint['short_title'] or not endpoint['identifier']:
                raise ValueError('Incomplete dataset-scoped endpoint')
        left, right = row['left'], row['right']
        if kind == 'within_dataset_alias' and (left['dataset_type'], left['short_title']) != (right['dataset_type'], right['short_title']):
            raise ValueError('Alias crosses dataset scopes')
        if kind == 'within_dataset_alias':
            alias = (left['dataset_type'], left['short_title'], left['identifier'].casefold())
            target = right['identifier'].casefold()
            if alias in alias_targets and alias_targets[alias] != target:
                raise ValueError('Ambiguous reviewed alias target')
            alias_targets[alias] = target
        if kind in {'source_link', 'replace_source_links'} and (left['dataset_type'], right['dataset_type']) != ('Analysis Result', 'Collection'):
            raise ValueError('Source link endpoint contract violated')
        if kind == 'collection_equivalence' and (left['dataset_type'] != 'Collection' or right['dataset_type'] != 'Collection' or left['short_title'] == right['short_title']):
            raise ValueError('Collection equivalence endpoint contract violated')
        if not row['method'] or not row['evidence']:
            raise ValueError('Identity mapping lacks evidence')
        key = json.dumps([kind, left, right], sort_keys=True)
        if key in seen:
            raise ValueError('Duplicate reviewed identity operation')
        seen.add(key)
    return data


def apply(conn, *, aliases, api, reference=DEFAULT_REFERENCE):
    data = load_reference(reference)
    counts = {'aliases': 0, 'source_links': 0, 'collection_equivalences': 0, 'missing_endpoints': 0}
    scopes = {tuple(row) for row in conn.execute('SELECT DISTINCT dataset_type,short_title FROM participants')}
    for row in data['records']:
        kind = row['kind']
        if (kind == 'within_dataset_alias') != aliases:
            continue
        left, right = row['left'], row['right']
        if (left['dataset_type'], left['short_title']) not in scopes:
            continue
        keys = [api.participant_key(p['dataset_type'], p['short_title'], p['identifier']) for p in (left, right)]
        for index, endpoint in enumerate((left, right)):
            if conn.execute('SELECT 1 FROM participants WHERE participant_key=?', (keys[index],)).fetchone():
                continue
            matches = conn.execute('''SELECT DISTINCT p.participant_key FROM participant_identifiers i
                JOIN participants p USING(participant_key) WHERE p.dataset_type=? AND p.short_title=?
                AND lower(i.raw_identifier)=lower(?)''',
                (endpoint['dataset_type'], endpoint['short_title'], endpoint['identifier'])).fetchall()
            if len(matches) == 1:
                keys[index] = matches[0][0]
        a, b = keys
        exists = [bool(conn.execute('SELECT 1 FROM participants WHERE participant_key=?', (key,)).fetchone()) for key in keys]
        # A canonical alias endpoint may legitimately be created from an existing
        # membership; a cross-dataset match may never invent either endpoint.
        if not exists[0] or (not aliases and not exists[1]):
            counts['missing_endpoints'] += 1
            conn.execute('INSERT OR REPLACE INTO participant_link_issues VALUES (?,?,?,?,?,?,?,?)',
                         (api.stable_id('participant_issue', 'reviewed', a, b), left['dataset_type'], left['short_title'], left['identifier'],
                          'reviewed_identity_endpoint_not_current', 'open', 'A reviewed finite mapping has an absent current endpoint; no match was invented.', api.json_dumps(row)))
            continue
        evidence = {**row, 'reference': reference.name, 'reference_sha256': api.file_sha256(reference),
                    'source_sets': {key: data['source_sets'][key] for key in [row['evidence'].get('source_set_sha256')] if key}}
        if aliases:
            if a == b:
                continue
            if not exists[1]:
                conn.execute('INSERT INTO participants SELECT ?,dataset_type,short_title,?,identity_scope,within_dataset_identity_status,identity_resolution_method,cross_dataset_identity_status FROM participants WHERE participant_key=?',
                             (b, right['identifier'], a))
            merge_alias(conn, a, b)
            conn.execute("UPDATE participants SET within_dataset_identity_status='resolved',identity_resolution_method=? WHERE participant_key=?", (row['method'], b))
            conn.execute("INSERT OR REPLACE INTO participant_identity_evidence VALUES (?,?,'within_dataset',?,'resolved','high',?,?)",
                         (api.stable_id('identity_evidence', 'reviewed', a, b), b, row['method'], 'Official finite within-dataset alias; all raw identifiers and assets retained.', api.json_dumps(evidence)))
            counts['aliases'] += 1
        elif kind == 'collection_equivalence':
            conn.execute('INSERT OR REPLACE INTO participant_equivalences VALUES (?,?,?,?,?,?,?)',
                         (api.stable_id('participant_equivalence', a, b), a, b, row['method'], 'high', reference.name, api.json_dumps(evidence)))
            counts['collection_equivalences'] += 1
        else:
            if kind == 'replace_source_links':
                conn.execute('DELETE FROM participant_source_links WHERE analysis_result_participant_key=?', (a,))
                conn.execute("DELETE FROM participant_identity_evidence WHERE participant_key=? AND resolution_scope='cross_dataset'", (a,))
            api.record_source_collection_link(conn, analysis_result_participant_key=a,
                source_collection_short_title=right['short_title'], source_participant_id=right['identifier'],
                source_collection_participant_key=b, link_status='linked_source_collection',
                resolution_method='official_reviewed_finite_participant_crosswalk', confidence='high',
                evidence_source=reference.name, evidence_pointer=f"reviewed:{api.stable_id('mapping', a, b)}",
                evidence_url='https://www.cancerimagingarchive.net/', evidence=evidence)
            conn.execute("UPDATE participant_link_issues SET status='resolved' WHERE dataset_type=? AND short_title=? AND lower(raw_identifier)=lower(?) AND status='open'",
                         (left['dataset_type'], left['short_title'], left['identifier']))
            counts['source_links'] += 1
    return counts
