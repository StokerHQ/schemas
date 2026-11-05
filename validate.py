"""Validates a Stoker metadata (recipes) repository, or a plugin catalogue, against the v1 schemas.

    python -I validate.py <repository root> [--plugins <plugin catalogue root>]

The root may hold `games/`, `runtimes/` and `plugins/`. Games and runtimes are YAML; a plugin catalogue may be
YAML or JSON. The catalogue is read from `--plugins` when given, else from the root's own `plugins/`. Without
one, plugin references go unchecked and the run says so.

Two passes. The first reads every document — YAML restricted to what JSON can say, so `no` stays a word and
`2013-01-14` stays a string — and checks it against its schema. The second checks what a schema cannot see,
because it spans documents: a folder is named after its game's slug, an id means one thing in the whole
repository, a store id belongs to one item, an install reports only ids the item has, an edition includes DLC
its game has, a release range names releases that exist, a copy can only ever match one build, fixes refer to
things that exist, and every runtime and plugin a game names is in its catalogue, configured as its contract
says.

Exits 0 when the repository is valid, 1 when anything is wrong, listing every problem rather than stopping at
the first - a contributor fixing one error per run gives up. Warnings (a store or an id kind this validator
does not know yet) are listed and do not fail the run: readers ignore what they do not know.
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS = [Path(__file__).resolve().parent / family / "v1" for family in ("metadata", "plugins")]

# The documents a game folder holds, by the schema each follows. Only game.yaml is required.
DOCUMENTS = {
    "game.yaml": "game.schema.json",
    "achievements.yaml": "achievements.schema.json",
    "stats.yaml": "stats.schema.json",
    "leaderboards.yaml": "leaderboards.schema.json",
}
RELEASES = "builds"

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RELEASE_FILE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Every store's id kinds this validator knows: (reported or identity, the shape of a value, unique to one item).
# A store or a kind not listed here is allowed and warned about - a reader ignores what it does not know.
DIGITS = r"^[1-9][0-9]*$"
STORE_KINDS = {
    "steam": {
        "appId": ("reported", DIGITS, True),
        "packageId": ("identity", DIGITS, True),
        "depotId": ("identity", DIGITS, True),
    },
    "gog": {
        "productId": ("reported", DIGITS, True),
        "galaxyClientId": ("identity", DIGITS, True),
    },
    "epic": {
        "catalogItemId": ("reported", r"^[A-Za-z0-9]+$", True),
        "entitlementName": ("reported", r"^\S(?:.*\S)?$", True),
        "appName": ("identity", r"^[A-Za-z0-9_.-]+$", True),
        # Every item of one game is in the same namespace, so a namespace belongs to many items.
        "namespace": ("identity", r"^[A-Za-z0-9]+$", False),
        "offerId": ("identity", r"^[A-Za-z0-9]+$", True),
    },
    "ubisoft": {
        "productId": ("reported", r"^(?:0|[1-9][0-9]*)$", True),
    },
    "eshop": {
        "titleId": ("reported", r"^[0-9a-f]{16}$", True),
    },
}

# A save path starts at one of these, and may hold others: the folders a game keeps saves in (Steam Auto-Cloud's roots
# among them), then the account ids a store writes into save paths.
PLACEHOLDERS = {
    "base", "root", "game", "home", "storeGameId", "storeUserId", "osUserName", "winAppData", "winLocalAppData",
    "winLocalAppDataLow", "winDocuments", "winPublic", "winProgramData", "winDir", "xdgData", "xdgConfig",
    "winSavedGames", "macAppSupport", "macDocuments", "ubisoftConnect",
    "steamId64", "steamAccountId", "ubisoftAccountId",
}


# ── Reading ────────────────────────────────────────────────────────────────────────────────────────────────


class StrictLoader(yaml.SafeLoader):
    """YAML that means what JSON would. YAML 1.1 reads `no` and `off` as false, `2013-01-14` as a date and `010`
    as eight; a recipe's values are strings unless they are plainly a JSON number, `true`, `false` or `null`.
    A key appearing twice in one mapping, or a key that is not a string, is refused."""


StrictLoader.yaml_implicit_resolvers = {}
for tag, pattern, first in (
    ("tag:yaml.org,2002:null", r"^(?:null|~|)$", ["n", "~", ""]),
    ("tag:yaml.org,2002:bool", r"^(?:true|false)$", ["t", "f"]),
    ("tag:yaml.org,2002:int", r"^-?(?:0|[1-9][0-9]*)$", list("-0123456789")),
    ("tag:yaml.org,2002:float", r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$", list("-0123456789")),
):
    StrictLoader.add_implicit_resolver(tag, re.compile(pattern), first)


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(None, None, f"key {key!r} is not a string - quote it", key_node.start_mark)
        if key in result:
            raise yaml.constructor.ConstructorError(None, None, f"key {key!r} appears more than once in one mapping", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def no_duplicate_keys(pairs):
    # json.loads keeps the last of two equal keys without a word, so a second "en" would silently
    # replace the first translation.
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(f"key {key!r} appears more than once in one object")
    return dict(pairs)


def read_document(path, relative, problems):
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            return yaml.load(text, Loader=StrictLoader)
        return json.loads(text, object_pairs_hook=no_duplicate_keys)
    except (ValueError, yaml.YAMLError) as error:
        problems.append(f"{relative}: not a valid document: {error}")
        return None


def require_uri_checks():
    # Without the format-nongpl extra, jsonschema has no checker for "uri" and passes every value
    # without a word - a broken link would validate. Refuse to run rather than check less than it says.
    missing = [name for name in ("uri", "date") if name not in FormatChecker.checkers]
    if missing:
        print("validate.py needs jsonschema[format-nongpl] to check " + ", ".join(missing) + ' formats: pip install "jsonschema[format-nongpl]"')
        sys.exit(2)


def load_schemas():
    resources = {}
    for path in (path for folder in SCHEMAS for path in folder.glob("*.schema.json")):
        resources[path.name] = json.loads(path.read_text(encoding="utf-8"))
    registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in resources.values())
    validators = {
        name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for name, schema in resources.items()
    }
    return resources, validators


def step_classes(common):
    """Each step kind's class, from the `x-stokerhq-class` on its definition: what a reader may apply on its own
    (`unattended`) and what needs the player's consent (`optIn`). A kind without one is a schema bug."""
    definitions = common["$defs"]
    classes = {}
    for rule in definitions["step"]["allOf"]:
        kinds = rule["if"]["properties"]["kind"]
        names = [kinds["const"]] if "const" in kinds else kinds["enum"]
        target = definitions[rule["then"]["$ref"].rsplit("/", 1)[-1]]
        for name in names:
            classes[name] = target.get("x-stokerhq-class")
    for name in definitions["step"]["properties"]["kind"]["enum"]:
        classes.setdefault(name, None)
    return classes


