.PHONY: test run smoke up down kind-up chaos

test:
	python3 -m unittest discover -s tests -v
run:
	python3 app/server.py
smoke:
	python3 scripts/smoke.py
up:
	docker compose -f compose.yaml -f compose.dev.yaml up --build -d --wait --wait-timeout 120
down:
	docker compose -f compose.yaml -f compose.dev.yaml down -v
kind-up:
	kind create cluster --name stormpilot
	docker build -t stormpilot:local .
	kind load docker-image stormpilot:local --name stormpilot
	kubectl --context kind-stormpilot apply -f k8s/base.yaml
	kubectl --context kind-stormpilot -n stormpilot-lab rollout status deployment/stormpilot --timeout=180s
chaos:
	CONFIRM_CHAOS=yes bash scripts/chaos.sh
