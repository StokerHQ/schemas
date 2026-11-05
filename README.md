# Stoker schemas

JSON Schemas for the formats Stoker shares, published at `https://schemas.stokerhq.app/`.

## Recipes (metadata) — v1

The format of the recipes repository: a public repository of **how to get a game working with Stoker**. A recipe
holds what Stoker needs to make a game work and cannot work out from the game's files itself: which store ids are the
game and its DLC, which build is which, how it starts, the fixes, plugins and hooks a version needs, the runtimes it
needs, where it saves. The game's store page words and pictures are archived beside it, as a convenience: they were
tedious to find. Stoker comes to it with one of two questions — *I have game Y from store X*, or *I have this
folder* — and the format answers both.

**v1 is still being developed.** Until the first recipe is published any field may change; from then on v1 only
grows by minor versions that add (see "Versions").

```
metadata/
  v1/                          the schemas, served as https://schemas.stokerhq.app/metadata/v1/<file>
  examples/                    a small, fictional recipes repository that validates
plugins/
  v1/                          the plugin registries' schemas: https://schemas.stokerhq.app/plugins/v1/<file>
  examples/                    a small, fictional plugin registry that validates
validate.py                    checks a recipes repository and a plugin registry against them
```

### Layout of a recipes repository

```
games/
  <slug>/
    game.yaml                  identity, every store's ids, DLC, editions, saves, the game's fixes, presentation refs
    achievements.yaml          achievement definitions with their names and descriptions, icons by archive ref
    stats.yaml                 stat definitions
    leaderboards.yaml          leaderboard definitions
    builds/
      <name>.yaml              one release (a version), every known build of it, and their fixes
runtimes/
  <key>.yaml                   one runtime machines need (Visual C++, DirectX) and its known copies
```

| Document | Schema |
|---|---|
| `game.yaml` | `metadata/v1/game.schema.json` |
| `achievements.yaml` | `metadata/v1/achievements.schema.json` |
| `stats.yaml` | `metadata/v1/stats.schema.json` |
| `leaderboards.yaml` | `metadata/v1/leaderboards.schema.json` |
| `builds/*.yaml` | `metadata/v1/release.schema.json` |
| `runtimes/*.yaml` | `metadata/v1/runtime.schema.json` |

`metadata/v1/common.schema.json` holds the types the others share. Only `game.yaml` is required.

**YAML, read as JSON.** Recipes are written in YAML, for comments and for diffs people can read; the schemas
validate the parsed document. Only what JSON can say is read: `true`, `false`, `null`, plain decimal numbers, and
strings — `no` stays a word and `2013-01-14` stays text. So **quote every value that looks like a number but is
text**: store ids, hashes made only of digits, versions like `"1.0"`, `schemaVersion`. A key that appears twice in
one mapping is refused.

**DLC and editions live in `game.yaml`.** A DLC entry is a name, its store ids and what it requires — a few lines. A
recipe never says how a DLC gets into a copy or whether a build has it: that depends on where the copy came from
(Steam's depots, Epic's install tags, GOG's installers) and is the reader's own knowledge of an install.
Achievements, stats and leaderboards have files of their own because they run to hundreds of entries and change on
their own.

### The rules the format is built on

- **Every game, DLC and edition has its own id**, a UUID minted once and never changed: the anchor every store's
  ids map onto. Not a title (titles change) and not one store's id (an anchor built from one store makes every other
  store second-class). The folder is named by a readable `slug`, which the game repeats; renaming the folder is a
  move, and the id stays.
- **Store ids are how an item is found.** Every item lists its ids per store, as many as the store has (`stores`).
  A `(store, kind, id)` belongs to one item in the whole repository.
- **Only what Stoker cannot read from the files.** Stoker finds a build's store SDKs, its DRM wrapper, its anti-cheat
  and its engine in the files at ingest, so a recipe does not repeat them; it records what no file says — the
  interface versions a GOG port's Steam layer answers with (`steamworks`), how the game starts, what a version needs.
  The format carries no instruction to replace a store's file or remove a wrapper: what Stoker does about those is
  its own rule, not a recipe's.
