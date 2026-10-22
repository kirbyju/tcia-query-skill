"""Release-built curated participant index; raw identity records remain unchanged."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3

SOURCE_METHODS = {
    'manifest_series_patient_crosswalk',
    'idc_source_collection_and_exact_identifier',
    'official_acrin_ispy2_clinical_crosswalk',
    'official_dataset_relationship_and_exact_identifier',
    'official_tcia_brats2021_workbook',
    'official_reviewed_finite_participant_crosswalk',
}
EQUIVALENCE_METHODS = {
    'official_collection_overlap_declaration_and_exact_shared_namespace',
    'official_brats_peds_source_mapping_and_reviewed_dfci_finite_padding_alias',
    'official_acrin_ispy2_clinical_crosswalk',
}
BENCHMARKS = {'Pseudo-PHI-DICOM-Data', 'MIDI-B-Test-MIDI-B-Validation'}
CATEGORIES = ('human', 'animal', 'phantom', 'benchmark', 'unclassified')
REVIEW_REFERENCE = Path(__file__).resolve().parents[1] / 'references/reviewed-participant-issue-dispositions.json'
SCHEMA = """
CREATE TABLE participant_groups (
    participant_group_id TEXT PRIMARY KEY,
    subject_category TEXT NOT NULL CHECK(subject_category IN ('human','animal','phantom','benchmark','unclassified')),
    species TEXT NOT NULL,
    membership_count INTEGER NOT NULL CHECK(membership_count > 0)
);
CREATE TABLE participant_group_members (
    participant_key TEXT PRIMARY KEY REFERENCES participants(participant_key),
    participant_group_id TEXT NOT NULL REFERENCES participant_groups(participant_group_id)
);
CREATE INDEX idx_participant_group_members_group ON participant_group_members(participant_group_id);
CREATE VIEW agent_participant_group_members AS
SELECT m.*, p.dataset_type, p.short_title, p.display_participant_id,
       g.subject_category, g.species
