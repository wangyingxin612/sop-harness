.PHONY: test eval eval-one independence simulate frontend-build docker-build docker-run dev-backend dev-frontend

test:
	cd backend && . ../.venv/bin/activate && pytest -q

eval:
	cd backend && . ../.venv/bin/activate && python -m evals.runner

eval-one:
	cd backend && . ../.venv/bin/activate && python -m evals.runner --id $(ID)

# The thesis test: the same suite against a deliberately hostile model.
# Free, no API key, ~10s -- so it can run on every commit, unlike `eval`.
independence:
	cd backend && . ../.venv/bin/activate && python -m evals.independence

# Simulated callers: personas with goals who improvise, judged by the same
# invariants. Costs money; use it to explore, not to gate.
simulate:
	cd backend && . ../.venv/bin/activate && python -m evals.simulator --conversations $(or $(N),6)

frontend-build:
	cd frontend && npm install && npm run build

docker-build:
	docker build -t sop-harness .

docker-run:
	docker run -p 8000:8000 -e ANTHROPIC_API_KEY=$(ANTHROPIC_API_KEY) sop-harness

dev-backend:
	cd backend && . ../.venv/bin/activate && uvicorn app.api.main:app --reload

dev-frontend:
	cd frontend && npm run dev
