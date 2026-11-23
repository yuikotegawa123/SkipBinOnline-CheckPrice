"""
Runtime tuning helpers shared by the four scraper modules.

The tool runs in three very different environments:
  * local Windows dev machine  → many cores, plenty of RAM
  * PyInstaller EXE            → same as above, but frozen
  * Streamlit Community Cloud  → 1-2 vCPU, 1 GB RAM, read-only filesystem

Hard-coded thread pools (5 Chrome instances x 5 workers) overwhelm the cloud
free tier, so worker counts are derived from the actual CPU count and can be
capped with the SKIPBIN_MAX_WORKERS environment variable.
"""

import os

# Cloud containers report 1-2 CPUs but Chrome still needs a few threads each.
_MIN_WORKERS = 2
_MAX_WORKERS = 8


def _env_int(name: str, default: int) -> int:
    try:
        val = os.environ.get(name)
        if val is None or str(val).strip() == "":
            return default
        n = int(str(val).strip())
        return n if n > 0 else default
    except Exception:
        return default


def cpu_count() -> int:
    """Return a sane CPU count (respects cgroup CPU quotas when visible)."""
    try:
        n = os.cpu_count() or 2
    except Exception:
        n = 2

    # Respect a cgroup v2 CPU quota (Streamlit Cloud uses container limits).
    try:
        for quota_path, period_path in (
            ("/sys/fs/cgroup/cpu.max", None),
            ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
        ):
            if not os.path.isfile(quota_path):
                continue
            with open(quota_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if quota_path.endswith("cpu.max"):
                # format: "<quota> <period>"  (quota == "max" means unlimited)
                parts = raw.split()
                if parts and parts[0] != "max":
                    quota = int(parts[0])
                    period = int(parts[1]) if len(parts) > 1 else 100000
                    if quota > 0 and period > 0:
                        n = min(n, max(1, quota // period))
                break
            else:
                if period_path and os.path.isfile(period_path):
                    with open(period_path, "r", encoding="utf-8") as f:
                        period = int(f.read().strip())
                    quota = int(raw)
                    if quota > 0 and period > 0:
                        n = min(n, max(1, quota // period))
                break
    except Exception:
        pass

    return max(1, n)


def workers(requested: int, min_workers: int = _MIN_WORKERS,
            max_workers: int = _MAX_WORKERS) -> int:
    """
    Scale a requested thread-pool size to the machine we are actually on.

    requested  – the number of workers the original code wanted
    returns   – a safe worker count for this environment
    """
    override = os.environ.get("SKIPBIN_MAX_WORKERS")
    if override:
        try:
            n = int(override)
            if n > 0:
                return max(1, n)
        except Exception:
            pass

    cpus = cpu_count()
    # Chrome instances are heavy: allow roughly one per CPU, but never fewer
    # than min_workers and never more than the originally requested amount.
    scaled = max(min_workers, min(requested, cpus + 1))
    return max(1, min(scaled, max_workers, requested))


def is_cloud() -> bool:
    """True when running inside a container/cloud environment."""
    if os.environ.get("STREAMLIT_SERVER_PORT") or os.environ.get("STREAMLIT_RUNTIME"):
        return True
    # Streamlit Community Cloud mounts the repo under /mount/src and runs the
    # app as /home/adminuser — neither the env vars nor /proc/1/cgroup are
    # guaranteed to reveal the container.
    if os.path.isdir("/mount/src") or os.path.isdir("/home/adminuser"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            content = f.read()
        return "docker" in content or "kubepods" in content or "containerd" in content
    except Exception:
        return False


def search_timeout(default: int = 600) -> int:
    """Maximum seconds a single supplier search may run before it is abandoned."""
    return _env_int("SKIPBIN_SEARCH_TIMEOUT", default)
