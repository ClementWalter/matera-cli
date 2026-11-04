# matera-cli

**Visibility: public.**

Also a Claude Desktop extension: download `matera-cli.mcpb` from the
[latest release](https://github.com/ClementWalter/matera-cli/releases/latest)
or from <https://clementwalter.github.io/claude-extensions/#matera>.

Read your [Matera](https://matera.eu) copropriété account from the terminal:
documents and folders, general assemblies, accounting years, the general ledger,
and a one-command mirror of every document into a local folder.

Matera has no public API. This tool uses the private JSON API behind
app.matera.eu, authenticated with the access token the web app keeps in your
browser. It never stores or refreshes credentials, so it cannot sign your
browser out.

## Install

Requires macOS, Python 3.11+, `uv`, and Chrome (or Arc, Brave, Edge) signed in to
https://app.matera.eu.

```console
uv run claudine/scripts/link_clis.py   # links bin/matera into ~/.local/bin
matera connect
```

`bin/matera` is a symlink to `matera_cli.py`, a PEP 723 script with pinned
dependencies.

## Usage

```console
matera whoami
matera documents --folder 'Mes assemblées'
matera download 7521821 --target-dir ~/Downloads/matera
matera ledger 2025
matera sync --output ~/Matera
```

If a command says no live token was found, open or reload app.matera.eu in the
browser and run it again: the web app renews the token every 15 minutes.

See `SKILL.md` for every command, recipe and known trap.

## Tests

```console
uv run --script tests/test_matera_cli.py
```

The tests are offline and use synthetic data.

## Privacy Policy

See https://clementwalter.github.io/claude-extensions/privacy/. The tool talks
only to api-core.matera.eu and the storage it redirects to, and writes only the
files you ask it to download.
