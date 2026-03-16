"""
GitHub Actions Dashboard
========================
A self-hosted Flask app that aggregates GitHub Actions workflow runs
across multiple repositories into a single, chronological view.

Users create their own GitHub App, configure it here, and select
which repos to monitor via GitHub's native installation flow.
"""

import os
import time
import json
import secrets
import sqlite3
import logging
from datetime import datetime, timezone
from functools import wraps

import jwt
import requests
from flask import (
    Flask, render_template, redirect, url_for, request,
    session, flash, jsonify, g
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABASE = os.environ.get("DATABASE_PATH", "dashboard.db")
GITHUB_API = "https://api.github.com"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
AUTO_REFRESH_SECONDS = int(os.environ.get("AUTO_REFRESH_SECONDS", "60"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS github_app_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            app_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_secret TEXT NOT NULL,
            private_key TEXT NOT NULL,
            webhook_secret TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS installations (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL UNIQUE,
            account_login TEXT NOT NULL,
            account_avatar_url TEXT DEFAULT '',
            account_type TEXT DEFAULT 'Organization',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS monitored_repos (
            id INTEGER PRIMARY KEY,
            installation_id INTEGER NOT NULL,
            repo_id INTEGER NOT NULL UNIQUE,
            repo_full_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            owner_login TEXT NOT NULL,
            owner_avatar_url TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (installation_id) REFERENCES installations(installation_id)
        );

        CREATE TABLE IF NOT EXISTS workflow_runs_cache (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL UNIQUE,
            repo_full_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            owner_avatar_url TEXT DEFAULT '',
            workflow_name TEXT DEFAULT '',
            head_branch TEXT DEFAULT '',
            event TEXT DEFAULT '',
            status TEXT DEFAULT '',
            conclusion TEXT DEFAULT '',
            actor_login TEXT DEFAULT '',
            actor_avatar_url TEXT DEFAULT '',
            html_url TEXT DEFAULT '',
            run_number INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            head_commit_message TEXT DEFAULT '',
            display_title TEXT DEFAULT '',
            cached_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_runs_created ON workflow_runs_cache(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_repo ON workflow_runs_cache(repo_full_name);
    """)
    db.close()


# ---------------------------------------------------------------------------
# GitHub App helpers
# ---------------------------------------------------------------------------
def get_app_config():
    """Get the stored GitHub App configuration."""
    db = get_db()
    row = db.execute("SELECT * FROM github_app_config WHERE id = 1").fetchone()
    return dict(row) if row else None


def generate_jwt_token(app_config):
    """Generate a JWT for GitHub App authentication."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": app_config["app_id"],
    }
    return jwt.encode(payload, app_config["private_key"], algorithm="RS256")


def get_installation_token(app_config, installation_id):
    """Exchange JWT for an installation access token."""
    jwt_token = generate_jwt_token(app_config)
    resp = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def github_api_get(token, url, params=None):
    """Make an authenticated GET request to GitHub API."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def require_app_config(f):
    """Redirect to setup if no GitHub App is configured."""
    @wraps(f)
    def decorated(*args, **kwargs):
        config = get_app_config()
        if not config:
            return redirect(url_for("setup"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes: Setup / Configuration
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Configure the GitHub App credentials."""
    config = get_app_config()

    if request.method == "POST":
        app_id = request.form.get("app_id", "").strip()
        client_id = request.form.get("client_id", "").strip()
        client_secret = request.form.get("client_secret", "").strip()
        private_key = request.form.get("private_key", "").strip()
        webhook_secret = request.form.get("webhook_secret", "").strip()

        if not all([app_id, client_id, client_secret, private_key]):
            flash("App ID, Client ID, Client Secret, and Private Key are all required.", "error")
            return render_template("setup.html", config=config, installs=[])

        # Validate the private key by trying to generate a JWT
        try:
            test_payload = {"iat": int(time.time()), "exp": int(time.time()) + 60, "iss": app_id}
            jwt.encode(test_payload, private_key, algorithm="RS256")
        except Exception as e:
            flash(f"Invalid private key: {e}", "error")
            return render_template("setup.html", config=config, installs=[])

        db = get_db()
        if config:
            db.execute("""
                UPDATE github_app_config
                SET app_id=?, client_id=?, client_secret=?, private_key=?, webhook_secret=?, updated_at=datetime('now')
                WHERE id=1
            """, (app_id, client_id, client_secret, private_key, webhook_secret))
        else:
            db.execute("""
                INSERT INTO github_app_config (id, app_id, client_id, client_secret, private_key, webhook_secret)
                VALUES (1, ?, ?, ?, ?, ?)
            """, (app_id, client_id, client_secret, private_key, webhook_secret))
        db.commit()

        flash("GitHub App configured successfully!", "success")
        return redirect(url_for("setup"))

    # Load installations for the settings page
    installs = []
    if config:
        db = get_db()
        installs = [dict(r) for r in db.execute(
            "SELECT * FROM installations ORDER BY account_login"
        ).fetchall()]

    return render_template("setup.html", config=config, installs=installs)


# ---------------------------------------------------------------------------
# Routes: GitHub App Manifest Flow
# ---------------------------------------------------------------------------
@app.route("/setup/manifest/create", methods=["POST"])
def manifest_create():
    """Initiate the GitHub App Manifest flow."""
    org = request.form.get("org", "").strip()
    app_name = request.form.get("app_name", "").strip() or f"action-watch-{secrets.token_hex(3)}"
    state = secrets.token_urlsafe(32)
    session["manifest_state"] = state

    base_url = request.url_root.rstrip("/")
    manifest = {
        "name": app_name,
        "url": base_url,
        "redirect_url": f"{base_url}/setup/manifest/callback",
        "setup_url": f"{base_url}/callback",
        "public": True,
        "default_permissions": {"actions": "read", "metadata": "read"},
    }

    if org:
        github_url = f"https://github.com/organizations/{org}/settings/apps/new?state={state}"
    else:
        github_url = f"https://github.com/settings/apps/new?state={state}"

    return render_template(
        "manifest_redirect.html",
        github_url=github_url,
        manifest=manifest,
    )


@app.route("/setup/manifest/callback")
def manifest_callback():
    """Handle the redirect back from GitHub after manifest app creation."""
    code = request.args.get("code", "")
    state = request.args.get("state", "")

    expected_state = session.pop("manifest_state", None)
    if not expected_state or state != expected_state:
        flash("Invalid or expired state token. Please try again.", "error")
        return redirect(url_for("setup"))

    if not code:
        flash("No code received from GitHub.", "error")
        return redirect(url_for("setup"))

    try:
        resp = requests.post(
            f"https://api.github.com/app-manifests/{code}/conversions",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        flash(f"Error exchanging manifest code: {e}", "error")
        return redirect(url_for("setup"))

    app_id = str(data["id"])
    client_id = data["client_id"]
    client_secret = data["client_secret"]
    private_key = data["pem"]
    webhook_secret = data.get("webhook_secret", "")

    db = get_db()
    config = get_app_config()
    if config:
        db.execute("""
            UPDATE github_app_config
            SET app_id=?, client_id=?, client_secret=?, private_key=?, webhook_secret=?, updated_at=datetime('now')
            WHERE id=1
        """, (app_id, client_id, client_secret, private_key, webhook_secret))
    else:
        db.execute("""
            INSERT INTO github_app_config (id, app_id, client_id, client_secret, private_key, webhook_secret)
            VALUES (1, ?, ?, ?, ?, ?)
        """, (app_id, client_id, client_secret, private_key, webhook_secret))
    db.commit()

    flash("GitHub App created and configured successfully!", "success")
    return redirect(url_for("setup"))


# ---------------------------------------------------------------------------
# Routes: GitHub App Installation
# ---------------------------------------------------------------------------
@app.route("/install")
@require_app_config
def install():
    """Redirect to GitHub to install/configure the App on an org/user."""
    config = get_app_config()
    slug = _get_app_slug(config)
    if not slug:
        flash("Could not determine your GitHub App's URL slug. Check your credentials.", "error")
        return redirect(url_for("setup"))

    install_url = f"https://github.com/apps/{slug}/installations/new"
    target = request.args.get("target", "").strip()
    if target:
        try:
            resp = requests.get(
                f"{GITHUB_API}/users/{target}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=10,
            )
            resp.raise_for_status()
            target_id = resp.json()["id"]
            install_url += f"?suggested_target_id={target_id}"
            flash(
                f"On the GitHub page, select \"{target}\" from the account list on the left.",
                "success",
            )
        except Exception:
            flash(f"Could not find GitHub account '{target}'.", "error")
            return redirect(url_for("setup"))

    return redirect(install_url)


@app.route("/callback")
@require_app_config
def callback():
    """Handle the redirect back from GitHub after installation (best-effort)."""
    return redirect(url_for("sync_all_installations"))


@app.route("/installations/sync-all", methods=["GET", "POST"])
@require_app_config
def sync_all_installations():
    """Discover all installations for this GitHub App via the API."""
    config = get_app_config()

    try:
        jwt_token = generate_jwt_token(config)
        # Fetch all installations for this app
        resp = requests.get(
            f"{GITHUB_API}/app/installations",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        all_installations = resp.json()

        for inst in all_installations:
            acct = inst.get("account", {})
            logger.info(
                "Found installation: id=%s account=%s type=%s repos_url=%s",
                inst["id"], acct.get("login"), acct.get("type"),
                inst.get("repositories_url", ""),
            )

        db = get_db()
        accounts = []
        for inst in all_installations:
            account = inst.get("account", {})
            installation_id = inst["id"]
            login = account.get("login", "unknown")
            accounts.append(login)

            db.execute("""
                INSERT INTO installations (installation_id, account_login, account_avatar_url, account_type)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    account_login=excluded.account_login,
                    account_avatar_url=excluded.account_avatar_url,
                    account_type=excluded.account_type
            """, (
                installation_id,
                login,
                account.get("avatar_url", ""),
                account.get("type", "Organization"),
            ))
            db.commit()

            _sync_installation_repos(config, installation_id)

        if accounts:
            flash(f"Synced {len(accounts)} installation(s): {', '.join(accounts)}", "success")
        else:
            flash("No installations found. Install the app on an org/user on GitHub first, then sync again.", "error")
    except Exception as e:
        logger.exception("Error syncing installations")
        flash(f"Error syncing installations: {e}", "error")

    referrer = request.referrer or ""
    if "/installations" in referrer:
        return redirect(url_for("installations"))
    return redirect(url_for("setup"))


def _get_app_slug(config):
    """Get the app slug from GitHub API."""
    try:
        jwt_token = generate_jwt_token(config)
        resp = requests.get(
            f"{GITHUB_API}/app",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("slug", "")
    except Exception:
        return ""


def _sync_installation_repos(config, installation_id):
    """Sync the list of repos accessible to an installation."""
    token = get_installation_token(config, installation_id)

    repos = []
    page = 1
    while True:
        data = github_api_get(
            token,
            f"{GITHUB_API}/installation/repositories",
            params={"per_page": 100, "page": page},
        )
        repos.extend(data.get("repositories", []))
        if len(data.get("repositories", [])) < 100:
            break
        page += 1

    db = get_db()
    for repo in repos:
        owner = repo.get("owner", {})
        db.execute("""
            INSERT INTO monitored_repos (installation_id, repo_id, repo_full_name, repo_name, owner_login, owner_avatar_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                repo_full_name=excluded.repo_full_name,
                repo_name=excluded.repo_name,
                owner_login=excluded.owner_login,
                owner_avatar_url=excluded.owner_avatar_url,
                installation_id=excluded.installation_id
        """, (
            installation_id,
            repo["id"],
            repo["full_name"],
            repo["name"],
            owner.get("login", ""),
            owner.get("avatar_url", ""),
        ))
    db.commit()
    logger.info(f"Synced {len(repos)} repos for installation {installation_id}")


# ---------------------------------------------------------------------------
# Routes: Installations & Repo Management
# ---------------------------------------------------------------------------
@app.route("/installations")
@require_app_config
def installations():
    """Show a flat list of all repos from all installations."""
    db = get_db()
    repos = [dict(r) for r in db.execute(
        "SELECT * FROM monitored_repos ORDER BY repo_full_name"
    ).fetchall()]

    return render_template("installations.html", repos=repos)


@app.route("/repos/toggle/<int:repo_id>", methods=["POST"])
@require_app_config
def toggle_repo(repo_id):
    """Toggle a repo's active monitoring status."""
    db = get_db()
    db.execute(
        "UPDATE monitored_repos SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE repo_id = ?",
        (repo_id,)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/repos/sync/<int:installation_id>", methods=["POST"])
@require_app_config
def sync_repos(installation_id):
    """Re-sync repos for an installation."""
    config = get_app_config()
    try:
        _sync_installation_repos(config, installation_id)
        flash("Repos synced successfully!", "success")
    except Exception as e:
        flash(f"Error syncing repos: {e}", "error")
    return redirect(url_for("installations"))


@app.route("/installations/remove/<int:installation_id>", methods=["POST"])
@require_app_config
def remove_installation(installation_id):
    """Remove an installation: uninstall from GitHub and delete local data."""
    config = get_app_config()

    # Call GitHub API to delete the installation
    try:
        jwt_token = generate_jwt_token(config)
        resp = requests.delete(
            f"{GITHUB_API}/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        # 204 = success, 404 = already gone — both are fine
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
    except Exception as e:
        logger.warning("GitHub API delete failed for installation %s: %s", installation_id, e)
        flash(f"Could not remove from GitHub (may already be removed): {e}", "error")

    # Remove local DB records regardless
    db = get_db()
    # Get repo names for cache cleanup
    repo_names = [r["repo_full_name"] for r in db.execute(
        "SELECT repo_full_name FROM monitored_repos WHERE installation_id = ?",
        (installation_id,)
    ).fetchall()]
    db.execute("DELETE FROM monitored_repos WHERE installation_id = ?", (installation_id,))
    db.execute("DELETE FROM installations WHERE installation_id = ?", (installation_id,))
    for name in repo_names:
        db.execute("DELETE FROM workflow_runs_cache WHERE repo_full_name = ?", (name,))
    db.commit()

    flash("Installation removed.", "success")
    return redirect(url_for("setup"))


# ---------------------------------------------------------------------------
# Routes: Dashboard (main view)
# ---------------------------------------------------------------------------
@app.route("/")
@require_app_config
def dashboard():
    """Main dashboard view."""
    db = get_db()
    active_repos = db.execute(
        "SELECT COUNT(*) as cnt FROM monitored_repos WHERE is_active = 1"
    ).fetchone()["cnt"]

    if active_repos == 0:
        return redirect(url_for("installations"))

    return render_template(
        "dashboard.html",
        auto_refresh_seconds=AUTO_REFRESH_SECONDS,
        lookback_days=LOOKBACK_DAYS,
    )


@app.route("/api/runs")
@require_app_config
def api_runs():
    """Fetch workflow runs for all active repos and return JSON."""
    config = get_app_config()
    db = get_db()

    active_repos = db.execute("""
        SELECT mr.*, i.installation_id as inst_id
        FROM monitored_repos mr
        JOIN installations i ON mr.installation_id = i.installation_id
        WHERE mr.is_active = 1
    """).fetchall()

    if not active_repos:
        return jsonify({"runs": [], "updated_at": _now_iso()})

    # Group repos by installation
    by_install = {}
    for repo in active_repos:
        iid = repo["inst_id"]
        if iid not in by_install:
            by_install[iid] = []
        by_install[iid].append(dict(repo))

    all_runs = []
    errors = []

    for installation_id, repos in by_install.items():
        try:
            token = get_installation_token(config, installation_id)
        except Exception as e:
            errors.append(f"Token error for installation {installation_id}: {e}")
            continue

        for repo in repos:
            try:
                data = github_api_get(
                    token,
                    f"{GITHUB_API}/repos/{repo['repo_full_name']}/actions/runs",
                    params={"per_page": 2},
                )
                runs = data.get("workflow_runs", [])

                for run in runs:
                    actor = run.get("actor") or {}
                    all_runs.append({
                        "run_id": run["id"],
                        "repo_full_name": repo["repo_full_name"],
                        "repo_name": repo["repo_name"],
                        "owner_avatar_url": repo.get("owner_avatar_url", ""),
                        "workflow_name": run.get("name", ""),
                        "display_title": run.get("display_title", ""),
                        "head_branch": run.get("head_branch", ""),
                        "event": run.get("event", ""),
                        "status": run.get("status", ""),
                        "conclusion": run.get("conclusion", ""),
                        "actor_login": actor.get("login", ""),
                        "actor_avatar_url": actor.get("avatar_url", ""),
                        "html_url": run.get("html_url", ""),
                        "run_number": run.get("run_number", 0),
                        "created_at": run.get("created_at", ""),
                        "updated_at": run.get("updated_at", ""),
                        "head_commit_message": (run.get("head_commit") or {}).get("message", ""),
                    })

            except Exception as e:
                errors.append(f"Error fetching {repo['repo_full_name']}: {e}")

    # Sort by created_at descending (newest first)
    all_runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    # Cache to DB
    _cache_runs(db, all_runs)

    return jsonify({
        "runs": all_runs,
        "updated_at": _now_iso(),
        "errors": errors,
    })


@app.route("/api/runs/cached")
@require_app_config
def api_runs_cached():
    """Return cached runs (fast, no GitHub API calls)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM workflow_runs_cache ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    return jsonify({
        "runs": [dict(r) for r in rows],
        "updated_at": _now_iso(),
        "cached": True,
    })


def _cache_runs(db, runs):
    """Cache workflow runs to SQLite."""
    for run in runs:
        db.execute("""
            INSERT INTO workflow_runs_cache
                (run_id, repo_full_name, repo_name, owner_avatar_url, workflow_name,
                 head_branch, event, status, conclusion, actor_login, actor_avatar_url,
                 html_url, run_number, created_at, updated_at, head_commit_message,
                 display_title, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                conclusion=excluded.conclusion,
                updated_at=excluded.updated_at,
                cached_at=datetime('now')
        """, (
            run["run_id"], run["repo_full_name"], run["repo_name"],
            run["owner_avatar_url"], run["workflow_name"], run["head_branch"],
            run["event"], run["status"], run["conclusion"],
            run["actor_login"], run["actor_avatar_url"], run["html_url"],
            run["run_number"], run["created_at"], run["updated_at"],
            run["head_commit_message"], run.get("display_title", ""),
        ))
    db.commit()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes: Settings reset
# ---------------------------------------------------------------------------
@app.route("/settings/reset", methods=["POST"])
def reset_config():
    """Clear the GitHub App configuration."""
    db = get_db()
    db.execute("DELETE FROM github_app_config")
    db.execute("DELETE FROM installations")
    db.execute("DELETE FROM monitored_repos")
    db.execute("DELETE FROM workflow_runs_cache")
    db.commit()
    flash("All configuration has been reset.", "success")
    return redirect(url_for("setup"))


# ---------------------------------------------------------------------------
# App info API (for the frontend to know about configuration state)
# ---------------------------------------------------------------------------
@app.route("/api/config/status")
def config_status():
    """Return whether the app is configured."""
    config = get_app_config()
    db = get_db()
    install_count = 0
    repo_count = 0
    if config:
        install_count = db.execute("SELECT COUNT(*) as cnt FROM installations").fetchone()["cnt"]
        repo_count = db.execute("SELECT COUNT(*) as cnt FROM monitored_repos WHERE is_active=1").fetchone()["cnt"]
    return jsonify({
        "configured": config is not None,
        "installations": install_count,
        "active_repos": repo_count,
        "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
    })


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
init_db()


def main():
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
