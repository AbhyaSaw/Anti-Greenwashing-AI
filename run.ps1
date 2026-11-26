# Start TerraTruth on http://127.0.0.1:8000
# First run:  python -m venv .venv ; .\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