- **Words and pictures are archived, not written here** (except achievements' names and descriptions), as a
  convenience. See "Presentation".
- **A build is a set of files.** The Steam depots, the GOG installer and a copy somebody had on disk are three builds
  of one release when their files differ, and one build with several `sources` when they are the same files. No two
  builds of a game share a fingerprint, a set of markers or a source.
- **The game's files are never here.** A build is described by a *fingerprint* of its files (`stoker-tree-1`), by
  marker files and by its store ids.
- **Values are names, not numbers.** Enums are camelCase strings.
- **Languages are BCP 47.** Text is a map of language tag to text, so translations are additions.
- **Dates are as precise as they are known** (`2017`, `2017-02`, `2017-02-24`).

### Store ids

```yaml
stores:
  ubisoft:
    ids:
      - { kind: productId, id: "22",  label: Standard edition }
      - { kind: productId, id: "71",  label: Steam edition }
      - { kind: productId, id: "543", label: Pre-order bonus item }
    installed: ["productId:22", "productId:71"]   # default: all
```

Kinds, per store (umu-database's store names, with `epic` for its `egs` and `eshop` for Nintendo's):

| Store | Reported — what a game's SDK asks about | Identity — what imports match and APIs address by |
|---|---|---|
| `steam` | `appId` | `packageId`, `depotId` |
| `gog` | `productId` | `galaxyClientId` |
| `epic` | `catalogItemId` (`EOS_Ecom_QueryOwnership`), `entitlementName` (`EOS_Ecom_QueryEntitlements`) | `appName`, `namespace` (shared by a game's items), `offerId` |
| `ubisoft` | `productId` | — |
| `eshop` | `titleId` | — |

**What an install reports as owned**, for a reader that answers a store SDK: the base game's reported ids for every
install of it; a DLC's for an install that carries it; an edition's for an install that carries every DLC it
includes. Per item and store, `installed` says whether that is **all** of the reported-kind ids or **only the listed
subset** — how an edition, bundle or reward id is kept for matching without being claimed. Identity kinds are never
reported. Other stores (`ea`, `amazon`, `battlenet`, `humble`, `itchio`, …) and kinds may be listed; the validator
warns that it does not know them, and readers ignore them.

**Editions** are the base game plus the DLC in `includes`. Matching a copy by an edition's id means the base game and
everything it includes. Content only one edition gets is a DLC that edition includes.

**DLC bundles** — a season pass, a collection — are DLC with `includes` of their own: owning one means owning the DLC
it includes, and its reported ids are reported by an install that carries every one of them.

### Saves

A save entry is a **glob that matches files**, starting at a placeholder: `*` and `?` match within one folder name, `**`
any number of folders, none included. `<winAppData>/Game/*.sav` is the folder's own `.sav` files; `<winAppData>/Game/**/*`
is everything in it and in every folder below. Matched case-insensitively. Entries carry `tags` (`save`, `config`),
`platforms` (absent: every system; never a store), `origin`, `enabled`, and their order is the order Stoker lists a
player's files in. `platforms` are the systems the build is for, not the machine it runs on: **a Windows build under
Wine or Proton on Linux reads its `windows` entries, with the `<win…>` placeholders inside its prefix**
(`drive_c/users/<user>/AppData/…`, Stoker's per-player prefix). A `<win…>` placeholder on any other system names no folder.

| Placeholder | Folder | Steam Auto-Cloud root |
|---|---|---|
| `<base>` | the install | `gameinstall` |
| `<winDocuments>` | Documents | `WinMyDocuments` |
| `<winAppData>` | AppData\Roaming | `WinAppDataRoaming` |
| `<winLocalAppData>` | AppData\Local | `WinAppDataLocal` |
| `<winLocalAppDataLow>` | AppData\LocalLow | `WinAppDataLocalLow` |
| `<winSavedGames>` | Saved Games (a known folder that can be moved) | `WinSavedGames` |
| `<home>` | the home folder | `LinuxHome`, `MacHome` |
| `<xdgData>`, `<xdgConfig>` | `$XDG_DATA_HOME`, `$XDG_CONFIG_HOME` | `LinuxXdgDataHome`, `LinuxXdgConfigHome` |
| `<macAppSupport>`, `<macDocuments>` | `~/Library/Application Support`, `~/Documents` | `MacAppSupport`, `MacDocuments` |
| `<ubisoftConnect>` | Ubisoft Connect's install folder, where some Ubisoft games write saves themselves | — |
| `<root>`, `<game>`, `<storeGameId>`, `<osUserName>`, `<winPublic>`, `<winProgramData>`, `<winDir>` | the store's library folder, the install folder's name, the store's id for the game, the user's name, and Windows' Public, ProgramData and Windows folders | — |

Inside a path, a store's account ids: `<steamId64>` (Steam's `{64BitSteamID}`), `<steamAccountId>` (`{Steam3AccountID}`),
`<ubisoftAccountId>` (`{UbisoftAccountID}`), and `<storeUserId>` for "any store's user id". A Stoker cloud-save rule
converts exactly — root, path, file pattern and recursive flag become the glob (recursive is `**/`), its platforms
`when.os`, its source `origin`, whether it is switched off `enabled`, its place in the list the entry's.

### Versions of a game

A **release** is a version (`builds/<name>.yaml`; `order`, higher is newer), a **build** one set of its files. A build
is its files — its fingerprint (`content`), else its `markers` — and never how or where a copy was obtained: the same
files from Steam, GOG, Epic or a disk are one build with one set of fixes. `sources` are optional store ids for those
files, a shortcut for a reader that has a store's metadata; no condition asks for a store. When two releases ship
exactly the same files, the build is defined once and the other release names it, `builds: [{ build: <id> }]`.

**Markers** are what a copy somebody changed is matched by: pick files that identify the version and that nothing
writes to — the main executable, and, where the executables do not change between versions (an engine's launcher), the
largest file that does, such as the main data archive. No two builds may have the same set.

**Language packs** are `languagePack` fixes with the `language` (BCP 47) Stoker picks them by from the player's
language, answering a build's `selectionGroups`. **Fixes,
plugins and hooks** sit on the game, a release or a build. Each is written once, at the widest level it holds for, and
narrowed by its `when`:

- `files`: files as stated, by hash or absent;
- `platforms`, `architectures`;
- `releases`: `{ from, before }` — a range by release id, compared by `order`, `before` not included — or `{ only }`.
  A release added later inside a range is covered.

**The combine rule**, one for fixes, plugins and hooks alike:

1. collect the build's, then the release's, then the game's;
2. drop those a level below switches off — fixes by `disableFixes`, hooks by `disableHooks`, a plugin by an assignment
   of the same plugin with `enabled: false` (an assignment of the same plugin lower down also replaces the one above,
   version and config included);
3. drop those whose `when` does not hold, and optional fixes nobody chose and answers to a `selectionGroup` nobody
   picked;
4. apply the rest build-level first, each fix's `when` checked against the files as they are when its turn comes,
   skipping a fix whose `appliedWhen` already holds.

A **plugin** (`plugins`) is a registry's plugin pinned by key and version, with its `config`: a hook plugin a loader
runs inside the game, a mod laid into it. A **hook** (`hooks`) is a declarative hook on a DLL's named export —
return a value instead, forward, or intercept and override its result, optionally rewrite one string argument
(`guards` check argument values first) — with an `id`, and `atLaunch` when it has to be in place before the game's
own code runs.

A copy that matches no build of the repository — modified, or an unknown patch — gets only the game's entries whose
`when` holds; their file checks are what make that safe. A build is matched by its fingerprint, then by its markers; `sources`, where given, let a reader with a store's metadata
match first (for Steam the depot manifests, never the build id alone).

### Steps, and what a reader may do on its own

A fix's steps change the install once (`files`, `download`, `delete`, `rename`, `iniSet`, `hexEdit`, `binaryPatch`,
`registry`, `firewall`, `run`, `runOnUninstall`), or apply every time the game starts (`environment`, `arguments`,
`windowsCompatibility`). `download` is how something published as a release — a mod, a community server — gets into an
install without the repository holding a byte of it: the publisher's file by URL and SHA-256, an archive's `from`
folder laid `into` the install, and the `result` fingerprint a reader checks afterwards. Each kind has a class,
`x-stokerhq-class` on its definition:

| Class | Kinds | Meaning |
|---|---|---|
| `unattended` | `files`, `delete`, `rename`, `iniSet`, `environment`, `arguments`, `windowsCompatibility` | a reader may apply it on its own |
| `optIn` | `download`, `hexEdit`, `binaryPatch`, `registry`, `firewall`, `run`, `runOnUninstall` | only with the player's consent for that fix: it brings somebody else's code into the install, runs a program, writes outside the install's files, or changes executable bytes |

What a step may do is limited by the schema, not by review alone:

- `run` and `firewall` name a program inside the install;
- `registry` writes below the game's own `Software\<vendor>` key, never under `Microsoft`, `Classes`, `Policies`,
  `RegisteredApplications` or `Clients`;
- `environment` never sets a variable that makes a runtime load code (`LD_PRELOAD`, `DYLD_*`, `COR_PROFILER`,
  `DOTNET_STARTUP_HOOKS`, `JAVA_TOOL_OPTIONS` and their kind);
- every path is inside the install.

A step read from the publisher's own install script (`source`) is exempt from the first two, because the publisher's
installer would have done the same. Code a fix needs goes into the install as files, named by hash. Values the
installing machine fills in may hold `%INSTALLDIR%`; `%%` is a literal `%`, and any other `%` is refused.

### Presentation

Descriptions, artwork, screenshots and achievement icons are **archived**, as a convenience, so they outlive the store pages they came
from, in a content-addressed archive kept apart from the recipes (`StokerHQ/Archive`): every file once, under its
SHA-256, with where it came from and when. A recipe's `presentation` block — on a game, a DLC or an edition — and an
achievement's `icon` refer to archived files by hash (`archiveRef`, `archiveImage`). A reader fetches a file from an
archive mirror and checks the hash, else from its `source.url`. Trailers are referenced, not archived: their source,
and the hash of each track as a reader stores it.

A long description is archived as its store publishes it — `text/html; charset=utf-8` for Steam's and GOG's — and
sanitised before it is shown; a short description is plain text. Achievements keep their names and descriptions in
the recipe, in every language known: short text a player sees beside an unlock. A store page's facts — developers,
publishers, release date — are in `storePage`.

### Licence

The recipes — everything in a recipes repository — and the archive's manifest of hashes and provenance are
**CC0-1.0**: anybody may use them for anything. The files in the archive are not ours to license: they remain their
publishers', kept for preservation, and the archive says how to ask for one to be removed. The schemas and
`validate.py` are MIT.

### Plugin registries — v1

A plugin is published in a **registry**: a git repository that is an index, never a file host. Recipes name a plugin
only by key and version (`pluginRef`), and a Stoker host subscribes to registries — the public StokerHQ one and any
private ones, all in this format — by their URL and key, and resolves those references against them.

```
registry.yaml                     who the registry is, its key, its publishers and their namespaces, its policy
plugins/
  <key>/
    plugin.yaml                   what the plugin is: kind, name, authors, licence, where its source is
    <version>.yaml                one immutable version: its release, its source commit, the publisher's signature,
                                  each file by URL and hash, the config contract, what it requires
index/v1/                         compiled from the above on every change, never edited (below)
```

| Document | Schema |
|---|---|
| `registry.yaml` | `plugins/v1/registry.schema.json` |
| `plugins/<key>/plugin.yaml` | `plugins/v1/plugin.schema.json` |
| `plugins/<key>/<version>.yaml` | `plugins/v1/version.schema.json` |
| `index/v1/plugins.json.zst`, `index/v1/plugins/<key>.json` | `plugins/v1/index.schema.json` (the second: its `$defs/pluginDocument`) |
| `index/v1/timestamp.json` | `plugins/v1/timestamp.schema.json` |
| `stoker-plugin.yaml` in a plugin's own repository | `plugins/v1/plugin-project.schema.json` — which plugin it builds, for which targets, against which release of the plugin SDK (StokerHQ/plugin-sdk), pinned by version and SHA-256 |

**Kinds**, by where the code runs — which is what decides how much trust it takes: `hook` (native code a bootstrap loads
inside a game), `mod` (files laid into a game for a mod loader, the loader included; says its `loader`), `integration`
(a store SDK's replacement inside a game, laid over the store's own file; says its `provider` and what it `replaces`;
one zip per architecture holding the bootstrap and its implementation),
`server` (an extension stokerd loads, with stokerd's rights; says which `stokerd` and extensibility API it needs),
`unpacker` (an assembly stokerd's ingest runs in a process of its own) and `library` (loaded by other plugins, never run
on its own). Hooks and mods are assigned in a recipe's `plugins`; the others are not.

**The files are release assets on the plugin's own repository.** A version names its `release` — repository and tag —
and each file by `url`, size, SHA-256 and BLAKE3. A repository holding one plugin tags releases `v<version>` (`v1.2.0`);
one holding several tags them `<key>/<version>` (`app.stokerhq.peacock/1.2.0`), so a release always says which plugin it
is. `source` names the exact commit the files were built from. The hashes decide what a file is: when a release asset is
gone, a host tries the registry's `mirrors` by SHA-256 (`<mirror>/<sha256[0:2]>/<sha256>`), and an administrator can
still supply a file by hand.

**Keys and namespaces.** A key is reverse-DNS; Stoker's own plugins are `app.stokerhq.*`. `registry.yaml` lists each
publisher with its public key and the prefixes it owns (`app.stokerhq.`), and a prefix belongs to one publisher. A
version is signed by a publisher who owns its key's prefix, or the registry refuses it.

**The signature.** The publisher signs each version: ECDSA P-256 with SHA-256, IEEE P1363 (r and s, 32 bytes each),
base64 — the scheme Stoker's launcher releases are signed with. Public keys are written as base64 of their DER
SubjectPublicKeyInfo (the body of a PEM `PUBLIC KEY` block, on one line); a key's id is the first 8 bytes of the
SHA-256 of those DER bytes, in lowercase hex. What is signed is this statement, UTF-8, every line ending in `\n`:

```
stoker-plugin-v1
<key>
<version>
<kind>
<os>\t<architecture>\t<fileName>\t<size>\t<sha256>\t<blake3>      one line per file, ordered ordinally by
...                                                               <os>\t<architecture>\t<fileName>
```

Sizes are decimal, hashes lowercase hex. Where a file is kept is not signed, so a moved release or a mirror still
verifies.

**The compiled index** is what a host reads: `index/v1/plugins.json.zst` (the publishers, the policy and every plugin
with every version, as JSON, zstd-compressed), one `index/v1/plugins/<key>.json` per plugin, and
`index/v1/timestamp.json`, which pins both by SHA-256 and size and carries a `serial` and an `expires`.
`timestamp.json.sig` is the registry key's signature over the timestamp's exact bytes, base64, the same algorithm.
A host refuses a timestamp with a lower serial than the one it holds, or one past its `expires`, and keeps what it
has.

**Policy.** `registry.yaml`'s `policy` says what the registry refuses whoever signed it: kinds (`refusedKinds`) and
integration providers (`refusedIntegrationProviders`). The public StokerHQ registry refuses `unpacker` and the
integrations that would answer a store's ownership check; a private registry may take them.

**A host's administrator approves every plugin version once** before it runs anywhere on that host. A registry makes a
plugin findable and checkable; it never installs one on its own. A version is never deleted: `retired` (with
`retiredReason`) withdraws it from new choices, and what is pinned to it keeps working.

The example registry is `plugins/examples/`. Its keys are example keys whose private halves were discarded, and its
signatures are real ones over each version's statement.
### Versions

Every document carries `schemaVersion`, `"<major>.<minor>"`. Once v1 is published, the schemas under `metadata/v1/`
are always the newest minor of major 1, and a minor version only **adds**: fields, enum values, kinds. It never
changes what an existing field means or makes an optional one required.

A reader:

- refuses a major it does not know;
- reads a newer minor of a major it knows, and **ignores fields it does not know**;
- treats a value it does not know in a field that only describes — a provider, a tag kind, a store or a store id kind —
  as absent, and an entry for a platform it does not know as not for this host;
- never silently skips something it does not know that would change what is done to an install — a fix role, a step
  kind. It treats that build as needing a newer reader instead, because a skipped fix is a broken install reported as
  a working one.

The schemas themselves stay strict (`additionalProperties: false`), so this repository's checks catch a misspelt
field; it is readers that are lenient, not the schemas.

### Checks beyond the schemas

`validate.py` runs every document against its schema, then checks what a schema cannot see:

- a game folder holds only the files and folders a game has, is named by the game's `slug`, and every document is
  YAML read as JSON (above) with no key twice in one mapping;
- no id is used twice anywhere in the repository, and every document of a game names that game;
- a store id has the shape of its kind, is a string, and belongs to one item across the repository (except kinds
  several items share); `installed` names only ids the item has, of a reported kind;
- an edition includes DLC of its game; a DLC's requirements and a bundle's `includes` are other DLC of the same game,
  never itself and never in a circle;
- a build a release names by id is defined by another release of the game, and named once;
- a release range names releases of the game, and runs forwards;
- release names and orders are unique within a game, and no two builds share a source (for Steam, a depot set), a
  fingerprint or a set of markers;
- a build's launches, compatibility reports and fixes are for platforms it runs on, it has one launch per key and
  platform and at most one default launch per platform, its fixes answer groups it declares, a `languagePack` says its
  language, its steps come from install scripts it carries, and a `steamworks` site's interface versions are read from
  a build the game has;
- a step's `launch` is a key a build it applies to has, `disableFixes` names fixes above it, and a `hexEdit` edits a
  file its fix's `when` pins by hash and writes as many bytes as it replaces;
- a save path uses only known placeholders, a partial date is a real day, and no achievement name or alias, stat or
  leaderboard name, or tag slug is used twice;
- every step kind has a class, every hook id is unique, and `disableHooks` names hooks above it;
- every runtime a build needs is in `runtimes/`, and no installer belongs to two runtimes;
- every plugin named is in the registry at that version, of a kind that can be used there, configured as its
  contract says;
- in a registry: a plugin's folder is its key and a version's file its version, a release's tag is `v<version>` or
  `<key>/<version>`, the signer is a publisher in `registry.yaml` who owns the key's prefix, no prefix lies inside
  another publisher's, the policy takes the version's kind and provider, a version has one file per system and
  architecture, and what it requires is in the registry. Signatures are not verified here: that needs the keys and the
  files, and is the signing tool's job.

```
python -I validate.py <recipes repository root> [--plugins <plugin registry root>]
python -I validate.py <plugin registry root>
python -I validate.py metadata/examples --plugins plugins/examples
python -I validate.py plugins/examples
```

Needs `jsonschema[format-nongpl]` 4.18 or newer and PyYAML. Without the `format-nongpl` extra jsonschema passes every
URI unchecked, so the validator refuses to run without it.

### Not decided yet

- **The compiled index** recipes are read from: one signed, compressed file per major, and a small file per store id
  (`by-store/<store>/<kind>/<id>.json`) so *I have game Y from store X* is one request.
- Where fix content (`files`, `binaryPatch`) comes from when a reader does not hold it and no `download` step says.
- **Emulator settings** for console builds, and **age ratings**.
