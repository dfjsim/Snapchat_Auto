call .venv\Scripts\activate.bat

rem --include-data-dir silently skips .exe files (Nuitka's default_ignored_suffixes covers
rem .exe/.dll/.bin), so scripts\data\sqlcipher3.exe never reached the onefile build and the
rem old-schema gallery.encrypteddb could not be decrypted at runtime (WinError 2). An
rem explicit --include-data-files is not suffix-filtered, so it does ship the binary.
rem A sqlcipher3 Python binding installed in .venv is bundled automatically and is used
rem first at runtime; the exe below is the fallback.

rem pyproject.toml is bundled so get_version() can read the version at runtime and show it in the
rem GUI (Nuitka sets neither sys.frozen nor sys._MEIPASS, and package metadata is absent in the exe).

rem Poster-frame extraction runs in a second process so that a cached video that hangs the decoder
rem can be killed (scripts\data\poster_worker.py). There is no interpreter to run "python -m" with
rem here, so the built exe re-enters ITSELF with --poster-worker, which Snapchat_Auto.main handles
rem before anything else. Nothing extra to bundle - but do not remove that flag, or every cached
rem video would start a copy of the GUI.

python -m nuitka --onefile --output-dir=dist --enable-plugin=tk-inter ^
	--include-data-dir=scripts=scripts ^
	--include-data-files=scripts\data\sqlcipher3.exe=scripts\data\sqlcipher3.exe ^
	--include-data-files=pyproject.toml=pyproject.toml ^
	Snapchat_Auto.py
