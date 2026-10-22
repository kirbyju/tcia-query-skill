"""Finite, source-bound repairs reviewed during the clinical-only inventory audit."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
IDENTITY_REFERENCE = REFERENCES / 'clinical_only_identity_repairs_v1.json'


def reviewed_rows(api, name):
    metadata = json.loads((REFERENCES / f'{name}.json').read_text())
    path = REFERENCES / f'{name}.csv'
    if metadata.get('schema_version') != 1 or api.file_sha256(path) != metadata.get('inventory_sha256'):
        raise RuntimeError(f'Reviewed inventory digest mismatch: {name}')
    with path.open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    return metadata, rows


def repair_histology_hsi_links(conn, *, api):
    """Retain the original assets and raw capture IDs; correct their patient grain."""
    title = 'HistologyHSI-BC-Recurrence'
    counts = {'assets': 0, 'participants': 0}
    if not conn.execute("SELECT 1 FROM public_non_dicom_assets WHERE short_title=? "
                        "AND asset_granularity='file' LIMIT 1", (title,)).fetchone():
        return counts
    meta, rows = reviewed_rows(api, 'histology_hsi_participant_links_v1')
    if (meta['short_title'] != title or len(rows) != meta['links']
            or len({r['asset_id'] for r in rows}) != len(rows)
            or len({r['subject_id'] for r in rows}) != meta['participants']):
        raise RuntimeError('HistologyHSI reviewed link coverage mismatch')
    # Validate the entire finite batch before changing ownership.
    for row in rows:
        asset = conn.execute('SELECT package_path,subject_id,file_format FROM '
                             'public_non_dicom_assets WHERE asset_id=? AND short_title=?',
                             (row['asset_id'], title)).fetchone()
        if (not asset or asset['package_path'] != row['package_path']
                or asset['file_format'] != row['file_format']
                or (asset['subject_id'] or '') not in {row['old_subject_id'], row['subject_id']}):
            raise RuntimeError(f"HistologyHSI reviewed asset identity mismatch: {row['asset_id']}")
    for row in rows:
        evidence = {'reference': 'histology_hsi_participant_links_v1.csv',
                    'inventory_sha256': meta['inventory_sha256'], 'source_url': meta['source_url'],
                    'package_path': row['package_path'], 'original_subject_id': row['old_subject_id']}
        conn.execute("UPDATE public_non_dicom_assets SET subject_id=?,subject_id_namespace=?,"
                     "participant_link_status='reviewed_source_inventory',"
                     "provenance_json=json_set(COALESCE(provenance_json,'{}'),"
                     "'$.clinical_only_ownership_review',json(?)) WHERE asset_id=?",
                     (row['subject_id'], f'tcia_dataset:{title}', api.json_dumps(evidence), row['asset_id']))
        conn.execute('DELETE FROM public_non_dicom_asset_participants WHERE asset_id=?', (row['asset_id'],))
        api.insert_asset_participant(
            conn, asset_id=row['asset_id'], short_title=title, subject_id=row['subject_id'],
            namespace=f'tcia_dataset:{title}', raw_subject_id=row['old_subject_id'] or row['subject_id'],
            participant_role='depicted_subject', link_status='reviewed_source_inventory', evidence=evidence)
    # The old PathDB projection collapsed many captures under the nonunique
    # source record ID "raw". It cannot establish ownership of a real file.
    # Keep the asset/locations and all raw links as QC evidence; the exact
    # Aspera assets above establish patient imaging availability independently.
    unresolved = meta.get('manual_qc_pathdb_asset')
    if unresolved:
        asset = conn.execute('SELECT * FROM public_non_dicom_assets WHERE asset_id=?',
                             (unresolved['asset_id'],)).fetchone()
        if asset:
            if (asset['short_title'] != title or asset['source_system'] != 'tcia_pathdb'
                    or asset['source_record_id'] != 'raw' or asset['file_name'] != 'raw'
                    or asset['package_path']):
                raise RuntimeError('HistologyHSI unresolved PathDB record changed; review required')
            raw_links = [dict(r) for r in conn.execute(
                'SELECT subject_id,raw_subject_id FROM public_non_dicom_asset_participants WHERE asset_id=?',
                (unresolved['asset_id'],))]
            if raw_links:
                if len(raw_links) != unresolved['raw_capture_links']:
                    raise RuntimeError('HistologyHSI unresolved PathDB capture count changed')
                conn.execute('INSERT OR REPLACE INTO public_non_dicom_review_issues VALUES '
                             '(?,?,?,\'high\',\'manual_review\',1,?,?)',
                             (api.stable_id('issue','hsi_pathdb_raw_collision'),title,
                              'nonunique_pathdb_record_id',unresolved['finding'],
                              api.json_dumps({'asset_id':unresolved['asset_id'],'original_links':raw_links})))
                conn.execute('DELETE FROM public_non_dicom_asset_participants WHERE asset_id=?',
                             (unresolved['asset_id'],))
            conn.execute("UPDATE public_non_dicom_assets SET subject_id='',subject_id_namespace='',"
                         "participant_link_status='unavailable' WHERE asset_id=?",(unresolved['asset_id'],))
    return {'assets': len(rows), 'participants': len({r['subject_id'] for r in rows})}


def ingest_ucsd_bmets_studies(conn, snapshot_db, *, api):
    """Represent verified package study directories without asserting file counts."""
    title = 'UCSD-BMETS-Longitudinal'
    counts = {'studies': 0, 'participants': 0}
    with api.closing(api.connect(snapshot_db)) as source:
        download = source.execute("SELECT * FROM agent_current_downloads WHERE short_title=? "
                                  "AND download_id='55864' AND hidden=0 AND controlled_access=0",
                                  (title,)).fetchone()
    if download is None:
        return counts
    meta, rows = reviewed_rows(api, 'ucsd_bmets_study_inventory_v1')
    if (meta['short_title'] != title or meta['source_url'] != download['download_url']
            or len(rows) != meta['studies'] or len({r['study_id'] for r in rows}) != len(rows)
            or len({r['participant_id'] for r in rows}) != meta['participants']):
        raise RuntimeError('UCSD-BMETS reviewed study scope differs from current source')
    for row in rows:
        match = re.fullmatch(r'(BMETS\d{4})_\d{2}', row['study_id'])
        if (not match or match[1] != row['participant_id']
                or row['package_path'] != f"{title}/{row['study_id']}"):
            raise RuntimeError('UCSD-BMETS study directory identity mismatch')
    for row in rows:
        asset_id = api.stable_id('asset', 'ucsd_bmets_study', meta['download_id'], row['study_id'])
        evidence = {'reference': 'ucsd_bmets_study_inventory_v1.csv',
                    'inventory_sha256': meta['inventory_sha256'], 'listing_sha256': meta['listing_sha256'],
                    'metadata_sha256': meta['metadata_sha256'], 'metadata_url': meta['metadata_url'],
                    'study_id': row['study_id'], 'coverage': 'verified_study_directory; file inventory unavailable'}
        api.insert_asset(conn, {
            'asset_id': asset_id, 'dataset_type': 'Collection', 'short_title': title,
            'download_row_id': download['download_row_id'], 'download_id': meta['download_id'],
            'subject_id': row['participant_id'], 'subject_id_namespace': f'tcia_dataset:{title}',
            'participant_link_status': 'reviewed_source_inventory', 'asset_granularity': 'study',
            'asset_name': row['study_id'], 'file_name': '', 'package_path': row['package_path'],
            'file_format': 'NIFTI', 'container_format': '', 'media_kind': 'image_volume',
            'spatial_dimensionality': '3d', 'temporal_dimensionality': 'unknown',
            'imaging_domain': 'radiology', 'modality': 'MR', 'object_role': 'source_image_or_annotation',
            'represented_file_count': None, 'size_bytes': None, 'checksum': '', 'checksum_algorithm': '',
            'representation_provenance_class': 'unknown', 'source_system': 'tcia_aspera',
            'source_record_id': f"ucsd-bmets-study:{row['study_id']}", 'source_url': meta['source_url'],
            'raw_values_json': '{}', 'provenance_json': api.json_dumps(evidence), 'quality_flag_json': '{}',
            'geometry_status': 'not_checked',
        })
        api.insert_location(conn, api.location_values(asset_id, meta['source_url'],
                                                     representation_class='unknown', provenance=evidence))
        api.insert_asset_participant(conn, asset_id=asset_id, short_title=title,
            subject_id=row['participant_id'], namespace=f'tcia_dataset:{title}',
            raw_subject_id=row['participant_id'], participant_role='depicted_subject',
            link_status='reviewed_source_inventory', evidence=evidence)
    return {'studies': len(rows), 'participants': len({r['participant_id'] for r in rows})}


def repair_ldct_case_ownership(conn, *, api):
    """Repair both reused and freshly ingested IDC rows; preserve source JSON.

    dicom_patient_id in these two IDC tables incorrectly contains a scanner
    manufacturer. The source case columns, not a guessed scanner crosswalk,
    identify the participant independently for every row.
    """
    corrected = 0
    subjects = set()
    for table, field, pattern in [
        ('ldct_and_projection_data_abdomen_v9', 'abdomen_case', r'L\d{3}'),
        ('ldct_and_projection_data_chest_v9', 'chest_case', r'C\d{3}'),
    ]:
        rows = conn.execute('SELECT * FROM clinical_rows WHERE short_title=? AND table_name=?',
                            ('LDCT-and-Projection-data', table)).fetchall()
        for row in rows:
            raw = json.loads(row['row_json'])
            case = str(raw.get(field, '')).strip()
            if not re.fullmatch(pattern, case):
                raise RuntimeError(f'LDCT source row has no valid {field}')
            if row['subject_id'] == case:
                continue
            if row['subject_id'] not in {'GE', 'Siemens'}:
                raise RuntimeError('Unexpected LDCT participant identity; manual review required')
            key = f"ldctandprojectiondata:{case.casefold()}"
            row_id = api.stable_id(row['source_id'], table, row['row_number'], key, row['row_json'])
            has_images = conn.execute('SELECT 1 FROM clinical_imaging_subjects WHERE '
                                     'short_title=? AND lower(subject_id)=lower(?)',
                                     ('LDCT-and-Projection-data', case)).fetchone() is not None
            replacement = dict(row)
            replacement.update(source_row_id=row_id, subject_id=case, subject_key=key,
                               has_imaging=int(has_images))
            columns = list(replacement)
            conn.execute(f"INSERT OR IGNORE INTO clinical_rows ({','.join(columns)}) "
                         f"VALUES ({','.join('?' for _ in columns)})", list(replacement.values()))
            # Bind source_id too so the existing source index limits this
            # lookup to the small table instead of all archive clinical facts.
            facts = conn.execute('SELECT * FROM clinical_facts WHERE source_id=? AND source_row_id=?',
                                 (row['source_id'], row['source_row_id'])).fetchall()
            for fact in facts:
                values = dict(fact)
                evidence = json.loads(values.get('provenance_json') or '{}')
                evidence.update(subject_mapping_method='official_ldct_source_case_column',
                                original_subject_id=row['subject_id'], subject_case_column=field)
                values.update(fact_id=api.stable_id(row_id, fact['concept'], fact['value_normalized'],
                                                  fact['original_column']),
                              source_row_id=row_id, subject_id=case, subject_key=key,
                              provenance_json=api.json_dumps(evidence))
                names = list(values)
                conn.execute(f"INSERT OR IGNORE INTO clinical_facts ({','.join(names)}) "
                             f"VALUES ({','.join('?' for _ in names)})", list(values.values()))
            conn.execute('DELETE FROM clinical_facts WHERE source_id=? AND source_row_id=?',
                         (row['source_id'], row['source_row_id']))
            conn.execute('DELETE FROM clinical_rows WHERE source_row_id=?', (row['source_row_id'],))
            corrected += 1
            subjects.add(case)
        if rows:
            counts = conn.execute('SELECT COUNT(DISTINCT subject_id),'
                                  'COUNT(DISTINCT CASE WHEN has_imaging=1 THEN subject_id END) '
                                  'FROM clinical_rows WHERE short_title=? AND table_name=?',
                                  ('LDCT-and-Projection-data', table)).fetchone()
            conn.execute('UPDATE clinical_idc_tables SET subject_count=?,subjects_with_imaging=? '
                         'WHERE short_title=? AND table_name=?',
                         (*counts, 'LDCT-and-Projection-data', table))
    return {'rows_rekeyed': corrected, 'corrected_participants': len(subjects)}
