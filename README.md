# Stoker schemas

JSON Schemas for the formats Stoker shares between hosts, published at
`https://schemas.stokerhq.app/`.

## Metadata — v1

JSON Schemas (draft 2020-12) for the Stoker metadata repository: a public git repository that any
Stoker host reads to find games, their store pages, achievements, how their builds are laid out and
started, and the fixes, mods and quirks they need to run under Stoker.

```
metadata/
  v1/                          the schemas, served as https://schemas.stokerhq.app/metadata/v1/<file>
  examples/                    a small, fictional metadata repository that validates
plugins/
  v1/                          the plugin catalogue's schemas: https://schemas.stokerhq.app/plugins/v1/<file>
validate.py                    checks a metadata repository and a plugin catalogue against them
```

### Layout of a metadata repository

```
games/
  <game id>/
    game.json                  identity, external ids, store page, tags, cloud saves, settings, plugins, fixes
    media.json                 artwork and the media reel (images inlined)
    achievements.json          achievement definitions, each with the other names it is granted under
    stats.json                 stat definitions
    leaderboards.json          leaderboard definitions
    dlc/
      <dlc id>.json            one DLC: external ids, store page, artwork, the other DLC it needs
    releases/
      <release id>.json        one version, its fixes, and every known build of it with the build's own fixes
runtimes/
  <key>.json                   one runtime machines need (Visual C++, DirectX) and its known copies
plugins/                       the plugin catalogue, for now (see "Plugin catalogue")
```

| Document | Schema |
|---|---|
| `game.json` | `metadata/v1/game.schema.json` |
| `media.json` | `metadata/v1/media.schema.json` |
| `achievements.json` | `metadata/v1/achievements.schema.json` |
| `stats.json` | `metadata/v1/stats.schema.json` |
| `leaderboards.json` | `metadata/v1/leaderboards.schema.json` |
| `dlc/*.json` | `metadata/v1/dlc.schema.json` |
| `releases/*.json` | `metadata/v1/release.schema.json` |
| `runtimes/*.json` | `metadata/v1/runtime.schema.json` |

`metadata/v1/common.schema.json` holds the types the others share. Only `game.json` is required.

**DLC are not games.** A DLC has no releases, nothing to start and no achievements of its own (the
stores keep those in its game's list); it is what a store sells — a name, ids, a page, artwork — and
lives in its game's folder. Whether a build carries it, and with which files, is that build's `dlc`
fix, which names it by `dlcId`.

**Runtimes are described once.** A build names a runtime by key (`vcredist-14-x64`); everything about
installing it once per machine — the probe that says it is there, the silent switches, the exit codes
that mean success — is in `runtimes/<key>.json`, with the copies games bundle, recognised by their
installer's hash wherever a game put it.

### Rules the format is built on

- **Ids are UUIDs, minted once and never changed.** A folder is named after its game's id, a release
  file after its release's id. A rename changes `name`, not the path. External ids — the stores'
  (Steam, GOG, Epic, Ubisoft, eShop) and the databases' (IGDB, PCGamingWiki, SteamGridDB, MobyGames,
  Wikidata) — are attributes, never keys: a game may have none, or all of them.
- **A build is a set of files.** The Steam depots, the GOG installer and a copy somebody had on disk
  are three builds of one release when their files differ, and one build with several `sources` when
  they are the same files — a DRM-free game often is. No two builds of a game share a fingerprint, a
  set of markers or a source.
- **One file per thing that changes on its own.** A new version is a new release file, never an edit
  of an old one; a new description does not diff next to megabytes of images.
- **Only what is true for every instance.** Nothing an instance decided for itself is here: hidden
  or archived titles, locked fields, custom tags, Steam tracking, and nothing about any player —
  unlocks, stats, scores, saves, play time.
- **Images are inlined, trailers are referenced.** Images are base64 with their SHA-256, so a clone
  is the complete record with no CDN to lose. A trailer is tens of megabytes; its tracks are recorded
  by source and hash, and an instance fetches and checks them.
