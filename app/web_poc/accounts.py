"""Google-only web accounts, revocable cookie sessions and persistent non-secret app data."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from app.modules.auth.google import GoogleIdentity
from app.web_poc.runtime import Workspace


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class WebSettings:
    public_origin: str = ""
    base_path: str = ""
    google_client_id: str = ""
    database_path: str = ":memory:"
    static_dir: str = ""
    google_hub: bool = False

    @property
    def hosted(self) -> bool:
        return bool(self.public_origin)

    @property
    def cookie_path(self) -> str:
        return self.base_path + "/"

    def validate(self) -> None:
        if self.hosted:
            parsed = urlsplit(self.public_origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("MIA_PUBLIC_ORIGIN must be an HTTPS origin")
            if (
                not self.google_client_id and not self.google_hub
            ) or self.database_path == ":memory:":
                raise ValueError("Hosted MIA requires Google client ID and persistent database")
        if self.base_path and (
            not self.base_path.startswith("/")
            or self.base_path.endswith("/")
            or ".." in self.base_path
        ):
            raise ValueError("Invalid MIA_BASE_PATH")

    @classmethod
    def environment(cls) -> "WebSettings":
        # Process environment only; never load credential files.
        value = cls(
            public_origin=os.environ.get("MIA_PUBLIC_ORIGIN", "").rstrip("/"),
            base_path=os.environ.get("MIA_BASE_PATH", "").rstrip("/"),
            google_client_id=os.environ.get("MIA_GOOGLE_CLIENT_ID", ""),
            database_path=os.environ.get("MIA_DATABASE_PATH", ":memory:"),
            static_dir=os.environ.get("MIA_STATIC_DIR", ""),
            google_hub=os.environ.get("MIA_GOOGLE_HUB") == "1",
        )
        value.validate()
        return value


class AccountStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS web_users (
            id TEXT PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, email TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
            expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS web_challenges (
            cookie_hash TEXT PRIMARY KEY, nonce TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS web_workspaces (
            user_id TEXT PRIMARY KEY REFERENCES web_users(id), document TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS web_reviews (
            plugin_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES web_users(id),
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            text TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(plugin_id,user_id));
        """)

    def close(self) -> None:
        self.db.close()

    def challenge(self) -> tuple[str, str]:
        self.db.execute("DELETE FROM web_challenges WHERE expires <= ?", (time.time(),))
        count = self.db.execute("SELECT count(*) FROM web_challenges").fetchone()[0]
        if count >= 500:
            raise ValueError("Too many sign-in attempts")
        cookie, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO web_challenges VALUES (?,?,?)", (digest(cookie), nonce, time.time() + 300)
        )
        self.db.commit()
        return cookie, nonce

    def consume_challenge(self, cookie: str) -> str | None:
        row = self.db.execute(
            "SELECT nonce FROM web_challenges WHERE cookie_hash=? AND expires>?",
            (digest(cookie), time.time()),
        ).fetchone()
        self.db.execute("DELETE FROM web_challenges WHERE cookie_hash=?", (digest(cookie),))
        self.db.commit()
        return str(row["nonce"]) if row else None

    def login(self, identity: GoogleIdentity) -> tuple[str, dict[str, str]]:
        if not identity.email_verified:
            raise ValueError("Verified Google email required")
        with self.db:
            self.db.execute(
                """INSERT INTO web_users VALUES (?,?,?,?)
                ON CONFLICT(google_sub) DO UPDATE SET name=excluded.name,email=excluded.email""",
                (
                    str(uuid4()),
                    identity.subject,
                    identity.display_name or "Пользователь Мии",
                    identity.email,
                ),
            )
            user = dict(
                self.db.execute(
                    "SELECT id,name,email FROM web_users WHERE google_sub=?", (identity.subject,)
                ).fetchone()
            )
            token = secrets.token_urlsafe(40)
            self.db.execute("DELETE FROM web_sessions WHERE expires<=?", (time.time(),))
            self.db.execute(
                "DELETE FROM web_sessions WHERE user_id=? AND token_hash NOT IN "
                "(SELECT token_hash FROM web_sessions WHERE user_id=? "
                "ORDER BY expires DESC LIMIT 31)",
                (user["id"], user["id"]),
            )
            self.db.execute(
                "INSERT INTO web_sessions VALUES (?,?,?)",
                (digest(token), user["id"], time.time() + 30 * 86400),
            )
        return token, user

    def user(self, token: str) -> dict[str, str] | None:
        row = self.db.execute(
            """SELECT u.id,u.name,u.email FROM web_sessions s
            JOIN web_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?""",
            (digest(token), time.time()),
        ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM web_sessions WHERE token_hash=?", (digest(token),))
        self.db.commit()

    def save(self, workspace: Workspace) -> None:
        # Explicit allowlist: provider keys/connections and tokens never reach this document.
        document = json.dumps(
            {
                "installed": workspace.installed,
                "states": workspace.states,
                "layout": workspace.layout,
                "revision": workspace.revision,
                "audit": workspace.audit[-100:],
            },
            ensure_ascii=False,
        )
        self.db.execute(
            "INSERT INTO web_workspaces VALUES (?,?) ON CONFLICT(user_id) "
            "DO UPDATE SET document=excluded.document",
            (workspace.subject_id, document),
        )
        self.db.commit()

    def restore(self, user_id: str) -> Workspace:
        workspace = Workspace(expires=time.monotonic() + 8 * 3600, subject_id=user_id)
        row = self.db.execute(
            "SELECT document FROM web_workspaces WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            value = json.loads(row["document"])
            workspace.installed = value["installed"]
            workspace.states = value["states"]
            workspace.layout = value["layout"]
            workspace.revision = value["revision"]
            workspace.audit = value.get("audit", [])
        return workspace

    def reviews(self, plugin_id: str, viewer: str | None) -> dict[str, Any]:
        rows = self.db.execute(
            """SELECT u.name,r.rating,r.text,r.updated,r.user_id
            FROM web_reviews r JOIN web_users u ON r.user_id=u.id WHERE plugin_id=?
            ORDER BY r.updated DESC LIMIT 100""",
            (plugin_id,),
        ).fetchall()
        summary = self.db.execute(
            "SELECT count(*),avg(rating) FROM web_reviews WHERE plugin_id=?", (plugin_id,)
        ).fetchone()
        return {
            "count": summary[0],
            "average": summary[1],
            "items": [
                {
                    "author": r["name"],
                    "rating": r["rating"],
                    "text": r["text"],
                    "updated_at": r["updated"] * 1000,
                    "mine": r["user_id"] == viewer,
                }
                for r in rows
            ],
        }

    def review(self, plugin_id: str, user_id: str, rating: int, text: str) -> None:
        self.db.execute(
            """INSERT INTO web_reviews VALUES (?,?,?,?,?)
            ON CONFLICT(plugin_id,user_id) DO UPDATE SET rating=excluded.rating,
            text=excluded.text,updated=excluded.updated""",
            (plugin_id, user_id, rating, text, time.time()),
        )
        self.db.commit()

    def remove_review(self, plugin_id: str, user_id: str) -> None:
        self.db.execute(
            "DELETE FROM web_reviews WHERE plugin_id=? AND user_id=?", (plugin_id, user_id)
        )
        self.db.commit()
