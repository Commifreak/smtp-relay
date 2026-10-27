# smtp-relay – project conventions

## Language

Everything in the repository is English: code, comments, UI texts, log output, README, commit messages and
pull requests.

## Database migrations

Every change that existing installations need applied to their database (new or changed columns, tables,
indexes, moved or converted data, renamed settings) is a **numbered migration** in `app/migrations.py`:

- Append a `Migration(<next id>, "<short description> (<version>)", <function>)` to `MIGRATIONS`. Never
  change, reorder or remove released migrations.
- The function must be **idempotent** (a database may already contain the change, e.g. from an older
  ad-hoc upgrade) and return a short result text such as `"added 2 column(s)"` or `"already up to date"`.
- It runs inside a transaction; raise an exception to roll it back. The container then refuses to start.
- Do not alter tables in `Database.__init__` or anywhere else at startup. `SCHEMA` in `app/db.py` only
  holds the base tables; new tables may be added there with `CREATE TABLE IF NOT EXISTS`, but changes to
  existing tables belong in a migration.
- On container start, pending migrations are announced and reported in a block on stdout (with a backup in
  `/data/backups/` beforehand). Keep it that way for every migration; new databases are migrated quietly.
- Add a test in `tests/test_db.py` that runs the migration against a database in the old state.

## Releases

- Bump `__version__` in `app/__init__.py` with every user-visible change (semver; breaking changes to
  configuration or behaviour need a new major version).
- Release images are built by tagging the merge commit on `main` with `v<version>`; tags cannot be pushed
  from Claude Code sessions, the maintainer sets them.
- `docker-compose.yml` pins the major version tag (`:1`), so existing deployments never jump to a new major
  version.

## Tests and screenshots

- Run `pytest -q` before every commit.
- When the UI changes visibly, update the screenshots in `docs/screenshots/` (demo data with `example.com`
  addresses only, light mode, 1280 px wide).
