# Build and deploy the market service. Run from anywhere: `make -C cloud <target>`.
# No Docker needed: prebuilt Linux wheels are fetched directly for Lambda's architecture.

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
CLOUD := $(ROOT)/cloud
PACKAGE := $(CLOUD)/build/package
PLATFORM := aarch64-manylinux_2_28
PYTHON_VERSION := 3.12
SAM := SAM_CLI_TELEMETRY=0 uvx --from aws-sam-cli sam

.PHONY: lock build check-bundle validate deploy

# Pin what the bundle installs. Rerun after changing requirements.txt.
lock:
	cd $(CLOUD) && uv pip compile requirements.txt --quiet --python-version $(PYTHON_VERSION) \
		--python-platform $(PLATFORM) --output-file requirements.lock

build:
	rm -rf $(CLOUD)/build
	mkdir -p $(PACKAGE)/cloud $(PACKAGE)/omni $(PACKAGE)/colors
	uv pip install --quiet --target $(PACKAGE) --python-version $(PYTHON_VERSION) \
		--python-platform $(PLATFORM) --only-binary :all: --no-deps -r $(CLOUD)/requirements.lock
	cp $(CLOUD)/__init__.py $(PACKAGE)/cloud/
	cp -r $(CLOUD)/market_service $(PACKAGE)/cloud/
	cp $(ROOT)/omni/__init__.py $(PACKAGE)/omni/
	cp -r $(ROOT)/omni/core $(ROOT)/omni/domain $(ROOT)/omni/events $(ROOT)/omni/providers $(PACKAGE)/omni/
	cp $(ROOT)/colors/teams.example.json $(PACKAGE)/colors/
	find $(PACKAGE) -type d -name __pycache__ -prune -exec rm -rf {} +
	$(MAKE) -f $(CLOUD)/Makefile check-bundle

# Load the handlers from the bundle alone, with the repo out of reach. The native wheels are built for
# Lambda and can't load here, which is fine: the handlers only import them once a key calls for them.
check-bundle:
	cd $(PACKAGE) && python3 -I -c "import sys; sys.path.insert(0, '.'); \
		import cloud.market_service.api_handler, cloud.market_service.poller_handler; \
		import omni.providers.mlb_statsapi; \
		from omni.providers.mlb_teams import MlbTeamRegistry; \
		assert len(MlbTeamRegistry.from_color_file()) == 30; \
		print('bundle ok')"

validate:
	cd $(CLOUD) && $(SAM) validate --lint

deploy: build
	cd $(CLOUD) && $(SAM) deploy $(DEPLOY_ARGS)
