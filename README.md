# GitHub Action Watch

Real-time dashboard for GitHub Actions across all your repos.

## Setup

### 1. Run

```bash
uv run app.py
```

### 2. Create a GitHub App

Open http://localhost:5001 — you'll land on the Settings page. Click **Create GitHub App** (optionally enter an org name to create it under that org).

### 3. Confirm on GitHub

GitHub shows a pre-filled app creation page with the right permissions. Click **Create GitHub App**.

### 4. Install and Sync

Back on the Settings page, your app is connected. Install it on your orgs via the **Install on Account** bar, then hit **Sync from GitHub** to discover repos.

Done. Your actions stream in real-time.

### Alternative: Use an Existing App

If you already have a GitHub App, expand **Use an Existing App** on the Settings page and enter your App ID, Client ID, Client Secret, and Private Key manually.
