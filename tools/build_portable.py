"""Build the portable Windows folder: copy it to a server, double-click ``Start tmt.bat``, done. No install, no admin rights.

The bundle holds an embedded Python (the official "embeddable package" from python.org), every dependency as a
pre-built wheel unpacked next to it, tmt itself, the demo project and the launcher. It needs nothing on the target
machine, not even Python, and nothing is written outside the user's own profile.

    python tools/build_portable.py                  # -> dist/tmt/   (zip this folder and hand it over)
    python tools/build_portable.py --zip            # also writes dist/tmt-<version>-win64.zip
    python tools/build_portable.py --python 3.13.3  # pick the embedded Python (must match the wheels' tag)

Needs internet access once, on the build machine (python.org and PyPI). The result runs offline.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (ROOT / "Start tmt.bat").read_text(encoding="ascii")  # the same launcher the project root has
README = """tmt - Dependency Explorer (portable)
====================================

Shows what a Talend master job depends on: the jobs it starts, the tables and files they read and write, and the
database connections they use. It only READS your job files. Nothing is installed, executed or uploaded.

Start
-----
1. Copy or unzip this folder anywhere you can write to (your Desktop, Documents, or a shared tools folder).
2. Double-click "Start tmt.bat". A window opens and so does your browser.
3. Choose the workspace (the folder that contains your Talend projects) and a master job.

Several users or servers
------------------------
* Every server needs a copy of this folder (or a shared one that every user can read).
* Every user just double-clicks "Start tmt.bat" in their own Windows session. Each user sees THEIR workspace:
  it is found automatically (for example C:\\Users\\<you>\\workspace) or chosen once and remembered in the browser.
* Two users on the same server at the same time are fine: the second one gets another port automatically.

Handy
-----
* Drag a workspace folder onto "Start tmt.bat" to open that workspace directly.
* Set the environment variable TMT_WORKSPACE to make a folder the default for a user.
* Command line, if you prefer it:  python\\python.exe -m tmt graph "<workspace>" --master <job> --format svg --out out

If Windows asks "Do you want to run this?": the files come from your own build. If a zip was downloaded, right-click
it > Properties > Unblock before unzipping.

Stopping: close the black window (or press Ctrl+C in it).
"""


def project_info() -> tuple[str, list[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"], list(data["project"]["dependencies"])


def download(url: str, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as res, target.open("wb") as out:  # noqa: S310 - fixed https URL
        shutil.copyfileobj(res, out)


def build(py_version: str, out_dir: Path, cache: Path, make_zip: bool = False) -> Path:
    version, deps = project_info()
    short = "".join(py_version.split(".")[:2])  # 3.13.3 -> 313
    stage = out_dir / "tmt"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "app").mkdir(parents=True)

    # 1. embedded Python, with a path file that finds our folders and nothing else
    embed = cache / f"python-{py_version}-embed-amd64.zip"
    download(f"https://www.python.org/ftp/python/{py_version}/python-{py_version}-embed-amd64.zip", embed)
    with zipfile.ZipFile(embed) as z:
        z.extractall(stage / "python")
    pth = stage / "python" / f"python{short}._pth"
    pth.write_text(f"python{short}.zip\n.\n..\\app\n..\\app\\site-packages\nimport site\n", encoding="ascii")

    # 2. dependencies, as ready-made wheels for that Python (nothing is compiled on the build or target machine)
    site = stage / "app" / "site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(site), "--platform", "win_amd64",
                    "--python-version", py_version.rsplit(".", 1)[0], "--implementation", "cp",
                    "--only-binary=:all:", "--no-compile", "--disable-pip-version-check", *deps], check=True)

    # 3. tmt itself, the demo project, the launcher
    shutil.copytree(ROOT / "src" / "tmt", stage / "app" / "tmt", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "samples" / "talend_demo", stage / "samples" / "talend_demo")
    shutil.copy(ROOT / "config.example.yaml", stage / "config.example.yaml")
    (stage / "Start tmt.bat").write_text(LAUNCHER, encoding="ascii", newline="\r\n")
    (stage / "README.txt").write_text(README, encoding="utf-8", newline="\r\n")
    (stage / "VERSION.txt").write_text(f"tmt {version}\nembedded Python {py_version}\n", encoding="ascii")
    for stray in list(stage.rglob("__pycache__")):
        shutil.rmtree(stray, ignore_errors=True)

    if not make_zip:
        return stage
    target = out_dir / f"tmt-{version}-win64.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, ("tmt/" + f.relative_to(stage).as_posix()))
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default="3.13.3", help="embedded Python version (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--zip", action="store_true", help="also write a zip next to the folder")
    ap.add_argument("--cache", type=Path, default=ROOT / "dist" / ".cache")
    args = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.python):
        sys.exit("--python must look like 3.13.3")
    target = build(args.python, args.out, args.cache, args.zip)
    print(f"built {target}")


if __name__ == "__main__":
    main()
