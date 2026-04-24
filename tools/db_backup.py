"""Nightly backup of trading-agent state.

Runs PRAGMA integrity_check on each SQLite DB, aborts the backup if any
DB is unhealthy (so we never upload a corrupt copy). Otherwise tars the
databases + memory files into ~/.agent-backups/, rsyncs to a remote,
and rotates.

Configurable via env:
    DB_BACKUP_DIR          local backup dir (default ~/.agent-backups)
    DB_BACKUP_REMOTE       remote SSH host (default admin@minions.protoscience.org)
    DB_BACKUP_REMOTE_DIR   remote path (default ~/agent-backups)
    DB_BACKUP_KEEP         how many local backups to retain (default 14)
"""
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO / "logs"

BACKUP_DIR = Path(os.environ.get("DB_BACKUP_DIR", Path.home() / ".agent-backups"))
REMOTE_HOST = os.environ.get("DB_BACKUP_REMOTE", "admin@minions.protoscience.org")
REMOTE_DIR = os.environ.get("DB_BACKUP_REMOTE_DIR", "~/agent-backups")
KEEP = int(os.environ.get("DB_BACKUP_KEEP", "14"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("db-backup")


def integrity_check(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            ok = row is not None and row[0] == "ok"
            if not ok:
                log.error(f"integrity_check {db_path.name}: {row}")
            return ok
        finally:
            conn.close()
    except Exception as e:
        log.exception(f"integrity_check {db_path.name} raised: {e}")
        return False


def run() -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    dbs = sorted(LOGS_DIR.glob("*.db"))
    if not dbs:
        log.warning(f"no .db files under {LOGS_DIR}; nothing to back up")
    else:
        for db in dbs:
            if not integrity_check(db):
                log.error("aborting backup — corrupt DB detected; existing backups preserved")
                return 2
        log.info(f"integrity: {len(dbs)} DB(s) ok")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"agent-backup-{stamp}.tar.gz"

    # Tar only targets that exist; skip gracefully otherwise.
    targets = []
    for p in [LOGS_DIR / "cost.db", LOGS_DIR / "trades.db", LOGS_DIR / "memory"]:
        if p.exists():
            targets.append(p.relative_to(REPO).as_posix())
    if not targets:
        log.warning("nothing to archive (no cost.db, trades.db, or memory/)")
        return 0

    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(REPO), *targets],
        check=True,
    )
    size_mb = archive.stat().st_size / (1024 * 1024)
    log.info(f"wrote {archive.name} ({size_mb:.1f} MB)")

    # rsync to remote — best effort, don't fail the whole backup if offline
    try:
        subprocess.run(
            [
                "rsync", "-az",
                "--timeout=30",
                str(archive),
                f"{REMOTE_HOST}:{REMOTE_DIR}/",
            ],
            check=True,
            timeout=120,
        )
        log.info(f"uploaded to {REMOTE_HOST}:{REMOTE_DIR}/")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning(f"rsync failed (local backup is safe): {e}")

    # Rotate: keep most-recent KEEP
    archives = sorted(BACKUP_DIR.glob("agent-backup-*.tar.gz"), reverse=True)
    for old in archives[KEEP:]:
        log.info(f"rotating out {old.name}")
        old.unlink()

    return 0


if __name__ == "__main__":
    sys.exit(run())
