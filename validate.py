"""Validates a Stoker metadata repository, or a plugin catalogue, against the v1 schemas.

    python -I validate.py <repository root> [--plugins <plugin catalogue root>]

The root may hold `games/`, `runtimes/` and `plugins/`. The plugin catalogue is read from
`--plugins` when given, else from the root's own `plugins/` - it is a format of its own, kept beside
the games for now and in a repository of its own later. Without one, plugin references go unchecked
and the run says so.

Two passes. The first checks every document against its schema. The second checks what a schema
cannot see, because it spans documents or needs the bytes decoded: a folder is named after the id
inside it, every document of a game names that game, an id means one thing in the whole repository,
an inlined image is the image its type, size and hash say, a DLC's requirements are DLC of the same
game, a copy can only ever match one build, fixes refer to things that exist, and every runtime and
plugin a game names is in the catalogue, of the right kind, configured as its contract says.

Exits 0 when the repository is valid, 1 when anything is wrong, listing every problem rather
than stopping at the first - a contributor fixing one error per run gives up.
"""

import argparse
import base64
import datetime
import hashlib
import json
import struct
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS = [Path(__file__).resolve().parent / family / "v1" for family in ("metadata", "plugins")]

DOCUMENTS = {
    "game.json": "game.schema.json",
    "media.json": "media.schema.json",
    "achievements.json": "achievements.schema.json",
    "stats.json": "stats.schema.json",
    "leaderboards.json": "leaderboards.schema.json",
}

# Folders under a game, one document per file, by the schema their files follow.
NESTED = {"dlc": "dlc", "releases": "release"}


def require_uri_checks():
    # Without the format-nongpl extra, jsonschema has no checker for "uri" and passes every value
    # without a word - a broken link would validate. Refuse to run rather than check less than it says.
    missing = [name for name in ("uri", "date") if name not in FormatChecker.checkers]
    if missing:
        print("validate.py needs jsonschema[format-nongpl] to check " + ", ".join(missing) + ' formats: pip install "jsonschema[format-nongpl]"')
        sys.exit(2)


def load_validators():
    resources = {}
    for path in (path for folder in SCHEMAS for path in folder.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources[path.name] = schema
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in resources.values()
    )
    return {
        name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for name, schema in resources.items()
    }


def no_duplicate_keys(pairs):
    # json.loads keeps the last of two equal keys without a word, so a second "en" would silently
    # replace the first translation.
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(f"key {key!r} appears more than once in one object")
    return dict(pairs)


