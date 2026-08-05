# Update checks

Inside an organisation this tool is handed out by dropping a new build on a shared folder, and
every examiner then runs a copy that quietly falls behind. The GUI can check that folder at
startup and offer the newer build. It is off until an examiner turns it on, it is the only thing
in the tool that looks at a network location other than the optional map tile server, and it never
reaches the internet: there is no update server, no version endpoint, and no telemetry — just a
directory listing.

The check itself is not implemented here: it lives in `dfjsim_shared_tools.auto_update`
(`check_for_update`, `describe_installer_dir`, `newest_installer`), pinned to a tag in
`pyproject.toml`, which is what makes the filename convention below stable and lets the sister
applications share it. What this repository owns is in [`Snapchat_Auto.py`](../Snapchat_Auto.py):
where the folder path comes from, and the GUI field and "Check" button that set it.

The import is deliberately guarded (`_updater()`): `requirements.txt`, the pip route the README
documents, does not carry the shared package, and an update check must never be what stops the
tool from starting.

## The folder is never in this repository

**This repository is public.** An internal share path is a filesystem path from an organisation's
infrastructure, so it may not appear in the source, in a committed config file, or inside a built
binary — the same rule as everywhere else in this project.

So the path is not configured in the repository at all. It lives only in the examiner's own GUI
settings file, `~/.snapchat_auto_gui.json`, under `installer_dir`, alongside the other remembered
GUI selections:

```json
{ "workdir": "...", "installer_dir": "\\\\server\\share\\Tools\\Snapchat_Auto" }
```

It is set in the GUI ("Folder with newer builds, for update checks"), with a **Check** button that
says straight away what the folder holds — a mistyped path or a disconnected share is worth
reporting while the examiner is looking at the field, not as a log line at the next start.

Empty is the default and means **no check runs and no folder is touched**. That is what anyone who
clones this repository gets.

## What counts as a newer build

Matching is by filename, against the convention the shared builder produces:

```
Snapchat_Auto-<X.Y.Z>+build.<N>[-win64].msi        (or .exe)
```

Anything else in the folder is ignored, including names that carry a version a person can read but
`packaging` cannot compare (`Snapchat_Auto-v1.5.0-js.exe`, how builds were named before this
existed). Versions are compared as PEP 440 versions, so the `+build.<N>` local segment breaks the
tie between two builds of the same `X.Y.Z`, and `-win32` files are ignored on a 64-bit machine.

**The running build must carry the same tag.** `[project].version` in `pyproject.toml` is what the
GUI reports and what the comparison uses, so it has to be `1.5.0+build.20260805`, not `1.5.0` — a
version without the tag is older than every tagged build of the same `X.Y.Z` and would offer an
"update" forever.

The version is read at runtime from `pyproject.toml`, which is why the build bundles it: the
onefile EXE with `--include-data-files` (see `build_nuitka.cmd`) and the MSI through
`[tool.wix-build] copy_files`. Without it the version reads `unknown`, and an unknown version
offers nothing — "cannot compare" must never turn into "everything on the share is newer".

## Publishing a build

1. Bump `[project].version` in `pyproject.toml` to `X.Y.Z+build.YYYYMMDD`.
2. `uv run build` → `dist/Snapchat_Auto-<version>-win64.msi` (Nuitka standalone + WiX; the WiX
   toolset has to be on the build machine).
3. Copy the MSI to the folder examiners have configured. That is the whole release.

The portable onefile EXE from `build_nuitka.cmd` is what GitHub visitors get, and it is offered by
the update check too if it is named to the same convention — but note the difference in what
"update" then means. `msiexec /i` installs the MSI over the existing installation; an `.exe` is
simply launched, so the stale local copy stays where it is and will offer the update again.

MSI note: a ProductVersion is numeric-only, so the `+build.<N>` tag is dropped from it (`1.5.0`).
The shared WiX template sets `AllowSameVersionUpgrades="yes"`, so a build-only bump still installs
over the previous one; the tag only ever lives in the filename and in the version the app reports.

## When it runs, and when it does not

- **GUI startup only**, before the disclaimer, because accepting an update launches the installer
  and ends the process.
- **Never on a headless run** (`--zip …`). A scripted run over several extractions has nobody
  there to answer a dialog, and must not be replaced mid-loop by an installer.
- **Never silently.** The shared helper installs without asking when it is given no parent window;
  it is always given one here, so an examiner mid-case decides.
- **Never fatally.** A disconnected share, a folder full of junk, an unreadable version: all are
  logged as a warning and the tool starts normally. An update check is a convenience; the run is
  the job.