def schema_problems(validator, relative, document):
    for error in validator.iter_errors(document):
        location = "/".join(str(part) for part in error.absolute_path) or "(document)"
        # A failed `not` quotes the whole instance, which for a release is the whole file.
        message = f"must not match {json.dumps(error.validator_value)}" if error.validator == "not" else error.message
        # The registry and environment rules are generated, case-folded patterns nobody can read.
        if error.validator in ("pattern", "not") and len(json.dumps(error.validator_value)) > 160:
            message = f"{json.dumps(error.instance)} is not allowed here; the schema's description of this field says why"
        yield f"{relative}: {location}: {message}"


def partial_date_problem(value):
    # The schema checks the shape; only a calendar knows 2017-02-30 is not a day.
    if isinstance(value, str) and len(value) == 10:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return f"{value} is not a day"
    return None


# ── The repository ──────────────────────────────────────────────────────────────────────────────────────────


def main(root, plugins_root=None):
    require_uri_checks()
    schemas, validators = load_schemas()
    problems, warnings = [], []

    classes = step_classes(schemas["common.schema.json"])
    for kind, step_class in sorted(classes.items()):
        if step_class not in ("unattended", "optIn"):
            problems.append(f"common.schema.json: step kind {kind} has no x-stokerhq-class")

    if not any((root / name).is_dir() for name in ("games", "runtimes", "plugins")):
        print(f"{root}: no games/, runtimes/ or plugins/ folder")
        return 1

    ids = {}  # every id in the repository, to where it was first seen
    store_ids = {}  # (store, kind, id) of a unique kind, to the item that has it

    def claim(identifier, where):
        if not isinstance(identifier, str):
            return
        if identifier in ids:
            problems.append(f"{where}: id {identifier} is already {ids[identifier]}")
        else:
            ids[identifier] = where

    catalogue_root = plugins_root or (root / "plugins" if (root / "plugins").is_dir() else None)
    catalogue = load_catalogue(validators, catalogue_root, problems) if catalogue_root else None
    runtimes = load_runtimes(validators, root, problems)
    games = 0
    references = []  # (where, plugin assignments, builds) checked against the catalogues at the end

    for folder in sorted((root / "games").iterdir()) if (root / "games").is_dir() else []:
        here = f"games/{folder.name}"
        if not folder.is_dir():
            problems.append(f"{here}: not a game folder")
            continue
        if not SLUG.match(folder.name):
            problems.append(f"{here}: a game folder is named by a slug - lowercase, digits, single hyphens")

        # A misspelt file name ("achievement.yaml") would otherwise be skipped without a word.
        for entry in sorted(folder.iterdir()):
            known = entry.name in DOCUMENTS if entry.is_file() else entry.name == RELEASES
            if not known:
                problems.append(f"{here}/{entry.name}: not a file or folder a game has")
        release_paths = []
        if (folder / RELEASES).is_dir():
            for child in sorted((folder / RELEASES).iterdir()):
                if not (child.is_file() and child.suffix == ".yaml" and RELEASE_FILE.match(child.stem)):
                    problems.append(f"{here}/{RELEASES}/{child.name}: not a release document (<name>.yaml, lowercase)")
                else:
                    release_paths.append(child)

        documents = {}
        for name, schema in DOCUMENTS.items():
            path = folder / name
            if path.is_file():
                document = read_document(path, f"{here}/{name}", problems)
                if document is not None:
                    problems.extend(schema_problems(validators[schema], f"{here}/{name}", document))
                    documents[name] = document
        releases = {}
        for path in release_paths:
            relative = f"{here}/{RELEASES}/{path.name}"
            document = read_document(path, relative, problems)
            if document is not None:
                problems.extend(schema_problems(validators["release.schema.json"], relative, document))
                releases[relative] = document

        game = documents.get("game.yaml")
        if not isinstance(game, dict):
            problems.append(f"{here}: no game.yaml")
            continue
        games += 1
        where_game = f"{here}/game.yaml"
        if game.get("slug") != folder.name:
            problems.append(f"{where_game}: slug {game.get('slug')} is not the folder's name")
        claim(game.get("id"), f"game {folder.name}")

        dlc = {entry.get("id"): entry for entry in game.get("dlc", []) if isinstance(entry, dict)}
        editions = game.get("editions", [])
        for index, entry in enumerate(game.get("dlc", [])):
            claim(entry.get("id"), f"{where_game}: dlc[{index}]")
            for required in entry.get("requires", []):
                if required == entry.get("id") or required not in dlc:
                    problems.append(f"{where_game}: dlc[{index}] requires {required}, which is not another DLC of this game")
        problems.extend(check_requirement_cycles(where_game, dlc))
        # A bundle DLC includes other DLC of the game; owning it is owning them, so it cannot include itself or go round.
        for index, entry in enumerate(game.get("dlc", [])):
            for included in entry.get("includes", []):
                if included == entry.get("id"):
                    problems.append(f"{where_game}: dlc[{index}] includes itself")
                elif included not in dlc:
                    problems.append(f"{where_game}: dlc[{index}] includes {included}, which is not another DLC of this game")
        problems.extend(check_requirement_cycles(where_game, dlc, "includes", "DLC bundles"))
        for index, edition in enumerate(editions):
            claim(edition.get("id"), f"{where_game}: editions[{index}]")
            for included in edition.get("includes", []):
                if included not in dlc:
                    problems.append(f"{where_game}: editions[{index}] includes {included}, which is not a DLC of this game")

        # Every item's store ids: well formed, reported only from what the item has, and one item's alone.
        items = [(f"{where_game}", game)]
        items += [(f"{where_game}: dlc[{index}]", entry) for index, entry in enumerate(game.get("dlc", []))]
        items += [(f"{where_game}: editions[{index}]", entry) for index, entry in enumerate(editions)]
        for where, item in items:
            if isinstance(item, dict):
                check_stores(where, item.get("stores"), store_ids, problems, warnings)

        for value in [game.get("storePage", {}).get("releaseDate")] + [entry.get("storePage", {}).get("releaseDate") for entry in dlc.values()]:
            if (problem := partial_date_problem(value)) is not None:
                problems.append(f"{where_game}: {problem}")

        for index, save in enumerate((game.get("saves") or {}).get("files", [])):
            for name in re.findall(r"<([^<>]*)>", save.get("path", "")):
                if name not in PLACEHOLDERS:
                    problems.append(f"{where_game}: saves.files[{index}]: <{name}> is not a placeholder (see the README)")

        tags = [tag.get("slug") for tag in game.get("tags", []) if isinstance(tag, dict)]
        for tag in sorted({tag for tag in tags if tags.count(tag) > 1}):
            problems.append(f"{where_game}: tag {tag} appears more than once")

        for name in ("achievements.yaml", "stats.yaml", "leaderboards.yaml"):
            if name in documents and documents[name].get("gameId") != game.get("id"):
                problems.append(f"{here}/{name}: gameId is not the game's id")
        # Every name a game grants or looks up by has to mean one thing: an apiName or alias that two
        # achievements share would record an unlock against whichever was found first.
        for file, key, field in (("achievements.yaml", "achievements", "apiName"), ("stats.yaml", "stats", "name"), ("leaderboards.yaml", "leaderboards", "name")):
            entries = (documents.get(file) or {}).get(key, [])
            names = [entry[field] for entry in entries if isinstance(entry, dict) and field in entry]
            if key == "achievements":
                names += [alias for entry in entries if isinstance(entry, dict) for alias in entry.get("aliases", [])]
            for name in sorted({name for name in names if names.count(name) > 1}):
                problems.append(f"{here}/{file}: {name} names more than one {key[:-1]}")

        for relative, release in releases.items():
            if release.get("gameId") != game.get("id"):
                problems.append(f"{relative}: gameId is not the game's id")
            claim(release.get("id"), relative)
            if (problem := partial_date_problem(release.get("releasedAt"))) is not None:
                problems.append(f"{relative}: {problem}")

        orders = {release.get("id"): release.get("order") for release in releases.values()}
        game_launches = {launch.get("key") for release in releases.values() for build in release.get("builds", []) for launch in build.get("launches", [])}
        game_fixes = {fix.get("id") for fix in game.get("fixes", [])}
        for index, fix in enumerate(game.get("fixes", [])):
            claim(fix.get("id"), f"{where_game}: fixes[{index}]")
            problems.extend(check_steps(f"{where_game}: fixes[{index}]", fix, None, game_launches))

        # Every condition in the game names releases of this game, and a range runs forwards.
        for where, condition in conditions(where_game, game, releases):
            problems.extend(check_release_range(where, condition.get("releases"), orders))

        problems.extend(check_releases(releases, dlc, game_fixes, claim))
        problems.extend(check_hooks(where_game, game, releases, claim))
        references.append((where_game, game.get("plugins", []), []))
        for relative, release in releases.items():
            builds = release.get("builds", [])
            assignments = list(release.get("plugins", [])) + [a for build in builds for a in build.get("plugins", [])]
            references.append((relative, assignments, builds))

    for where, assignments, builds in references:
        problems.extend(check_references(where, assignments, builds, runtimes, catalogue))

    for problem in problems:
        print(problem)
    for warning in warnings:
        print(f"warning: {warning}")
    if catalogue is None and games:
        print("no plugin catalogue (plugins/ or --plugins): plugin references were not checked")
    plugins = len(catalogue) if catalogue else 0
    print(f"{games} games, {len(runtimes)} runtimes, {plugins} plugins, {len(problems)} problems, {len(warnings)} warnings")
    return 1 if problems else 0


