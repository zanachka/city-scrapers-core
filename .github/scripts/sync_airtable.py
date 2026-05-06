"""
Sync spider slugs from changed spider files to Airtable.

For each spider file passed in, extract all (name, agency) pairs and
create/update Airtable records.

The extraction of spiders currently only works for the following
two patterns:
    1. Singular scraper — a class whose body has `name = "..."` and
         `agency = "..."` as direct class attributes. Only top-level class
         assignments count; anything inside a method is ignored.
    2. Spider factory — a module-level `spider_configs = [{...}, ...]`
         list of dicts, each with its own "name" and "agency" keys.
"""

import ast
import sys
from pathlib import Path

from decouple import UndefinedValueError, config
from pyairtable import Api
from pyairtable.formulas import match

"""
AirTable field names for the slug and agency name.
These should match the field names in the AirTable base.
"""
# Spiders table field names
SLUG_FIELD = "Slug"
AGENCY_FIELD = "Agency name"
PROGRAM_FIELD = "Program"

# Backlog table field names
BACKLOG_AGENCY_FIELD = "Agency name"
BACKLOG_PROGRAM_LOOKUP_FIELD = "Program"


def extract_spiders(source: str) -> list[dict]:
    tree = ast.parse(source)
    spiders: list[dict] = []

    """
    Helper function that finds `key = "..."` in a list of statements
    and return the string. Only looks at direct Assign nodes in the
    given body - doesn't recurse into nested functions or classes.
    """

    def get_str_assign(body, key):
        for stmt in body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == key
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                return stmt.value.value
        return None

    for node in ast.iter_child_nodes(tree):
        # Case 1: regular scraper
        if isinstance(node, ast.ClassDef):
            name = get_str_assign(node.body, "name")
            agency = get_str_assign(node.body, "agency")
            if name:
                spiders.append({"name": name, "agency": agency})

        # Case 2: spider factory
        elif (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "spider_configs"
                for t in node.targets
            )
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            for element in node.value.elts:
                if not isinstance(element, ast.Dict):
                    continue
                entry = {"name": None, "agency": None}
                for k, v in zip(element.keys, element.values):
                    if not (
                        isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                    ):
                        continue
                    if k.value in ("name", "agency") and isinstance(v.value, str):
                        entry[k.value] = v.value
                if entry["name"]:
                    spiders.append(entry)

    return spiders


def find_program_for_agency(agency: str, backlog_table) -> str | None:
    """Look up `agency` in the Backlog table and return the linked Program record ID.

    The Backlog table's Program field is a *lookup*, it returns
    the linked program's record ID. This function Returns the
    Programs record ID, or None if the agency isn't in Backlog or
    the looked-up Program name doesn't resolve to a Programs record.
    """
    backlog_records = backlog_table.all(
        formula=match({BACKLOG_AGENCY_FIELD: agency}),
        fields=[BACKLOG_PROGRAM_LOOKUP_FIELD],
        max_records=1,
    )
    if not backlog_records:
        return None

    # Lookup fields always return a list, even when the source link is single.
    program_lookup = (
        backlog_records[0]["fields"].get(BACKLOG_PROGRAM_LOOKUP_FIELD) or []
    )
    if not program_lookup:
        return None
    return program_lookup[0]


def sync_to_airtable(
    spiders: list[dict], table, table_records, program_record_id: str | None = None
) -> dict:
    """
    Sync spiders to Airtable, keyed on agency name.
    Agency name is considered the source of truth for
    matching records.

    Workflow for each spider:
      - If the agency is already in the table and the slug matches: skip.
      - If the agency is in the table but the slug differs: update the slug.
      - If the agency is not in the table: create a new record.

    If `program_record_id` is provided, every created or updated record also
    has its Program field set to link to that record. If None, the Program
    field is left untouched.

    Note:
    If the agency name for the same slug changes, a new record
    will be created and the table will end up with multiple
    records for the same slug but different agency names.
    There would have to be some manual cleanup to remove the
    old record with the outdated slug, but this is a safer
    approach than accidentally overwriting an existing record
    with a new slug that belongs to a different agency.

    Returns a summary: {'created': [...], 'updated': [...], 'skipped': [...]}.
    """
    existing: dict[str, tuple[str, str]] = {}
    for row in table_records:
        agency = row["fields"].get(AGENCY_FIELD)
        if agency:
            existing[agency] = (row["id"], row["fields"].get(SLUG_FIELD, ""))

    to_create = []
    to_update = []
    skipped = []

    for spider in spiders:
        slug = spider["name"]
        agency = spider.get("agency")
        if not agency:
            # Can't key on agency if it's missing — log and move on.
            print(f"[INFO] skipping spider '{slug}' — no agency name found")
            continue

        if agency in existing:
            record_id, current_slug = existing[agency]
            if current_slug == slug:
                skipped.append(agency)
            else:
                fields = {SLUG_FIELD: slug}
                if program_record_id:
                    fields[PROGRAM_FIELD] = [program_record_id]
                to_update.append({"id": record_id, "fields": fields})
        else:
            fields = {SLUG_FIELD: slug, AGENCY_FIELD: agency}
            if program_record_id:
                fields[PROGRAM_FIELD] = [program_record_id]
            to_create.append(fields)
            # Track in-memory so duplicates within this same run don't double-create.
            existing[agency] = ("", slug)

    created = []
    if to_create:
        created_records = table.batch_create(to_create)
        created = [row["fields"].get(AGENCY_FIELD) for row in created_records]

    updated = []
    if to_update:
        table.batch_update(to_update)
        updated = [row["fields"][SLUG_FIELD] for row in to_update]

    return {"created": created, "updated": updated, "skipped": skipped}


def main():
    try:
        pat = config("AIRTABLE_PAT")
        base_id = config("AIRTABLE_BASE_ID")
        slugs_table = config("SLUGS_TABLE_ID")
        backlog_table = config("BACKLOG_TABLE_ID")
    except UndefinedValueError as e:
        sys.exit(f"[ERROR] Missing env var: {e}")

    files = [
        Path(p) for p in sys.argv[1:] if Path(p).suffix == ".py" and Path(p).exists()
    ]
    if not files:
        print("[INFO] No spider files to process.")
        return

    api = Api(pat)
    slugs_table = api.table(base_id, slugs_table)
    backlog_table = api.table(base_id, backlog_table)

    slugs_table_records = slugs_table.all(fields=[SLUG_FIELD, AGENCY_FIELD])

    # Process each file separately so all spiders from one factory file
    # share the same Program (looked up via the file's first spider agency).
    overall = {"created": [], "updated": [], "skipped": []}
    for path in files:
        try:
            spiders = extract_spiders(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"  ! skipping {path} — syntax error: {e}")
            continue

        if not spiders:
            print(f"  {path}: no spiders found")
            continue

        first_agency = spiders[0].get("agency")
        program_id = None
        if first_agency:
            program_id = find_program_for_agency(first_agency, backlog_table)

        if program_id:
            print(f"[INFO] Found Program record {program_id} for {first_agency}")
        else:
            print(f"[INFO] No Program match for '{first_agency}'")

        result = sync_to_airtable(spiders, slugs_table, slugs_table_records, program_id)
        for key in overall:
            overall[key].extend(result[key])

    print(f"[INFO] Created {len(overall['created'])}: {overall['created']}")
    print(f"[INFO] Updated {len(overall['updated'])}: {overall['updated']}")
    print(
        f"[INFO] Skipped {len(overall['skipped'])} (already up to date): {overall['skipped']}"  # noqa
    )


if __name__ == "__main__":
    main()