- **The game's files are never here.** A build is described by a *fingerprint* of its files
  (`contentFingerprint`, algorithm `stoker-tree-1`, computed from the BLAKE3 every Stoker manifest
  already holds per file), by marker files and by its store ids. An instance matches its own copy to
  an entry by them. Fixes, settings and plugin assignments are applied to a copy; the stored build is
  never edited.
- **Values are names, not numbers.** Enums are camelCase strings; the database's integers are an
  implementation detail of one program.
- **Languages are BCP 47.** Prose is a map of language tag to text, so translations are additions.
- **Dates are as precise as they are known** (`2017`, `2017-02`, `2017-02-24`).

### How a copy finds its fixes

A Stoker instance holds a copy of a game from some store, or from somebody's disk. To make it run:

1. **Match it to a build.** First by its `sources` — a store's ids for those files; for Steam the
   depot manifests, never the build id alone, which every depot of a branch shares — then by the
   fingerprint of all its files (`content`), then by `markers`: a few files nothing writes to, such
   as the executables, which still match when configs, saves or a hand-applied patch changed the
   rest. A copy that matches nothing still gets its game's fixes.
2. **Collect the fixes, in order:** the build's own (`setup`, `languagePack`, `dlc`, `unpacked`,
   and patches only this copy needs), then the release's, then the game's — leaving out those a
   release or build lists in `disableFixes`, the optional ones nobody chose, and the answers to a
   `selectionGroup` nobody picked.
3. **Apply each fix whose `when` holds**, checked against the files as they are at that point, and
   skip one whose `appliedWhen` already holds. A fix's steps — `files`, `delete`, `rename`,
   `iniSet`, `hexEdit`, `binaryPatch`, `unpack`, `registry`, `firewall`, `run` — change the install
   once; `environment`, `arguments` and `windowsCompatibility` apply every time the game starts,
   for every launch or for the one whose `key` they name.

`unpack` runs an unpacker plugin over the instance's own copy and keeps the result only when it hashes
as recorded; it appears only in a build's `unpacked` fix, which comes first, so a game's fix written
against the unpacked executable finds it unpacked.

Values the installing machine fills in — registry values, `iniSet` values, `run` and `arguments`
arguments, `environment` values — may hold `%INSTALLDIR%`, the install's root in the machine's own
form; `%%` is a literal `%`, and any other `%` is refused.

### Trust

Hosts apply what this repository says to their players' machines, so what a fix may do is limited
by the schema, not by review alone:

- `run` and `firewall` name a program inside the install;
- `registry` writes below the game's own `Software\<vendor>` key, never under `Microsoft`,
  `Classes`, `Policies`, `RegisteredApplications` or `Clients`;
- `environment` never sets a variable that makes a runtime load code (`LD_PRELOAD`, `DYLD_*`,
  `COR_PROFILER`, `DOTNET_STARTUP_HOOKS`, `JAVA_TOOL_OPTIONS` and their kind);
- `delete`, `rename`, `iniSet`, `hexEdit` and the rest touch paths inside the install only.

A step read from the publisher's own install script (`source`) is exempt from the first two, because
the publisher's installer would have done the same. Code a fix needs goes into the install as files,
named by hash.

Only the maintainer pushes to this repository. Plugins — code that runs inside games, or on the host —
are held to more: a host's administrator approves every version before it runs (see "Plugin
catalogue").

Because `when` names files by hash, a fix written once on the game serves every copy that has those
bytes, whichever store it came from.

### Plugin catalogue — v1

Plugins are a format of their own, so the catalogue can move to a repository of its own: games name
a plugin only by key and version (`pluginRef`), never by where it is kept. The layout is the same
wherever it lives:

```
plugins/
  <key>/
    plugin.json                what the plugin is: kind, name, author, licence, homepage
    <version>.json             one immutable version: its files by hash, where their author publishes
                               them, the config contract from its manifest, what it requires
```

| Document | Schema |
|---|---|
| `plugins/<key>/plugin.json` | `plugins/v1/plugin.schema.json` |
| `plugins/<key>/<version>.json` | `plugins/v1/version.schema.json` |

Kinds: `hook` (native code a bootstrap loads in a game), `mod` (files laid into a game for a mod
loader, the loader included), `unpacker` (removes a DRM wrapper from a stored build, on the host
itself) and `library` (loaded by other plugins). Hooks and mods are assigned to games and releases;
an unpacker is named only by a build's `unpack` step.