def check_stores(where, stores, store_ids, problems, warnings):
    if not isinstance(stores, dict):
        return
    for store, entry in stores.items():
        if not isinstance(entry, dict):
            continue
        known = STORE_KINDS.get(store)
        if known is None:
            warnings.append(f"{where}: store {store} is not one this validator knows; its ids are kept but not checked")
        seen = set()
        for index, store_id in enumerate(entry.get("ids", [])):
            if not isinstance(store_id, dict):
                continue
            kind, value = store_id.get("kind"), store_id.get("id")
            at = f"{where}: stores.{store}.ids[{index}]"
            if not isinstance(value, str):
                problems.append(f"{at}: the id is {value!r}, not a string - store ids are always quoted")
                continue
            if (kind, value) in seen:
                problems.append(f"{at}: {kind} {value} is listed twice")
            seen.add((kind, value))
            if known is None:
                continue
            if kind not in known:
                warnings.append(f"{at}: {store} id kind {kind} is not one this validator knows")
                continue
            _, shape, unique = known[kind]
            if not re.match(shape, value):
                problems.append(f"{at}: {value!r} is not the shape of a {store} {kind}")
            if unique:
                key = (store, kind, value)
                if key in store_ids:
                    problems.append(f"{at}: {store} {kind} {value} is already {store_ids[key]}'s")
                else:
                    store_ids[key] = where
        installed = entry.get("installed", "all")
        if isinstance(installed, list):
            for name in installed:
                kind, _, value = str(name).partition(":")
                if (kind, value) not in seen:
                    problems.append(f"{where}: stores.{store}.installed names {name}, which is not one of its ids")
                elif known is not None and kind in known and known[kind][0] != "reported":
                    problems.append(f"{where}: stores.{store}.installed names {name}, but a {kind} is never reported")


