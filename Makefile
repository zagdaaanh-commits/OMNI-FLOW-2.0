install:
	python -m pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 7860

worker:
	python -m app.worker

migrate:
	python scripts/migrate.py

test:
	OMNIFLOW_DISABLE_DOTENV=1 python -m pytest -q

compile:
	python -m compileall app agents models tools db scripts storage.py

prod-build:
	docker compose -f docker-compose.prod.yml build

prod-up:
	docker compose -f docker-compose.prod.yml up -d

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f --tail=100

deploy:
	./scripts/deploy_hk.sh