def images(node, where):
    """Every inlined image in a document, with a readable location for messages."""
    if isinstance(node, dict):
        if {"mediaType", "sha256", "data"} <= node.keys():
            yield where, node
        for key, value in node.items():
            yield from images(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from images(value, f"{where}[{index}]")


def image_header(data):
    """The media type and size the bytes themselves declare, or None when they are none of ours."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return "image/webp", width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L":
            b = data[21:25]
            return "image/webp", 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | (b[1] >> 6))
        if chunk == b"VP8X":
            return "image/webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        return None
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 <= len(data) and data[offset] == 0xFF:
            marker = data[offset + 1]
            length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return "image/jpeg", width, height
            offset += 2 + length
    return None


def partial_date_problem(value):
    # The schema checks the shape; only a calendar knows 2017-02-30 is not a day.
    if isinstance(value, str) and len(value) == 10:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return f"{value} is not a day"
    return None


def main(root, plugins_root=None):
    require_uri_checks()
    validators = load_validators()
    problems = []
    games = {}
    ids = {}  # every id in the repository, to where it was first seen

    def claim(identifier, where):
        if not isinstance(identifier, str):
            return
        if identifier in ids:
            problems.append(f"{where}: id {identifier} is already {ids[identifier]}")
        else:
            ids[identifier] = where

    if not any((root / name).is_dir() for name in ("games", "runtimes", "plugins")):
        print(f"{root}: no games/, runtimes/ or plugins/ folder")
        return 1

    catalogue_root = plugins_root or (root / "plugins" if (root / "plugins").is_dir() else None)
    catalogue = load_catalogue(validators, catalogue_root, problems) if catalogue_root else None
    runtimes = load_runtimes(validators, root, problems)
    references = []  # (where, document) of every game and release, checked against the catalogues at the end

    for folder in sorted((root / "games").iterdir()) if (root / "games").is_dir() else []:
        if not folder.is_dir():
            problems.append(f"games/{folder.name}: not a game folder")
            continue

        # A misspelt file name ("achievement.json") would otherwise be skipped without a word.
        for entry in sorted(folder.iterdir()):
            known = entry.name in DOCUMENTS if entry.is_file() else entry.name in NESTED
            if not known:
                problems.append(f"games/{folder.name}/{entry.name}: not a file or folder a game has")
            elif entry.is_dir():
                for child in sorted(entry.iterdir()):
                    if not (child.is_file() and child.suffix == ".json"):
                        problems.append(f"games/{folder.name}/{entry.name}/{child.name}: not a .json document")

        documents = {}
        nested = [*sorted((folder / "dlc").glob("*.json")), *sorted((folder / "releases").glob("*.json"))]
        for path in [*(folder / name for name in DOCUMENTS), *nested]:
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
            except ValueError as error:
                problems.append(f"{relative}: not valid JSON: {error}")
                continue

            schema = DOCUMENTS.get(path.name) if path.parent == folder else f"{NESTED[path.parent.name]}.schema.json"
            problems.extend(schema_problems(validators[schema], relative, document))

            for where, image in images(document, "$"):
                try:
                    data = base64.b64decode(image["data"], validate=True)
                except (ValueError, TypeError):
                    continue  # the schema already reported it
                if hashlib.sha256(data).hexdigest() != image["sha256"]:
                    problems.append(f"{relative}: {where}: sha256 does not match the image's bytes")
                header = image_header(data)
                if header is None:
                    problems.append(f"{relative}: {where}: the bytes are not a PNG, JPEG or WebP image")
                elif header != (image["mediaType"], image.get("width"), image.get("height")):
                    problems.append(f"{relative}: {where}: the bytes are a {header[1]}x{header[2]} {header[0]}, not what the entry says")

            documents[path] = document

        game = documents.get(folder / "game.json")
        if game is None:
            problems.append(f"games/{folder.name}: no game.json")
            continue
        if game.get("id") != folder.name:
            problems.append(f"games/{folder.name}/game.json: id {game.get('id')} is not the folder's name")
        claim(game.get("id"), f"game {folder.name}")
        games[folder.name] = game

        dlc = {path.stem: document for path, document in documents.items() if path.parent.name == "dlc"}
        releases = {path: document for path, document in documents.items() if path.parent.name == "releases"}

        for path, document in documents.items():
            relative = path.relative_to(root).as_posix()
            if path.name != "game.json" and document.get("gameId") != folder.name:
                problems.append(f"{relative}: gameId is not the folder's game")
            if path.parent != folder:
                if path.stem != document.get("id"):
                    problems.append(f"{relative}: id {document.get('id')} is not the file's name")
                claim(document.get("id"), relative)
            for value in (document.get("storePage", {}).get("releaseDate"), document.get("releasedAt")):
                if (problem := partial_date_problem(value)) is not None:
                    problems.append(f"{relative}: {problem}")
            if path.parent.name == "dlc":
                for required in document.get("requires", []):
                    if required == path.stem or required not in dlc:
                        problems.append(f"{relative}: requires {required}, which is not another DLC of this game")

        # A game's fix may name any launch one of its builds has.
        game_launches = {launch.get("key") for release in releases.values() for build in release.get("builds", []) for launch in build.get("launches", [])}
        references.append((f"games/{folder.name}/game.json", game))
        references.extend((path.relative_to(root).as_posix(), release) for path, release in releases.items())
        game_fixes = {fix.get("id") for fix in game.get("fixes", [])}
        for index, fix in enumerate(game.get("fixes", [])):
            claim(fix.get("id"), f"games/{folder.name}/game.json: fixes[{index}]")
            problems.extend(check_steps(f"games/{folder.name}/game.json: fixes[{index}]", fix, None, game_launches))

        problems.extend(check_requirement_cycles(folder.name, dlc))
        problems.extend(check_releases(root, releases, dlc, game_fixes, claim))

        slugs = [tag.get("slug") for tag in game.get("tags", []) if isinstance(tag, dict)]
        for slug in sorted({slug for slug in slugs if slugs.count(slug) > 1}):
            problems.append(f"games/{folder.name}/game.json: tag {slug} appears more than once")

        # Every name a game grants or looks up by has to mean one thing: an apiName or alias that two
        # achievements share would record an unlock against whichever was found first.
        for file, key, field in (("achievements.json", "achievements", "apiName"), ("stats.json", "stats", "name"), ("leaderboards.json", "leaderboards", "name")):
            entries = (documents.get(folder / file) or {}).get(key, [])
            names = [entry[field] for entry in entries if isinstance(entry, dict) and field in entry]
            if key == "achievements":
                names += [alias for entry in entries if isinstance(entry, dict) for alias in entry.get("aliases", [])]
            for name in sorted({name for name in names if names.count(name) > 1}):
                problems.append(f"games/{folder.name}/{file}: {name} names more than one {key[:-1]}")

    for where, document in references:
        problems.extend(check_references(where, document, runtimes, catalogue))

    for problem in problems:
        print(problem)
    if catalogue is None and references:
        print("no plugin catalogue (plugins/ or --plugins): plugin references were not checked")
    plugins = len(catalogue) if catalogue else 0
    print(f"{len(games)} games, {len(runtimes)} runtimes, {plugins} plugins, {len(problems)} problems")
    return 1 if problems else 0


def schema_problems(validator, relative, document):
    for error in validator.iter_errors(document):
        location = "/".join(str(part) for part in error.absolute_path) or "(document)"
        # A failed `not` quotes the whole instance, which for a release is the whole file.
        message = f"must not match {json.dumps(error.validator_value)}" if error.validator == "not" else error.message
        # The registry and environment rules are generated, case-folded patterns nobody can read.
        if error.validator in ("pattern", "not") and len(json.dumps(error.validator_value)) > 160:
            message = f"{json.dumps(error.instance)} is not allowed here; the schema's description of this field says why"
        yield f"{relative}: {location}: {message}"


def read_document(path, relative, problems):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except ValueError as error:
        problems.append(f"{relative}: not valid JSON: {error}")
        return None


def load_runtimes(validators, root, problems):
    runtimes = {}
    folder = root / "runtimes"
    if not folder.is_dir():
        return runtimes
    installers = {}
    for path in sorted(folder.iterdir()):
        relative = path.relative_to(root).as_posix()
        if not (path.is_file() and path.suffix == ".json"):
            problems.append(f"{relative}: not a .json document")
            continue
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
    """Every plugin of a catalogue: key to its plugin.json and its versions."""
    catalogue = {}
    folder = root / "plugins" if (root / "plugins").is_dir() else root
    for plugin_folder in sorted(folder.iterdir()):
        name = f"plugins/{plugin_folder.name}"
        if not plugin_folder.is_dir():
            problems.append(f"{name}: not a plugin folder")
            continue
        plugin = read_document(plugin_folder / "plugin.json", f"{name}/plugin.json", problems) if (plugin_folder / "plugin.json").is_file() else None
        if plugin is None:
            problems.append(f"{name}: no readable plugin.json")
            continue
        problems.extend(schema_problems(validators["plugin.schema.json"], f"{name}/plugin.json", plugin))
        if plugin.get("key") != plugin_folder.name:
            problems.append(f"{name}/plugin.json: key {plugin.get('key')} is not the folder's name")

        versions = {}
        for path in sorted(plugin_folder.iterdir()):
            relative = f"{name}/{path.name}"
            if path.name == "plugin.json":
                continue
            if not (path.is_file() and path.suffix == ".json"):
                problems.append(f"{relative}: not a .json document")
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
    allowed = {"hook": set(), "mod": {"mod"}, "unpacker": {"library"}, "library": {"library"}}
    for key, entry in catalogue.items():
        for version_name, version in entry["versions"].items():
            for required in version.get("requires", []):
                found = lookup(catalogue, required)
                where = f"plugins/{key}/{version_name}.json"
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


def check_references(where, document, runtimes, catalogue):
    """The runtimes and plugins a game or a release names: in the catalogues, of a kind that can be used there,
    configured as the version's contract says."""
    assignments = list(document.get("plugins", []))
    for index, build in enumerate(document.get("builds", [])):
        for dependency in build.get("dependencies", []):
            if dependency.get("runtime") not in runtimes:
                yield f"{where}: builds[{index}] needs runtime {dependency.get('runtime')}, which runtimes/ does not have"
        for fix in build.get("fixes", []):
            for step in fix.get("steps", []):
                if step.get("kind") == "unpack" and catalogue is not None:
                    found = lookup(catalogue, step.get("unpacker", {}))
                    if found is None:
                        yield f"{where}: builds[{index}] unpacks with {step['unpacker'].get('key')} {step['unpacker'].get('version')}, which the plugin catalogue does not have"
                    elif found.get("kind") != "unpacker":
                        yield f"{where}: builds[{index}] unpacks with {step['unpacker'].get('key')}, which is {article(found.get('kind'))}, not an unpacker"
    if catalogue is None:
        return
    for index, assignment in enumerate(assignments):
        reference = assignment.get("plugin", {})
        found = lookup(catalogue, reference)
        if found is None:
            yield f"{where}: plugins[{index}]: {reference.get('key')} {reference.get('version')} is not in the plugin catalogue"
            continue
        if found.get("kind") not in ("hook", "mod"):
            yield f"{where}: plugins[{index}]: {reference.get('key')} is {article(found.get('kind'))}, which is never assigned"
            continue
        contract = found.get("config")
        config = assignment.get("config", {})
        if contract is None:
            if config:
                yield f"{where}: plugins[{index}]: {reference.get('key')} {reference.get('version')} takes no configuration"
            continue
        try:
            errors = list(Draft202012Validator({**contract, "additionalProperties": False}).iter_errors(config))
        except Exception:
            continue  # the catalogue already reported the contract
        for error in errors:
            location = "/".join(str(part) for part in error.absolute_path)
            yield f"{where}: plugins[{index}].config{('.' + location) if location else ''}: {error.message}"


def check_requirement_cycles(game, dlc):
    # Requirements are walked to find everything a DLC needs; a cycle would walk forever.
    state = {}

    def visit(identifier, trail):
        if state.get(identifier) == "done":
            return
        if state.get(identifier) == "walking":
            yield f"games/{game}/dlc: requirements go round in a circle: {' -> '.join(trail + [identifier])}"
            return
        state[identifier] = "walking"
        for required in dlc.get(identifier, {}).get("requires", []):
            if required in dlc:
                yield from visit(required, trail + [identifier])
        state[identifier] = "done"

    for identifier in sorted(dlc):
        yield from visit(identifier, [])


def check_releases(root, releases, dlc, game_fixes, claim):
    # Within a game a release is found by its name and "newest" by its order, so neither may repeat.
    for field in ("name", "order"):
        seen = {}
        for path, release in releases.items():
            value = release.get(field)
            if value in seen:
                yield f"{path.relative_to(root).as_posix()}: {field} {value} is also {seen[value]}'s"
            seen.setdefault(value, path.stem)

    builds = {}
    release_fixes = {}
    for path, release in releases.items():
        relative = path.relative_to(root).as_posix()
        release_fixes[path] = {fix.get("id") for fix in release.get("fixes", [])}
        release_launches = {launch.get("key") for build in release.get("builds", []) for launch in build.get("launches", [])}
        for index, fix in enumerate(release.get("fixes", [])):
            claim(fix.get("id"), f"{relative}: fixes[{index}]")
            yield from check_steps(f"{relative}: fixes[{index}]", fix, None, release_launches)
        for disabled in release.get("disableFixes", []):
            if disabled not in game_fixes:
                yield f"{relative}: disables fix {disabled}, which is not one of the game's"
        for index, build in enumerate(release.get("builds", [])):
            builds[build.get("id")] = (path, index, build)
            claim(build.get("id"), f"{relative}: builds[{index}]")
            for fix_index, fix in enumerate(build.get("fixes", [])):
                claim(fix.get("id"), f"{relative}: builds[{index}].fixes[{fix_index}]")
            for disabled in build.get("disableFixes", []):
                if disabled not in game_fixes | release_fixes[path]:
                    yield f"{relative}: builds[{index}] disables fix {disabled}, which is not one of its release's or its game's"

    # Markers are what a modified copy is matched by; two builds with the same set would make that
    # match a coin toss just as two equal fingerprints would.
    marker_sets = {}
    for path, index, build in builds.values():
        markers = frozenset((marker.get("path"), marker.get("blake3")) for marker in build.get("markers", []))
        if not markers:
            continue
        where = f"{path.relative_to(root).as_posix()}: builds[{index}]"
        if markers in marker_sets:
            yield f"{where}: same markers as {marker_sets[markers]}"
        marker_sets.setdefault(markers, where)

    # A copy is matched to its build by its sources first. Two builds with the same source would make
    # that match a coin toss - for Steam the depot manifests are the source, so a build id shared by
    # the Windows and the Linux build is fine and the same depot set twice is not.
    seen_sources = {}
    for path, index, build in builds.values():
        where = f"{path.relative_to(root).as_posix()}: builds[{index}]"
        for source in build.get("sources", []):
            identity = source_identity(source)
            if identity is None:
                continue
            if identity in seen_sources and seen_sources[identity] != where:
                yield f"{where}: has the same {source.get('kind')} source as {seen_sources[identity]}"
            seen_sources.setdefault(identity, where)

    # Then by its files, so two builds with one fingerprint would make that match a coin toss too.
    digests = {}
    for path, index, build in builds.values():
        digest = build.get("content", {}).get("digest")
        if digest is None:
            continue
        where = f"{path.relative_to(root).as_posix()}: builds[{index}]"
        if digest in digests:
            yield f"{where}: same fingerprint as {digests[digest]}"
        digests.setdefault(digest, where)

    for path, index, build in builds.values():
        yield from check_build(f"{path.relative_to(root).as_posix()}: builds[{index}]", build, dlc, builds)


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
    for field in ("launches", "integrations", "compatibility"):
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
        if "dlcId" in fix and fix["dlcId"] not in dlc:
            yield f"{where}: fix {fix.get('name')} is DLC {fix['dlcId']}, which the game has no dlc/ file for"
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

    for index, integration in enumerate(build.get("integrations", [])):
        origin = integration.get("interfaceVersionsFrom", {}).get("buildId")
        if origin is not None and origin not in builds:
            yield f"{where}: integrations[{index}] read its interface versions from build {origin}, which this game does not have"


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


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    arguments.add_argument("root", type=Path)
    arguments.add_argument("--plugins", type=Path, help="a plugin catalogue kept elsewhere")
    options = arguments.parse_args()
    sys.exit(main(options.root, options.plugins))