def conditions(where_game, game, releases):
    """Every `when` in a game's documents: its fixes', plugins' and hooks', its releases' and their builds'."""
    def of(where, holder):
        for key in ("fixes",):
            for index, fix in enumerate(holder.get(key, [])):
                for field in ("when", "appliedWhen"):
                    if isinstance(fix.get(field), dict):
                        yield f"{where}: {key}[{index}].{field}", fix[field]
        for key in ("plugins", "hooks"):
            for index, entry in enumerate(holder.get(key, [])):
                if isinstance(entry, dict) and isinstance(entry.get("when"), dict):
                    yield f"{where}: {key}[{index}].when", entry["when"]

    yield from of(where_game, game)
    for relative, release in releases.items():
        yield from of(relative, release)
        for index, build in enumerate(release.get("builds", [])):
            yield from of(f"{relative}: builds[{index}]", build)


def check_release_range(where, condition, orders):
    if not isinstance(condition, dict):
        return
    for field in ("from", "before"):
        if field in condition and condition[field] not in orders:
            yield f"{where}: releases.{field} {condition[field]} is not a release of this game"
    for release in condition.get("only", []):
        if release not in orders:
            yield f"{where}: releases.only names {release}, which is not a release of this game"
    start, end = orders.get(condition.get("from")), orders.get(condition.get("before"))
    if isinstance(start, int) and isinstance(end, int) and start >= end:
        yield f"{where}: releases.from is not older than releases.before, so the range is empty"


