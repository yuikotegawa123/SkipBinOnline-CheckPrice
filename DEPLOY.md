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
# 6. Settings → "Advanced settings" → Python version → 3.11 (or 3.12).
#
#    IMPORTANT: Community Cloud **ignores runtime.txt**. The Python version
#    can only be selected in the dashboard. The runtime.txt file is kept for
#    Docker / Heroku / self-hosted deployments only.
#
# 7. packages.txt installs chromium + chromium-driver (needed by the two
#    Selenium scrapers) plus the shared libraries Chromium needs at runtime.
#
#    IMPORTANT: packages.txt is parsed as **one package name per line**.
#    Comment lines starting with '#' are NOT supported — apt is handed them
#    as package names, the install fails with "Unable to locate package" and
#    the whole deployment aborts with "installer returned a non-zero exit
#    code". Keep the file to bare package names only.
#
#    Also note that Community Cloud runs Debian 13 "trixie", where several
#    libraries were renamed for the 64-bit time_t transition:
#    libasound2 → libasound2t64, libatk1.0-0 → libatk1.0-0t64,
#    libatk-bridge2.0-0 → libatk-bridge2.0-0t64,
#    libatspi2.0-0 → libatspi2.0-0t64, libcups2 → libcups2t64,
#    libglib2.0-0 → libglib2.0-0t64, libgtk-3-0 → libgtk-3-0t64.
#
# Optional tuning (Settings → Environment variables)
# -------------------------------------------------
#   SKIPBIN_MAX_WORKERS     hard cap on parallel workers (default: CPU-based)
#   SKIPBIN_SEARCH_TIMEOUT   seconds before a supplier search is abandoned (600)
#
# Note on resources
# -----------------
# The free tier has ~1 vCPU / 1 GB RAM. The Home page automatically runs the
# two Selenium-based suppliers sequentially on low-core machines (and always
# in the cloud) to avoid OOM kills; on >=4 cores they run in parallel.
#
# Diagnosing a failed search
# --------------------------
# Sidebar → "⚙️ Environment" shows the resolved Chrome/driver paths, the
# Python version, whether the app is running in a cloud container, and a
# "🧪 Test Chrome launch" button that actually starts headless Chrome and
# reports the exact error. Scrapers also write [Bookabin] / [SBF] lines to
# the Streamlit logs and to $TMPDIR/skipbin_cache/*_debug.log.
