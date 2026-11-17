.PHONY: install run-adk-web run-adk-cli run-api test eval tf-init tf-validate deploy-cloud-run deploy-agent-engine

install:
	pip install -e ".[dev]"

run-adk-web:
	adk web . --port 8000

run-adk-cli:
	adk run resolutions_agent

run-api:
	uvicorn app_server.main:fastapi_app --reload --port 8080

test:
	pytest tests/ -v

eval:
	adk eval resolutions_agent evals/resolutions_2027.evalset.json --config_file_path=evals/eval_config.json

tf-init:
	terraform -chdir=terraform init -backend=false

tf-validate:
	terraform -chdir=terraform validate

deploy-cloud-run:
	adk deploy cloud_run --project=$${GOOGLE_CLOUD_PROJECT} --region=$${GOOGLE_CLOUD_LOCATION:-us-central1} --with_ui resolutions_agent

deploy-agent-engine:
	adk deploy agent_engine --project=$${GOOGLE_CLOUD_PROJECT} --region=$${GOOGLE_CLOUD_LOCATION:-us-central1} resolutions_agent