def check_hooks(where_game, game, releases, claim):
    """Hook ids are ids like any other, and `disableHooks` names hooks of a level above."""
    game_hooks = set()
    for index, hook in enumerate(game.get("hooks", [])):
        claim(hook.get("id"), f"{where_game}: hooks[{index}]")
        game_hooks.add(hook.get("id"))
    for relative, release in releases.items():
        release_hooks = set()
        for index, hook in enumerate(release.get("hooks", [])):
            claim(hook.get("id"), f"{relative}: hooks[{index}]")
            release_hooks.add(hook.get("id"))
        for disabled in release.get("disableHooks", []):
            if disabled not in game_hooks:
                yield f"{relative}: disables hook {disabled}, which is not one of the game's"
        for build_index, build in enumerate(release.get("builds", [])):
            for index, hook in enumerate(build.get("hooks", [])):
                claim(hook.get("id"), f"{relative}: builds[{build_index}].hooks[{index}]")
            for disabled in build.get("disableHooks", []):
                if disabled not in game_hooks | release_hooks:
                    yield f"{relative}: builds[{build_index}] disables hook {disabled}, which is not one of its release's or its game's"


def check_requirement_cycles(where, dlc, field="requires", what="DLC requirements"):
    # Requirements and bundles are walked to find everything a DLC needs or stands for; a cycle would walk forever.
    state = {}

    def visit(identifier, trail):
        if state.get(identifier) == "done":
            return
        if state.get(identifier) == "walking":
            yield f"{where}: {what} go round in a circle: {' -> '.join(trail + [identifier])}"
            return
        state[identifier] = "walking"
        for required in dlc.get(identifier, {}).get(field, []):
            if required in dlc and required != identifier:
                yield from visit(required, trail + [identifier])
        state[identifier] = "done"

    for identifier in sorted(i for i in dlc if isinstance(i, str)):
        yield from visit(identifier, [])


