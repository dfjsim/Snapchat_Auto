call .venv\Scripts\activate.bat

python -m nuitka --onefile --output-dir=dist --enable-plugin=tk-inter ^
	--include-data-dir=scripts=scripts ^
	Snapchat_Auto_v1.2.1.py
