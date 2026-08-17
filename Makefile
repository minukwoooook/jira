.PHONY: test vendor verify-vendor

PYTHON ?= .venv/bin/python
PIP := $(PYTHON) -m pip

test:
	.venv/bin/pytest -v

vendor:
	$(PIP) download -r requirements.txt -d vendor/ \
	  --only-binary=:all: --python-version 3.12 --platform manylinux2014_x86_64

verify-vendor:
	$(PYTHON) -m venv /tmp/offline-check
	/tmp/offline-check/bin/python -m pip install --no-index --find-links vendor/ -r requirements.txt
	/tmp/offline-check/bin/python -c "import oracledb, httpx, pydantic_settings; print('ok')"
	rm -rf /tmp/offline-check