def check_releases(releases, dlc, game_fixes, claim):
    # Within a game a release is found by its name and "newest" by its order, so neither may repeat.
    for field in ("name", "order"):
        seen = {}
        for relative, release in releases.items():
            value = release.get(field)
            if value in seen:
                yield f"{relative}: {field} {value} is also {seen[value]}'s"
            seen.setdefault(value, relative)

    builds = {}
    release_fixes = {}
    for relative, release in releases.items():
        release_fixes[relative] = {fix.get("id") for fix in release.get("fixes", [])}
        release_launches = {launch.get("key") for build in release.get("builds", []) for launch in build.get("launches", [])}
        for index, fix in enumerate(release.get("fixes", [])):
            claim(fix.get("id"), f"{relative}: fixes[{index}]")
            yield from check_steps(f"{relative}: fixes[{index}]", fix, None, release_launches)
        for disabled in release.get("disableFixes", []):
            if disabled not in game_fixes:
                yield f"{relative}: disables fix {disabled}, which is not one of the game's"
        for index, build in enumerate(release.get("builds", [])):
            if "build" in build:
                continue  # a reference to a build another release defines: checked below, once every build is known
            builds[build.get("id")] = (relative, index, build)
            claim(build.get("id"), f"{relative}: builds[{index}]")
            for fix_index, fix in enumerate(build.get("fixes", [])):
                claim(fix.get("id"), f"{relative}: builds[{index}].fixes[{fix_index}]")
            for disabled in build.get("disableFixes", []):
                if disabled not in game_fixes | release_fixes[relative]:
                    yield f"{relative}: builds[{index}] disables fix {disabled}, which is not one of its release's or its game's"

    # A release may ship a build another release of the game defines, by its id - one set of files, one build.
    for relative, release in releases.items():
        named = set()
        for index, entry in enumerate(release.get("builds", [])):
            if "build" not in entry:
                continue
            target = entry["build"]
            where = f"{relative}: builds[{index}]"
            if target in named:
                yield f"{where}: names build {target} twice"
            named.add(target)
            if target not in builds:
                yield f"{where}: names build {target}, which no release of this game defines"
            elif builds[target][0] == relative:
                yield f"{where}: names build {target}, which this release defines itself"

    # Markers are what a modified copy is matched by; two builds with the same set would make that
    # match a coin toss just as two equal fingerprints would.
    marker_sets = {}
    for relative, index, build in builds.values():
        markers = frozenset((marker.get("path"), marker.get("blake3")) for marker in build.get("markers", []))
        if not markers:
            continue
        where = f"{relative}: builds[{index}]"
        if markers in marker_sets:
            yield f"{where}: same markers as {marker_sets[markers]}"
        marker_sets.setdefault(markers, where)

    # A copy is matched to its build by its sources first. Two builds with the same source would make
    # that match a coin toss - for Steam the depot manifests are the source, so a build id shared by
    # the Windows and the Linux build is fine and the same depot set twice is not.
    seen_sources = {}
    for relative, index, build in builds.values():
        where = f"{relative}: builds[{index}]"
        for source in build.get("sources", []):
            identity = source_identity(source)
            if identity is None:
                continue
            if identity in seen_sources and seen_sources[identity] != where:
                yield f"{where}: has the same {source.get('kind')} source as {seen_sources[identity]}"
            seen_sources.setdefault(identity, where)

    # Then by its files, so two builds with one fingerprint would make that match a coin toss too.
    digests = {}
    for relative, index, build in builds.values():
        digest = build.get("content", {}).get("digest")
        if digest is None:
            continue
        where = f"{relative}: builds[{index}]"
        if digest in digests:
            yield f"{where}: same fingerprint as {digests[digest]}"
        digests.setdefault(digest, where)

    for relative, index, build in builds.values():
        yield from check_build(f"{relative}: builds[{index}]", build, dlc, builds)


def source_identity(source):
    """What makes two sources the same place, or None for one that names no place (a directory)."""
    kind = source.get("kind")
    if kind == "steam":
        return kind, frozenset((source.get("depots") or {}).items())
    if kind == "gog":
        return kind, source.get("buildId")
    if kind == "epic":
        return kind, source.get("buildVersion"), source.get("manifestSha1")
    if kind == "installer":
        return kind, frozenset(file.get("sha256") for file in source.get("files", []))
    if kind == "eshop":
        return kind, source.get("titleId"), source.get("version")
    return None


def check_build(where, build, dlc, builds):
    platforms = set(build.get("platforms", []))
    for field in ("launches", "compatibility"):
        for index, entry in enumerate(build.get(field, [])):
            if entry.get("platform") not in platforms:
                yield f"{where}: {field}[{index}] is for {entry.get('platform')}, which the build does not run on"

    # A key names one launch per platform; steps choose a launch by it.
    keys = [(launch.get("key"), launch.get("platform")) for launch in build.get("launches", [])]
    for key, platform in sorted({entry for entry in keys if keys.count(entry) > 1}):
        yield f"{where}: more than one launch {key} for {platform}"
    launches = {key for key, _ in keys}

    defaults = [launch["platform"] for launch in build.get("launches", []) if launch.get("default")]
    for platform in sorted(set(defaults)):
        if defaults.count(platform) > 1:
            yield f"{where}: more than one default launch for {platform}"

    groups = {group["key"]: group for group in build.get("selectionGroups", [])}
    fixes = build.get("fixes", [])
    for fix in fixes:
        if "selectionGroup" in fix and fix["selectionGroup"] not in groups:
            yield f"{where}: fix {fix.get('name')} answers group {fix['selectionGroup']}, which the build does not declare"
    for key, group in groups.items():
        defaults = [fix for fix in fixes if fix.get("selectionGroup") == key and fix.get("default")]
        if group.get("mode") == "pickOne" and len(defaults) > 1:
            yield f"{where}: group {key} picks one, but {len(defaults)} fixes are its default"

    scripts = {script.get("path") for script in build.get("installScripts", [])}
    for index, fix in enumerate(fixes):
        # A build's fix for a platform the build does not run on can never apply - a mistake, not a choice.
        for platform in (fix.get("when") or {}).get("platforms", []):
            if platform not in platforms:
                yield f"{where}.fixes[{index}]: is for {platform}, which the build does not run on"
        yield from check_steps(f"{where}.fixes[{index}]", fix, scripts, launches)

    sites = [site.get("path") for site in build.get("steamworks", [])]
    for path in sorted({path for path in sites if sites.count(path) > 1}):
        yield f"{where}: steamworks lists {path} more than once"
    for index, site in enumerate(build.get("steamworks", [])):
        origin = site.get("interfaceVersionsFrom", {}).get("buildId")
        if origin is not None and origin not in builds:
            yield f"{where}: steamworks[{index}] read its interface versions from build {origin}, which this game does not have"


