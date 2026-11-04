---
name: matera-cli
description: Read a Matera (syndic de copropriété, app.matera.eu) account from the terminal with the `matera` command — identity and role, document folders, every document the account can see (council members see far more than plain co-owners), general assemblies, accounting years, the general ledger PDF, and a one-shot mirror of all documents into a local folder tree. Read-only; borrows the signed-in browser's access token. Use when the user asks for their copropriété documents, AG convocations or minutes (PV), appels de fonds, factures, devis, the grand livre, or to "download everything from Matera". Triggers - "matera", "syndic matera", "télécharge les docs de la copro", "PV d'AG", "appels de fonds", "grand livre copro".
---

# matera-cli

Read-only access to a Matera copropriété account through Matera's private API.

## How to invoke

`matera` is on `$PATH` via `uv run claudine/scripts/link_clis.py`, which links
`bin/matera` into `~/.local/bin`. If it is missing, run `<skill-dir>/bin/matera`
directly: it is a PEP 723 script and `uv` resolves its pinned dependencies.

## Authentication

There is no stored credential. Each run reads the current **access token** from
the IndexedDB of a signed-in Chromium browser (Chrome, Arc, Brave, Edge). The
Matera web app renews that token itself; it lives 15 minutes.

- Start with `matera connect --json`: it reports the person and when the
  borrowed token expires.
- On `No live Matera token` or `401`, ask the user to open or reload
  https://app.matera.eu in Chrome while signed in, then retry. Do not loop.
- `MATERA_ACCESS_TOKEN` overrides the browser lookup.
- Never call `/api/v1/auth/refresh_token` with the browser's refresh token:
  refresh tokens are single-use, and consuming it signs the browser out.

## Output conventions

- Default: one line per record, tab-separated, id first.
- `--json`: normalized records; data only on stdout. Logs and progress go to
  stderr (`--debug` for more).
- File URLs (capability links to pre-signed S3) are never printed; use the
  document id with `download`.

## Commands

```bash
# Read
matera connect --json                       # is a live browser token available? who is it?
matera whoami --json                        # person, role (owner/council), building id
matera folders                              # folder tree with document counts
matera documents --folder 'Mes assemblées'  # filters are client-side, case-insensitive
matera documents --match 'PV AG' --json
matera documents --kind assembly_minutes --since 2025-01-01
matera documents --owners-only              # only what every co-owner can see
matera assemblies                           # AG ids, dates, status
matera exercices                            # accounting years and approval status

# Local writes (downloads)
matera download 7521821 7984796 --target-dir ~/Downloads/matera
matera download 7521821 -o pv.pdf --json    # {"id", "path"}
matera ledger 2025 -o grand-livre-2025.pdf  # general ledger PDF of a year
matera sync --output DIR --dry-run          # what a mirror would fetch
matera sync --output DIR                    # mirror everything, idempotent
matera sync --output DIR --folder 'Mes factures'
```

## Recipes

- **Everything into a Drive folder:** `matera sync --output "<folder>"`. It
  keeps Matera's folder tree, writes `.matera-manifest.json` (id → path,
  revision) and skips what is already there. Re-run any time to pick up new
  documents.
- **Latest AG minutes:** `matera documents --kind assembly_minutes --json`,
  then `matera download <id>`.
- **Documents for the next AG:** `matera documents --folder 'Mes assemblées'`.
- **Big listings:** `matera documents --json > docs.json` and filter with `jq`
  rather than reading the whole list in context.

## Gotchas

- Measured (2026-11, one building): 1,069 documents and 92 folders come back in a
  single page each (~2 s and ~0.2 s); a full sync of ~600 MB took 30 s with 6
  workers. `results()` fails loudly if a list ever reports `has_next_page`.
- Downloads need no token: blob URLs redirect to pre-signed S3. Only listing
  does, so a sync started with a valid token finishes even if it expires.
- Some folders referenced by documents are hidden from the folder list (403
  on direct read); their files land under `_dossier-<id>/`. Documents with no
  folder land under `_sans-dossier/`.
- Matera's declared file size is sometimes off by a few hundred bytes; the
  manifest's revision check keeps `sync` from re-downloading those forever.
- Names are written in Unicode NFC. Matera returns some names in NFD; on a
  Google Drive mount, NFC and NFD spellings are different files, so mixing them
  creates duplicates. Do not prune a synced folder by comparing raw `os.walk`
  names with the manifest: normalize both sides to NFC first.
- Names without an extension get one from the MIME type (Drive would add it
  anyway, which would break the size check).
- The account sees one building: the one in its token. Multi-building accounts
  are not handled.
