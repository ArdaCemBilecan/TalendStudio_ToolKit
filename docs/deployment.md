# Giving it to your users: the portable bundle

Many customers have several servers and several users, each with their own Talend workspace. `tmt` reads the
workspace of whoever starts it, so every user needs to be able to start it. The portable bundle makes that a
double-click: no installation, no administrator rights, no Python on the machine.

## Build it (once, on any Windows machine with internet access)

```bash
pip install -e ".[dev]"
python tools/build_portable.py            # -> dist/tmt/   (about 60 MB unpacked)
```

`dist/tmt/` is the folder to give the customer: zip it and send it. It holds the official embedded Python from
python.org, every dependency as a ready-made wheel, `tmt`, the demo project (`samples/talend_demo`), a short
`README.txt` and the launcher **`Start tmt.bat`**. It runs fully offline. `--zip` also writes `dist/tmt-<version>-win64.zip`;
`--python 3.13.x` picks another embedded Python (3.13 is the default and the one the tests run on).

Do not zip the whole project folder: it contains your development environment (`.venv`), tests and notes, and the
customer would not need any of it.

The same launcher is in the project root (`Start tmt.bat`) for your own use: it picks the project's `.venv`.

## Roll it out

1. Copy the zip (or the folder) to each server and unzip it. Two layouts work, pick the simpler one for you:
   * **one shared folder per server** (for example `C:\Tools\tmt`): every user double-clicks the same
     `Start tmt.bat`. The bundle never writes into its own folder, so read-only access is enough;
   * **one copy per user** (their Desktop or Documents).
2. Each user double-clicks **`Start tmt.bat`** in their own Windows session. A window opens (keep it open while
   using the tool) and the browser opens the explorer.

What each user sees is *their* workspace:

* it is found automatically: `%TMT_WORKSPACE%` if set, otherwise a folder with Talend projects in the usual places
  (`~\workspace`, `~\Talend_WS`, or a folder with `talend` or `workspace` in its name on the Desktop or in
  Documents);
* otherwise they press *Browse...* once; the browser remembers the choice;
* a workspace can also be given directly: **drag the folder onto `Start tmt.bat`**, or
  `Start tmt.bat "D:\Talend\WS"`.

## Good to know

* **Several users on one server at the same time** is fine. The first gets port 8765, the next ones get a free
  port automatically; each address contains its own private token, so nobody can use another user's window.
* **Nothing leaves the machine**: the server listens on the loopback address only and only reads job files.
* **Windows warnings.** A zip downloaded from the internet is marked by Windows; right-click the zip, *Properties*,
  *Unblock*, then unzip. If your security tooling blocks `.bat` files, run
  `python\python.exe -m tmt ui` from a terminal instead.
* **Updating** is replacing the folder with a newer zip; nobody keeps data inside it.
* **Command line** use works from the same bundle: `python\python.exe -m tmt graph "<workspace>" --master JOB
  --format svg --out out`.
* **Linux or macOS servers:** the bundle is Windows-only. There, `pip install .` into a virtual environment and
  run `tmt ui`.