def check_steps(where, fix, scripts, launches):
    """What a fix's steps refer to. `scripts` is None for a game's or a release's fix, which has no
    install scripts to have read a step from; `launches` are the keys the fix can name."""
    pinned = {check.get("path") for check in (fix.get("when") or {}).get("files", []) if "blake3" in check}
    for index, step in enumerate(fix.get("steps", [])):
        if not isinstance(step, dict):
            continue
        if "source" in step and (scripts is None or step["source"] not in scripts):
            yield f"{where}.steps[{index}]: read from {step['source']}, which is not one of the build's installScripts"
        if "launch" in step and step["launch"] not in launches:
            yield f"{where}.steps[{index}]: is for launch {step['launch']}, which no build it applies to has"
        if step.get("kind") == "hexEdit":
            # Offsets mean something only in the file they were taken from.
            if step.get("file") not in pinned:
                yield f"{where}.steps[{index}]: edits {step.get('file')}, which the fix's when does not pin by hash"
            for edit_index, edit in enumerate(step.get("edits", [])):
                if len(edit.get("from", "")) != len(edit.get("to", "")):
                    yield f"{where}.steps[{index}].edits[{edit_index}]: writes a different number of bytes than it replaces"


# ── Catalogues ──────────────────────────────────────────────────────────────────────────────────────────────


def documents_in(folder, root, problems, suffixes):
    for path in sorted(folder.iterdir()):
        relative = path.relative_to(root).as_posix()
        if not (path.is_file() and path.suffix in suffixes):
            problems.append(f"{relative}: not a {' or '.join(suffixes)} document")
            continue
        yield path, relative


def load_runtimes(validators, root, problems):
    runtimes = {}
    folder = root / "runtimes"
    if not folder.is_dir():
        return runtimes
    installers = {}
    for path, relative in documents_in(folder, root, problems, (".yaml",)):
        runtime = read_document(path, relative, problems)
        if runtime is None:
            continue
        problems.extend(schema_problems(validators["runtime.schema.json"], relative, runtime))
        if runtime.get("key") != path.stem:
            problems.append(f"{relative}: key {runtime.get('key')} is not the file's name")
        # A bundled installer is recognised by its hash, so one installer under two runtimes would
        # make a game's copy count for whichever was read first.
        for index, copy in enumerate(runtime.get("copies", [])):
            digest = (copy.get("installer") or {}).get("blake3")
            if digest in installers:
                problems.append(f"{relative}: copies[{index}]: the same installer as {installers[digest]}")
            installers.setdefault(digest, f"{relative}: copies[{index}]")
        runtimes[path.stem] = runtime
    return runtimes