FROM participant_group_members m JOIN participants p USING(participant_key)
JOIN participant_groups g USING(participant_group_id);
CREATE VIEW agent_participant_summary AS
SELECT subject_category, species, COUNT(*) AS participant_count
FROM participant_groups GROUP BY subject_category, species;
CREATE TABLE participant_group_link_review (
    link_kind TEXT NOT NULL,
    link_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(link_kind, link_id)
);
CREATE TABLE participant_issue_dispositions (
    issue_id TEXT PRIMARY KEY REFERENCES participant_link_issues(issue_id),
    source_status TEXT NOT NULL,
    disposition TEXT NOT NULL,
    evidence_reference TEXT NOT NULL
);
"""


def build_groups(conn: sqlite3.Connection, dataset_species: dict[tuple[str, str], str],
                 *, review_reference: Path = REVIEW_REFERENCE) -> dict[str, int]:
    """Consolidate only supported edges; reject an entire conflicting component."""
    people = {r['participant_key']: dict(r) for r in conn.execute('SELECT * FROM participants')}
    parent = {k: k for k in people}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    candidates, reviews = [], []
    links = [('source', dict(r)) for r in conn.execute('SELECT * FROM participant_source_links ORDER BY participant_source_link_id')]
    links += [('equivalence', dict(r)) for r in conn.execute('SELECT * FROM participant_equivalences ORDER BY participant_equivalence_id')]
    for kind, row in links:
        source = kind == 'source'
        edge_id = row['participant_source_link_id' if source else 'participant_equivalence_id']
        left = row['analysis_result_participant_key' if source else 'left_participant_key']
        right = row['source_collection_participant_key' if source else 'right_participant_key']
        reason = ''
        if source and row['link_status'] != 'linked_source_collection':
            reason = 'source_not_linked'
        elif left not in people or right not in people:
            reason = 'missing_endpoint'
        elif row['confidence'] != 'high' or row['resolution_method'] not in (SOURCE_METHODS if source else EQUIVALENCE_METHODS):
            reason = 'unreviewed_method_or_confidence'
        elif not row['evidence_source'] or (source and not row['evidence_pointer']):
            reason = 'missing_evidence'
        elif (people[right]['dataset_type'] != 'Collection'
              or people[left]['dataset_type'] != ('Analysis Result' if source else 'Collection')
              or (source and people[right]['short_title'] != row['source_collection_short_title'])
              or (not source and people[left]['short_title'] == people[right]['short_title'])):
            reason = 'endpoint_scope_mismatch'
        if not reason and not source and row['resolution_method'] == 'official_acrin_ispy2_clinical_crosswalk':
            evidence = json.loads(row['evidence_json'])
            raw = evidence.get('evidence', {})
            fields = raw.get('source_fields', {})
            if (people[left]['short_title'] != 'ISPY2' or people[right]['short_title'] != 'ACRIN-6698'
                    or row['evidence_source'] != 'clinical_only_identity_repairs_v1.json'
                    or evidence.get('left', {}).get('identifier') != people[left]['display_participant_id']
                    or evidence.get('right', {}).get('identifier') != people[right]['display_participant_id']
                    or fields.get('dicom_patient_id') != people[right]['display_participant_id']
                    or 'ISPY2-' + str(fields.get('i_spy_2_research_id', '')) != people[left]['display_participant_id']
                    or len(str(raw.get('source_row_sha256', ''))) != 64):
                reason = 'unsupported_acrin_ispy2_crosswalk_scope'
        if reason:
            reviews.append((kind, edge_id, 'not_linked' if reason == 'source_not_linked' else 'rejected', reason))
            continue
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
        candidates.append((kind, edge_id, left, right, row['resolution_method']))

    components = defaultdict(list)
    for key in sorted(people):
        components[find(key)].append(key)
    conflicts = set()
    for root, members in components.items():
        scopes = [(people[k]['dataset_type'].casefold(), people[k]['short_title'].casefold()) for k in members]
        if len(scopes) != len(set(scopes)):
            conflicts.add(root)
    reviewed_source_keys = set()
    for kind, edge_id, left, right, method in candidates:
        if find(left) in conflicts:
            reviews.append((kind, edge_id, 'rejected', 'conflicting_same_dataset_members'))
        else:
            reviews.append((kind, edge_id, 'accepted', ''))
            if kind == 'source' and method == 'official_reviewed_finite_participant_crosswalk':
                reviewed_source_keys.add(left)

    conn.execute('DELETE FROM participant_issue_dispositions')
    conn.execute('DELETE FROM participant_group_link_review')
    conn.execute('DELETE FROM participant_group_members')
    conn.execute('DELETE FROM participant_groups')
    group_rows, member_rows = [], []
    for root, component in sorted(components.items()):
        for members in ([[k] for k in component] if root in conflicts else [component]):
            group_id = hashlib.sha256(json.dumps(members).encode()).hexdigest()
            labels = {label.strip() for k in members for label in dataset_species.get(
                (people[k]['dataset_type'], people[k]['short_title']), '').split(';') if label.strip()}
            categories = {'human' if s == 'Human' else 'animal' if s in {'Canine', 'Mouse'} else
                          'phantom' if s in {'Physical Phantom', 'Computational Phantom'} else 'unclassified' for s in labels}
            if all(people[k]['short_title'] in BENCHMARKS for k in members):
                category, species = 'benchmark', 'Benchmark subjects'
            else:
                category = next(iter(categories)) if len(categories) == 1 else 'unclassified'
                species = next(iter(labels)) if len(labels) == 1 else 'Unknown / mixed species'
            group_rows.append((group_id, category, species, len(members)))
            member_rows.extend((k, group_id) for k in members)
    conn.executemany('INSERT INTO participant_groups VALUES (?,?,?,?)', group_rows)
    conn.executemany('INSERT INTO participant_group_members VALUES (?,?)', member_rows)
    conn.executemany('INSERT INTO participant_group_link_review VALUES (?,?,?,?)', reviews)

    policy = json.loads(review_reference.read_text())
    if policy.get('schema_version') != 1:
        raise ValueError('Unsupported participant issue disposition policy')
    identifier_keys = defaultdict(set)
    for r in conn.execute('SELECT p.dataset_type,p.short_title,i.raw_identifier,p.participant_key FROM participants p JOIN participant_identifiers i USING(participant_key)'):
        identifier_keys[(r[0], r[1], r[2].casefold())].add(r[3])
    for key, p in people.items():
        identifier_keys[(p['dataset_type'], p['short_title'], p['display_participant_id'].casefold())].add(key)
    source_codes = {'source_collection_participant_not_current', 'brats_source_collection_participant_not_current',
                    'idc_source_collection_participant_not_current', 'ambiguous_source_collection_match', 'unresolved_source_collection'}
    for row in conn.execute('SELECT * FROM participant_link_issues'):
        disposition, evidence = 'actionable', ''
        keys = identifier_keys[(row['dataset_type'], row['short_title'], row['raw_identifier'].casefold())]
        if row['status'].lower() in {'resolved', 'closed'}:
            disposition = 'source_resolved'
        elif row['issue_code'] in source_codes and len(keys) == 1 and keys <= reviewed_source_keys:
            disposition, evidence = 'resolved_by_reviewed_mapping', 'participant_source_links'
        else:
            for cohort in policy['cohorts']:
                if (row['dataset_type'] == cohort['dataset_type'] and row['short_title'] == cohort['short_title']
                    and row['issue_code'] == cohort['issue_code'] and len(keys) == 1
                    and row['raw_identifier'].casefold() in {i.casefold() for i in cohort['identifiers']}
                    and not conn.execute("SELECT 1 FROM participant_source_links WHERE analysis_result_participant_key=? AND link_status='linked_source_collection'", (next(iter(keys)),)).fetchone()):
                    disposition, evidence = 'explained_annotation_only', review_reference.name
        conn.execute('INSERT INTO participant_issue_dispositions VALUES (?,?,?,?)',
                     (row['issue_id'], row['status'], disposition, evidence))
    return {'participant_groups': len(group_rows), 'duplicate_memberships_removed': len(people) - len(group_rows),
            'rejected_group_links': sum(r[2] == 'rejected' for r in reviews)}


def validate_groups(conn: sqlite3.Connection) -> list[str]:
    """Check completeness, group cardinality, and dataset scope before release."""
    errors = []
    checks = {
        'participants missing curated membership': '''SELECT COUNT(*) FROM participants p
            LEFT JOIN participant_group_members m USING(participant_key) WHERE m.participant_key IS NULL''',
        'incorrect group membership counts': '''SELECT COUNT(*) FROM participant_groups g
            WHERE membership_count <> (SELECT COUNT(*) FROM participant_group_members m
                                       WHERE m.participant_group_id=g.participant_group_id)''',
        'conflicting dataset identities in curated groups': '''SELECT COUNT(*) FROM (
            SELECT participant_group_id,lower(dataset_type),lower(short_title),COUNT(*) n
            FROM agent_participant_group_members GROUP BY 1,2,3 HAVING n>1)''',
        'issues missing staff disposition': '''SELECT COUNT(*) FROM participant_link_issues i
            LEFT JOIN participant_issue_dispositions d USING(issue_id) WHERE d.issue_id IS NULL''',
    }
    for label, sql in checks.items():
        count = conn.execute(sql).fetchone()[0]
        if count:
            errors.append(f'{label}: {count}')
    return errors
