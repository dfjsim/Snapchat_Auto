call .venv\Scripts\activate.bat

rem --include-data-dir silently skips .exe files (Nuitka's default_ignored_suffixes covers
rem .exe/.dll/.bin), so scripts\data\sqlcipher3.exe never reached the onefile build and the
rem old-schema gallery.encrypteddb could not be decrypted at runtime (WinError 2). An
rem explicit --include-data-files is not suffix-filtered, so it does ship the binary.
rem A sqlcipher3 Python binding installed in .venv is bundled automatically and is used
rem first at runtime; the exe below is the fallback.

rem pyproject.toml is bundled so get_version() can read the version at runtime and show it in the
rem GUI (Nuitka sets neither sys.frozen nor sys._MEIPASS, and package metadata is absent in the exe).

python -m nuitka --onefile --output-dir=dist --enable-plugin=tk-inter ^
	--include-data-dir=scripts=scripts ^
	--include-data-files=scripts\data\sqlcipher3.exe=scripts\data\sqlcipher3.exe ^
	--include-data-files=pyproject.toml=pyproject.toml ^
	Snapchat_Auto.py