def load_catalogue(validators, root, problems):
    """Every plugin of a catalogue: key to its plugin document and its versions. YAML or JSON."""
    catalogue = {}
    folder = root / "plugins" if (root / "plugins").is_dir() else root
    suffixes = (".yaml", ".json")
    for plugin_folder in sorted(folder.iterdir()):
        name = f"plugins/{plugin_folder.name}"
        if not plugin_folder.is_dir():
            problems.append(f"{name}: not a plugin folder")
            continue
        heads = [plugin_folder / f"plugin{suffix}" for suffix in suffixes if (plugin_folder / f"plugin{suffix}").is_file()]
        plugin = read_document(heads[0], f"{name}/{heads[0].name}", problems) if len(heads) == 1 else None
        if plugin is None:
            problems.append(f"{name}: no readable plugin.yaml or plugin.json (exactly one)")
            continue
        problems.extend(schema_problems(validators["plugin.schema.json"], f"{name}/{heads[0].name}", plugin))
        if plugin.get("key") != plugin_folder.name:
            problems.append(f"{name}/{heads[0].name}: key {plugin.get('key')} is not the folder's name")

        versions = {}
        for path in sorted(plugin_folder.iterdir()):
            relative = f"{name}/{path.name}"
            if path.stem == "plugin":
                continue
            if not (path.is_file() and path.suffix in suffixes):
                problems.append(f"{relative}: not a .yaml or .json document")
                continue
            version = read_document(path, relative, problems)
            if version is None:
                continue
            problems.extend(schema_problems(validators["version.schema.json"], relative, version))
            if version.get("version") != path.stem:
                problems.append(f"{relative}: version {version.get('version')} is not the file's name")
            if version.get("key") != plugin_folder.name:
                problems.append(f"{relative}: key {version.get('key')} is not the plugin's")
            if version.get("kind") != plugin.get("kind"):
                problems.append(f"{relative}: kind {version.get('kind')} is not the plugin's {plugin.get('kind')}")
            slots = [(file.get("os"), file.get("architecture")) for file in version.get("files", [])]
            for os, architecture in sorted({slot for slot in slots if slots.count(slot) > 1}, key=str):
                problems.append(f"{relative}: more than one file for {os} {architecture}")
            problems.extend(check_contract(relative, version.get("config")))
            versions[path.stem] = version
        catalogue[plugin_folder.name] = {"plugin": plugin, "versions": versions}

    # What a version requires has to be in the catalogue and of a kind that can stand beside it.
    allowed = {"hook": set(), "mod": {"mod"}, "library": {"library"}}
    for key, entry in catalogue.items():
        for version_name, version in entry["versions"].items():
            for required in version.get("requires", []):
                found = lookup(catalogue, required)
                where = f"plugins/{key}/{version_name}"
                if found is None:
                    problems.append(f"{where}: requires {required.get('key')} {required.get('version')}, which the catalogue does not have")
                elif found["kind"] not in allowed.get(version.get("kind"), set()):
                    problems.append(f"{where}: {article(version.get('kind'))} cannot require the {found['kind']} {required.get('key')}")
    return catalogue


def article(word):
    return f"{'an' if str(word)[:1] in 'aeiou' else 'a'} {word}"


def lookup(catalogue, reference):
    entry = catalogue.get(reference.get("key"))
    return entry["versions"].get(reference.get("version")) if entry else None


def check_contract(relative, contract):
    """A contract has to be a schema a validator can use, and its defaults have to satisfy it."""
    if not isinstance(contract, dict):
        return
    try:
        Draft202012Validator.check_schema(contract)
    except Exception as error:  # SchemaError, or a pattern the regex engine refuses
        yield f"{relative}: config is not a usable schema: {getattr(error, 'message', error)}"
        return
    fields = dict(contract.get("properties", {}))
    for name, field in list(fields.items()):
        if field.get("type") == "object":
            fields.update({f"{name}.{inner}": value for inner, value in field.get("properties", {}).items()})
    for name, field in fields.items():
        if "default" in field:
            for error in Draft202012Validator({k: v for k, v in field.items() if k != "default"}).iter_errors(field["default"]):
                yield f"{relative}: config field {name}: its default {json.dumps(field['default'])} breaks its own rule: {error.message}"
    for name in contract.get("required", []):
        if name not in contract.get("properties", {}):
            yield f"{relative}: config requires {name}, which it does not define"


def check_references(where, assignments, builds, runtimes, catalogue):
    """The runtimes and plugins a game, a release or its builds name: in the catalogues, of a kind that can be used there,
    configured as the version's contract says."""
    for index, build in enumerate(builds):
        for dependency in build.get("dependencies", []):
            if dependency.get("runtime") not in runtimes:
                yield f"{where}: builds[{index}] needs runtime {dependency.get('runtime')}, which runtimes/ does not have"
    if catalogue is None:
        return
    for index, assignment in enumerate(assignments):
        reference = assignment.get("plugin", {})
        found = lookup(catalogue, reference)
        if found is None:
            yield f"{where}: plugin {reference.get('key')} {reference.get('version')} is not in the plugin catalogue"
            continue
        if found.get("kind") not in ("hook", "mod"):
            yield f"{where}: plugin {reference.get('key')} is {article(found.get('kind'))}, which is never assigned"
            continue
        contract = found.get("config")
        config = assignment.get("config", {})
        if contract is None:
            if config:
                yield f"{where}: plugin {reference.get('key')} {reference.get('version')} takes no configuration"
            continue
        try:
            errors = list(Draft202012Validator({**contract, "additionalProperties": False}).iter_errors(config))
        except Exception:
            continue  # the catalogue already reported the contract
        for error in errors:
            location = "/".join(str(part) for part in error.absolute_path)
            yield f"{where}: plugin {reference.get('key')} config{('.' + location) if location else ''}: {error.message}"


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    arguments.add_argument("root", type=Path)
    arguments.add_argument("--plugins", type=Path, help="a plugin catalogue kept elsewhere")
    options = arguments.parse_args()
    sys.exit(main(options.root, options.plugins))
