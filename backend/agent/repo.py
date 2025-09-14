import os, subprocess, shutil, zipfile, re
import requests

def shallow_clone(github_url: str, work_dir: str) -> str:
    repo_dir = os.path.join(work_dir, "repo")
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)
    os.makedirs(repo_dir, exist_ok=True)

    try:
        subprocess.run(["git", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "clone", "--depth", "1", github_url, repo_dir], check=True)
        return repo_dir
    except Exception:
        base = _extract_owner_repo(github_url)
        if not base:
            raise RuntimeError("Git not available and URL not recognized as a public GitHub repo")
        owner, repo = base
        for branch in ["main", "master"]:
            zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
            r = requests.get(zip_url, timeout=60)
            if r.status_code == 200:
                zip_path = os.path.join(work_dir, f"{repo}-{branch}.zip")
                with open(zip_path, "wb") as fz:
                    fz.write(r.content)
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(work_dir)
                extracted = os.path.join(work_dir, f"{repo}-{branch}")
                if os.path.isdir(extracted):
                    for name in os.listdir(extracted):
                        shutil.move(os.path.join(extracted, name), os.path.join(repo_dir, name))
                    shutil.rmtree(extracted)
                    return repo_dir
        raise RuntimeError("Failed to fetch repository via GitHub zip fallback")

def _extract_owner_repo(url: str):
    m = re.search(r"github\.com/([^/]+)/([^/]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = repo.replace(".git", "")
    return owner, repo