**No binaries.** A version lists each file's SHA-256 and BLAKE3 and the URLs its author publishes it
at; whatever a host downloads, or an administrator supplies when every link is dead, is checked
against the hash. The same holds for runtime installers. Nobody's binaries are redistributed, which
licences such as Steamless's would not allow anyway.

**A host's administrator approves every plugin version once** before it runs anywhere on that host,
whatever the catalogue says — an unpacker runs on the host itself, a hook or a mod inside players'
games. The catalogue's hashes make sure what was approved is what runs.

### Versions

Every document carries `schemaVersion`, `"<major>.<minor>"`. The schemas under `metadata/v1/` are
always the newest minor of major 1, and a minor version only **adds**: fields, enum values, kinds.
It never changes what an existing field means or makes an optional one required, so every document
that was valid stays valid.

A reader:

- refuses a major it does not know;
- reads a newer minor of a major it knows, and **ignores fields it does not know**;
- treats a value it does not know in a field that only describes — a provider, a tag kind, a
  definition's origin — as absent, and an entry for a platform it does not know as not for this host;
- never silently skips something it does not know that would change what is done to an install — a
  fix role, a step kind, an integration provider or action. It treats that build as needing a newer
  Stoker instead, because a skipped fix is a broken install reported as a working one.

The schemas themselves stay strict (`additionalProperties: false`), so this repository's checks
catch a misspelt field; it is readers that are lenient, not the schemas.

### Checks beyond the schemas

`validate.py` runs every document against its schema, then checks what a schema cannot see:

- a game folder holds only the files and folders a game has, and every document is JSON with no key
  twice in one object;
- folder and file names match the ids inside, every document names its own game, and no id is used
  twice anywhere in the repository;
- an inlined image is a PNG, JPEG or WebP whose bytes have the stated type, size and SHA-256;
- partial dates are real days;
- a DLC's requirements are other DLC of the same game, and never go round in a circle;
- release names and orders are unique within a game, and no two builds share a source (for Steam,
  a depot set), a fingerprint or a set of markers;
- a build's launches, integrations, compatibility reports and fixes are for platforms it runs on, it
  has one launch per key and platform and at most one default launch per platform, its fixes answer
  groups it declares (a `pickOne` group with at most one default) and name DLC the game has, its
  steps come from install scripts it carries, and interface versions are read from builds the game
  has;
- a step's `launch` is a key a build it applies to has;
- `disableFixes` names fixes that exist above it, and a `hexEdit` edits a file its fix's `when` pins
  by hash and writes as many bytes as it replaces;
- no achievement name or alias, stat or leaderboard name is used twice, nor a tag slug.

- every runtime a build needs is in `runtimes/`, and no installer belongs to two runtimes;
- every plugin a game, release or `unpack` step names is in the catalogue at that version, of a kind
  that can be used there, configured as its contract says; a version's kind is its plugin's, it has one
  file per platform and architecture, what it requires exists and may stand beside it, and its config
  contract is a usable schema whose defaults obey it.

```
python -I validate.py <metadata repository root> [--plugins <plugin catalogue root>]
python -I validate.py metadata/examples
```

The catalogue is read from `--plugins` when given, else from the repository's own `plugins/`.
Without either, plugin references go unchecked and the run says so.

Needs `jsonschema[format-nongpl]` 4.18 or newer. Without the `format-nongpl` extra jsonschema
passes every URI unchecked, so the validator refuses to run without it.

### Not decided yet

- **Signed plugin versions.** Approval by each host's administrator and the catalogue's hashes are the
  trust model for now; a signature a host could verify independently of where it cloned the catalogue
  from comes when the catalogue has its own repository.
- **Where fix content comes from.** A `files` step names its files by fingerprint and a
  `binaryPatch` its diff by hash; for a community patch or a mod an instance does not already hold,
  the repository will need to say where to get them. That arrives as a minor version.
- **Emulator settings** for console builds, and **age ratings** — neither is modelled yet.
- **Full file listings per build.** The fingerprint identifies a build; a listing would also show
  *where* two builds differ, at a cost of megabytes per large game.
