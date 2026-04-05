DEV_IMG = fermax-blue-dev
DEV_RUN = docker run --rm -v $(PWD):/app -w /app $(DEV_IMG)

.PHONY: lint format format-check typecheck test check cli pre-push dev-image

dev-image:
	@docker build -q -t $(DEV_IMG) -f Dockerfile.dev . > /dev/null

lint: dev-image
	$(DEV_RUN) ruff check custom_components/ tests/ scripts/

format: dev-image
	$(DEV_RUN) ruff format custom_components/ tests/ scripts/

format-check: dev-image
	$(DEV_RUN) ruff format --check custom_components/ tests/ scripts/

typecheck: dev-image
	$(DEV_RUN) mypy custom_components/fermax_blue/ --ignore-missing-imports

test: dev-image
	$(DEV_RUN) pytest tests/ -v --cov=custom_components/fermax_blue --cov-report=term-missing --tb=short

check: dev-image
	$(DEV_RUN) sh -c "ruff check custom_components/ tests/ scripts/ && ruff format --check custom_components/ tests/ scripts/ && mypy custom_components/fermax_blue/ --ignore-missing-imports && pytest tests/ -q --tb=short"

cli: dev-image
	docker run --rm -it -v $(PWD):/app -w /app -e FERMAX_USER -e FERMAX_PASS $(DEV_IMG) python scripts/cli.py

pre-push: dev-image
	bash scripts/pre-push.sh
