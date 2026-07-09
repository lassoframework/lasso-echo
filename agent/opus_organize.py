"""
opus-organize (behind AGENT_OPUS_FACTORY_ENABLED, default OFF).

The Opus video factory scans COLLECTIONS, but a fresh account's clips live in
PROJECTS and are never added to a collection, so the scan reads nothing. This
command bridges that: it reads each project's finished clips and adds them to one
target collection the factory can then scan.

The documented Opus API has NO bulk project-listing endpoint, so projects are
enumerated from the pinned AGENT_OPUS_PROJECT_IDS (copy each id from its project
URL). Routes used (verified against help.opus.pro/api-reference):
  - GET  /api/exportable-clips?q=findByProjectId&projectId=   (a project's clips)
  - POST /api/collections            {collectionName}         (create collection)
  - POST /api/collection-contents    {collectionId, contentId} (add ONE clip)
  - GET  /api/exportable-clips?q=findByCollectionId&collectionId= (idempotency read)

Dry-run (default) writes NOTHING: it prints the plan of which clips would be added
to which collection. --write creates the target collection if absent and adds
every qualifying clip that is not already in it (idempotent). Nothing publishes;
this only organizes source clips. The key is env-only and never printed.
"""

from . import config, opus_ingest

DEFAULT_COLLECTION_NAME = "LASSO Clips"

# A clip is exportable/finished when it has an id and an export URL.
_EXPORT_URL_KEYS = ("uriForExport", "downloadUrl", "download_url",
                    "exportUrl", "export_url")


def _clip_id(clip):
    return str(clip.get("id", "") or "") if isinstance(clip, dict) else ""


def _export_url(clip):
    if not isinstance(clip, dict):
        return ""
    for k in _EXPORT_URL_KEYS:
        v = clip.get(k)
        if v:
            return str(v)
    return ""


def _finished(clips):
    """The exportable clips (id + export URL present), preserving order."""
    return [c for c in clips if _clip_id(c) and _export_url(c)]


def target_collection_name():
    """The collection to organize into: AGENT_OPUS_PODCAST_SHOW if set, else the
    default. Read at call time."""
    return config.opus_podcast_show().strip() or DEFAULT_COLLECTION_NAME


def _find_collection_id(api, name):
    """The id of an existing collection whose title matches name (case-insensitive),
    or "" if none exists yet."""
    want = name.strip().lower()
    for c in api.list_collections_detailed():
        if str(c.get("title", "")).strip().lower() == want:
            return c.get("id", "")
    return ""


def _existing_content_ids(api, collection_id):
    """The clip ids already in the collection (for idempotency)."""
    if not collection_id:
        return set()
    clips = api.list_exportable_clips("findByCollectionId", collection_id)
    return {_clip_id(c) for c in clips if _clip_id(c)}


def organize(api=None, write=False, target_name=None):
    """
    Build (and with write=True, apply) the plan that adds every project's finished
    clips to one target collection. Returns a plan dict, or None while the master
    flag is OFF. Dry-run has zero side effects.
    """
    if not config.opus_factory_enabled():
        print("opus-organize: OFF (set AGENT_OPUS_FACTORY_ENABLED=true). Nothing done.")
        return None
    api = api or opus_ingest._default_api()
    if api is None:
        print("opus-organize: OPUS_API_KEY is not set; nothing done.")
        return None

    project_ids = opus_ingest.validated_project_ids(config.opus_project_ids())
    name = (target_name or target_collection_name()).strip()
    plan = {"target_name": name, "collection_id": "", "created": False,
            "projects": [], "to_add": [], "already_in": [], "added": [],
            "final_count": None}

    if not project_ids:
        print("opus-organize: no project ids pinned. The Opus API has no bulk "
              "project-listing endpoint, so set AGENT_OPUS_PROJECT_IDS to the ids "
              "copied from each project URL (comma separated). Nothing done.")
        return plan

    # Enumerate each project's finished clips (the API returns NO score field; the
    # clips arrive in curation-rank order, so score is reported as n/a).
    all_clip_ids = []
    for pid in project_ids:
        try:
            clips = api.list_project_clips(pid)
        except opus_ingest.OpusScanError as exc:
            print(f"opus-organize: could not list clips for project {pid}: "
                  f"HTTP {exc.http_status}. {exc.body_snippet}")
            raise
        finished = _finished(clips)
        ids = [_clip_id(c) for c in finished]
        all_clip_ids.extend(ids)
        plan["projects"].append({"project_id": pid, "clips": ids})

    # Resolve the target collection (read-only) so dry-run can report accurately.
    existing_id = _find_collection_id(api, name)
    plan["collection_id"] = existing_id
    already = _existing_content_ids(api, existing_id)
    seen = set()
    to_add = []
    for cid in all_clip_ids:
        if cid in already:
            if cid not in plan["already_in"]:
                plan["already_in"].append(cid)
            continue
        if cid in seen:
            continue                       # duplicate id across projects
        seen.add(cid)
        to_add.append(cid)
    plan["to_add"] = to_add

    _print_plan(plan, write)

    if not write:
        print("opus-organize: DRY RUN, nothing was created or added.")
        return plan

    # --write: create the collection if needed, then add every clip not already in.
    collection_id = existing_id
    if not collection_id:
        collection_id = api.create_collection(name)
        plan["created"] = True
        plan["collection_id"] = collection_id
        print(f"opus-organize: created collection {name!r} -> id {collection_id}")
    if collection_id and to_add:
        plan["added"] = api.add_clips_to_collection(collection_id, to_add)
    # confirm the final count by re-reading the collection
    final = _existing_content_ids(api, collection_id)
    plan["final_count"] = len(final)
    print(f"opus-organize: collection {collection_id} now holds "
          f"{plan['final_count']} clip(s) ({len(plan['added'])} added this run).")
    return plan


def _print_plan(plan, write):
    mode = "WRITE" if write else "DRY RUN"
    print(f"opus-organize PLAN ({mode}) -> collection {plan['target_name']!r}"
          + (f" (existing id {plan['collection_id']})" if plan["collection_id"]
             else " (will be created)"))
    total = 0
    for proj in plan["projects"]:
        print(f"  project {proj['project_id']}: {len(proj['clips'])} finished clip(s)")
        for cid in proj["clips"]:
            print(f"    - {cid}  score n/a (API provides none; curation order)")
        total += len(proj["clips"])
    print(f"summary: {total} finished clip(s) across {len(plan['projects'])} "
          f"project(s); {len(plan['to_add'])} to add, "
          f"{len(plan['already_in'])} already in the collection.")


def organize_cli(argv):
    """python -m agent opus-organize [--write] [--name NAME]"""
    write, name, i = False, None, 0
    while i < len(argv):
        if argv[i] == "--write":
            write = True
        elif argv[i] == "--name" and i + 1 < len(argv):
            name = argv[i + 1]; i += 2; continue
        i += 1
    organize(write=write, target_name=name)
