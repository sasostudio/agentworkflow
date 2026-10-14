"""Engine dashboard V2 — the Vercel site (엔진_대시보드_설계서 §10 · 10-04). The site IS dashboard.html (one file for
the PC and the cloud — it switches to cloud mode when it is not served from 127.0.0.1) plus config.json with the two
values that may be public: the Supabase URL and the publishable key. The secret key never leaves the PC: build()
refuses to write a site that contains it.

Where to look: the site shows "설정 없음" -> config.json here · the deploy fails -> deploy() (Vercel CLI output)
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .dashboard import HTML
from .safety import ENGINE_ROOT
from .uploader import load_env

SITE_DIR = ENGINE_ROOT / ".dashboard-site" / "engine-dashboard"  # the folder name = the Vercel project name


class SiteError(Exception):
    pass


def build(cfg=None, site_dir=SITE_DIR):
    """Write index.html + config.json (+ no-cache headers). -> the folder."""
    cfg = cfg or load_env()
    if not cfg.get("publishable"):
        raise SiteError(".env.dashboard 의 SUPABASE_PUBLISHABLE_KEY 가 비어 있음")
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "index.html": HTML.read_text(encoding="utf-8"),
        "config.json": json.dumps({"url": cfg["url"], "key": cfg["publishable"]}, ensure_ascii=False),
        "vercel.json": json.dumps({"headers": [{"source": "/(.*)", "headers": [
            {"key": "Cache-Control", "value": "no-store"}, {"key": "X-Content-Type-Options", "value": "nosniff"},
            {"key": "Referrer-Policy", "value": "no-referrer"}]}]}, indent=2),
    }
    for name, text in files.items():
        if cfg["secret"] and cfg["secret"] in text:  # the one value that must never be published
            raise SiteError(f"{name} 에 secret key 가 들어감 — 배포 중단")
        (site_dir / name).write_text(text, encoding="utf-8")
    return site_dir


def deploy(out=print, site_dir=SITE_DIR):
    """build() then `vercel deploy --prod --yes` in that folder (the CLI links/creates the project on first use)."""
    d = build(site_dir=site_dir)
    exe = shutil.which("vercel")
    if not exe:
        raise SiteError("Vercel CLI 없음 — npm i -g vercel 뒤 vercel login")
    argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, "deploy", "--prod", "--yes"]  # vercel is a .cmd shim
    r = subprocess.run(argv, cwd=str(d), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL)
    out((r.stdout or "").strip()[-1500:])
    if r.returncode != 0:
        raise SiteError("vercel deploy 실패:\n" + (r.stderr or "").strip()[-1500:])
    return 0
