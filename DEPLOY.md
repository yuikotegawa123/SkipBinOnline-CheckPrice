# Streamlit Community Cloud — skip bin price checker
#
# Deploy steps
# ----------
# 1. Push this repo to GitHub (the .streamlit/secrets.toml file is gitignored
#    and must NOT be committed).
# 2. Streamlit Community Cloud → "New app" → pick the repo.
# 3. Main file path:  app.py
# 4. Deploy.
# 5. Settings → Secrets → add:
#
#       [gist]
#       token   = "ghp_xxxxxxxxxxxxxxxxxxxx"
#       gist_id = "xxxxxxxxxxxxxxxxxxxxxxxx"
#
#    The GitHub PAT needs the `gist` scope. Without it the app still runs,
#    but supplier accounts are stored locally instead of syncing.
#
# 6. runtime.txt pins Python 3.11 and packages.txt installs
#    chromium + chromium-driver (needed by the two Selenium scrapers).
#
# Optional tuning (Settings → Environment variables)
# -------------------------------------------------
#   SKIPBIN_MAX_WORKERS     hard cap on parallel workers (default: CPU-based)
#   SKIPBIN_SEARCH_TIMEOUT   seconds before a supplier search is abandoned (600)
#
# Note on resources
# -----------------
# The free tier has ~1 vCPU / 1 GB RAM. The Home page automatically runs the
# two Selenium-based suppliers sequentially on low-core machines to avoid
# OOM kills; on >=4 cores they run in parallel.
