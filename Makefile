.PHONY: help install test self-test demo mock serve tools clean

PY ?= python3
export PYTHONPATH := src

help:  ## Show available targets
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime + dev dependencies
	$(PY) -m pip install -r requirements.txt

test:  ## Run the full test suite (offline, ~11s)
	$(PY) -m pytest -q

self-test:  ## 17 end-to-end checks against the bundled mock
	$(PY) -m freshdesk_connector.cli self-test

demo:  ## Run the three merchant workflows against the mock
	$(PY) -m freshdesk_connector.cli demo --mock

mock:  ## Run the mock Freshdesk API on :8099
	$(PY) -m freshdesk_connector.cli mock --port 8099

serve:  ## Run the MCP server over stdio (for an MCP client to spawn)
	$(PY) -m freshdesk_connector.cli serve

tools:  ## Regenerate the MCP tool specification
	$(PY) -m freshdesk_connector.cli tools --writes --out docs/mcp-tools.json

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist
