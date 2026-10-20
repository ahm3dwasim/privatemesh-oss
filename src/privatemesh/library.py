"""The skill library: what the owner has taught, and what may run again.

This is the compounding artifact the direction asks for. A learned capability is not a transcript
that scrolls away — it is a row: the exact code, the exact manifest, the exact tests, and the
identity those three hash to. A LATER run, in a LATER process, gets the same bytes back.

Two rules carry the whole safety story, and both are enforced here rather than trusted:

* **Identity is the hash of (code, manifest, tests, held-out tests).** `install` recomputes it from
  the bytes it is about to store and refuses a mismatch; `load` recomputes it again on the way out
  and refuses a mismatch. So a row edited by hand, by a migration, or by a skill that reached the
  database is a row the library will not hand back — the approval binds to the bytes, and the
  bytes are checked every time they are read.
* **Nothing changes under an existing identity.** One identity, one set of bytes, forever. A new
  version of a skill is a new identity and therefore a new approval; installing it revokes the
  identity it replaces, so a name never has two live versions and a stale approval never quietly
  starts binding to new code.

`state` has exactly the two values the contract names — `active` and `revoked`. A superseded
version is revoked too: what the owner granted was an identity, and once it is not the one in use
it must not be usable. The library keeps the row, because the approval record is part of the
history the owner is entitled to read.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from pathlib import Path

from .skillrt.manifest import SkillManifest

ACTIVE, REVOKED = "active", "revoked"


class LibraryError(RuntimeError):
    """A skill could not be stored, or is not what it claims to be on the way out."""


class SkillLibrary:
    """SQLite, one row per (name, identity). The owner's taught capabilities."""

    def __init__(self, path: str | Path = ":memory:", *, now=None) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS skills("
            " identity TEXT PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " version TEXT NOT NULL,"
            " purpose_plain TEXT NOT NULL DEFAULT '',"
            " code TEXT NOT NULL,"
            " tests TEXT NOT NULL DEFAULT '',"
            " held_out TEXT NOT NULL DEFAULT '',"
            " manifest TEXT NOT NULL,"
            " state TEXT NOT NULL DEFAULT 'active',"
            " learned_at TEXT NOT NULL,"
            " last_used_at TEXT NOT NULL DEFAULT '',"
            " uses INTEGER NOT NULL DEFAULT 0,"
            # Teach Mode: the owner's sentence, for a skill they taught. '' for a skill the build
            # knew how to ask for. Nullable in the API (`str | null`), and stored as '' here because
            # adding a column to a live table cannot carry a NOT NULL DEFAULT of NULL.
            " rule TEXT NOT NULL DEFAULT '')")
        self._conn.execute("CREATE INDEX IF NOT EXISTS skills_name ON skills(name)")
        self._migrate()

    def _migrate(self) -> None:
        """Bring an existing library file up to the current columns.

        A data directory outlives a build: the sample's whole claim is that a skill learned once is
        still there next time, so a new column has to arrive without the owner losing the library
        they already approved. `CREATE TABLE IF NOT EXISTS` above does nothing to a table that
        exists, which is exactly the case that needs this.
        """
        columns = {row["name"] for row in
                   self._conn.execute("PRAGMA table_info(skills)").fetchall()}
        if "rule" not in columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN rule TEXT NOT NULL DEFAULT ''")

    # ---- writing ------------------------------------------------------------------------------
    def install(self, *, manifest: SkillManifest, code: str, tests: str = "",
                held_out: str = "", purpose_plain: str = "") -> dict:
        """Store exactly this artifact under the identity it hashes to. Idempotent, never lenient.

        The identity is RECOMPUTED here rather than accepted from the caller. The owner approves a
        hash; if the bytes do not hash to it, storing the row would mean the next run executed
        something the owner never saw."""
        identity = manifest.identity(code, tests, held_out=held_out)
        learned_at = self._clock().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM skills WHERE identity=?", (identity,)).fetchone()
                if existing is not None:
                    self._same_bytes(existing, manifest, code, tests, held_out)
                    self._conn.execute("COMMIT")
                    return dict(existing)
                # A new identity for a name replacing an older one: the older one is revoked in
                # the same transaction, so there is never a window with two live versions.
                self._conn.execute(
                    "UPDATE skills SET state=? WHERE name=? AND state=?", (REVOKED, manifest.name,
                                                                           ACTIVE))
                # The column is a COPY of `manifest.rule`, never a second source: the manifest is
                # inside the identity hash, so it is the one a check can be made against, and a
                # caller that could pass a rule disagreeing with the hashed one could describe the
                # artifact as something the owner never approved.
                self._conn.execute(
                    "INSERT INTO skills(identity, name, version, purpose_plain, code, tests,"
                    " held_out, manifest, state, learned_at, last_used_at, uses, rule)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,'',0,?)",
                    (identity, manifest.name, manifest.version, purpose_plain, code, tests,
                     held_out, manifest.to_json(), ACTIVE, learned_at, manifest.rule))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self.get(identity)

    def _same_bytes(self, row: sqlite3.Row, manifest: SkillManifest, code: str, tests: str,
                    held_out: str) -> None:
        """A backstop, not the primary defence. `install` recomputes the identity from the very
        bytes it is about to store, so two different artifacts landing on one hash needs a sha256
        collision or a row written by something other than `install`. The check is kept because it
        is three lines and it is the difference between refusing and silently overwriting, but the
        real guarantee is the recomputation above it."""
        if (row["code"], row["tests"], row["held_out"], row["manifest"]) != (
                code, tests, held_out, manifest.to_json()):
            raise LibraryError(
                f"identity {row['identity']} already names a different artifact; an identity is "
                f"one set of bytes and a new artifact needs a new approval")

    def revoke(self, name: str) -> bool:
        """Disable every live version of `name`. Future plans cannot use it."""
        with self._lock:
            cur = self._conn.execute("UPDATE skills SET state=? WHERE name=? AND state=?",
                                     (REVOKED, name, ACTIVE))
            return cur.rowcount > 0

    def record_use(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET uses = uses + 1, last_used_at=? WHERE name=? AND state=?",
                (self._clock().isoformat(timespec="seconds"), name, ACTIVE))

    # ---- reading ------------------------------------------------------------------------------
    def get(self, identity: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM skills WHERE identity=?", (identity,)).fetchone()
        return dict(row) if row is not None else None

    def active(self, name: str) -> dict | None:
        """The one live version of `name`, or None. There is at most one, by construction."""
        row = self._conn.execute("SELECT * FROM skills WHERE name=? AND state=?",
                                 (name, ACTIVE)).fetchone()
        return dict(row) if row is not None else None

    def serves(self, capability: str) -> bool:
        """Can a change needing `capability` be served without learning anything?"""
        return self.active(capability) is not None

    def taught(self) -> list[dict]:
        """Every live TAUGHT skill, oldest first.

        Ordered by `learned_at` so the intersection several taught skills form is a property of the
        rules and not of whichever row SQLite happened to return first — an owner who teaches the
        same two rules in the other order gets the same plan.
        """
        rows = self._conn.execute(
            "SELECT * FROM skills WHERE state=? AND rule <> '' ORDER BY learned_at, name",
            (ACTIVE,)).fetchall()
        return [dict(r) for r in rows]

    def _by_id_or_name(self, key: str) -> dict | None:
        """The row an identity or a name points at, revoked or not, newest first.

        `load` needs this so a revoked skill is reported as REVOKED rather than as missing. The
        owner withdrew an approval; "there is no such skill" is a different answer, and it is the
        one that would send them looking for a typo instead of at the thing they revoked."""
        row = self._conn.execute("SELECT * FROM skills WHERE identity=?", (key,)).fetchone()
        if row is not None:
            return dict(row)
        row = self._conn.execute(
            "SELECT * FROM skills WHERE name=? ORDER BY learned_at DESC, rowid DESC LIMIT 1",
            (key,)).fetchone()
        return dict(row) if row is not None else None

    def summaries(self) -> list[dict]:
        """`GET /v2/skills`. Newest first, every version, revoked ones included — the owner's
        library is a record, and hiding the revoked entries would hide the approvals that were
        withdrawn."""
        rows = self._conn.execute(
            "SELECT * FROM skills ORDER BY learned_at DESC, name").fetchall()
        return [self._summary(r) for r in rows]

    @staticmethod
    def _summary(row) -> dict:
        return {"name": row["name"], "purpose_plain": row["purpose_plain"],
                "version": row["version"], "identity": row["identity"], "state": row["state"],
                "learned_at": row["learned_at"], "last_used_at": row["last_used_at"],
                "uses": int(row["uses"]),
                # The owner's sentence, for a skill they taught; null for one the build knew how to
                # ask for. `str | None` rather than `""` because an empty rule and no rule are
                # different answers on a library screen.
                "rule": (row["rule"] or None) if "rule" in row.keys() else None}

    # ---- getting the bytes back out to run them -----------------------------------------------
    def load(self, name_or_identity: str) -> tuple[SkillManifest, str, str, str, dict]:
        """Re-derive the identity from the STORED bytes and refuse anything that does not match.

        `run_harness` computed this hash from the files on disk at learning time; recomputing it
        here from what the database kept is what makes "the thing that runs is the thing that was
        approved" a check rather than a hope."""
        row = self.active(name_or_identity) or self._by_id_or_name(name_or_identity)
        if row is None:
            raise LibraryError(f"no skill '{name_or_identity}' in the library")
        if row["state"] != ACTIVE:
            raise LibraryError(f"skill '{row['name']}' is revoked")
        manifest = self.manifest_of(row)
        expected = manifest.identity(row["code"], row["tests"], held_out=row["held_out"])
        if expected != row["identity"]:
            raise LibraryError(
                f"skill '{row['name']}' does not hash to its stored identity "
                f"({expected} != {row['identity']}); the bytes changed after approval")
        return manifest, row["code"], row["tests"], row["held_out"], dict(row)

    def manifest_of(self, row: dict) -> SkillManifest:
        return SkillManifest(**json.loads(row["manifest"]))

    def materialize(self, row: dict, directory: str | Path) -> Path:
        """Write the skill where a runner can import it, and return its directory.

        Only the module is written. The tests are NOT unpacked into the skill's own tree: the
        held-out set is the thing that grades it, and unpacking the coder's tests next to the code
        would put them inside the surface the code can rewrite."""
        manifest = self.manifest_of(row)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{manifest.module_name}.py").write_text(row["code"], encoding="utf-8")
        return directory
