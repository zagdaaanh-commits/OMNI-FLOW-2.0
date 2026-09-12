install:
	python -m pip install -r requirements.txt
	python -m playwright install chromium

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest -q

compile:
	python -m compileall app agents models tools storage.py
