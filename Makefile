.PHONY: test vendor verify-vendor

PYTHON ?= .venv/bin/python
PIP := $(PYTHON) -m pip

test:
	.venv/bin/pytest -v

# 런타임 + 테스트 의존성을 함께 담는다. 런북 7단계(JIRA_FIXTURES=captured pytest)가
# pytest를 요구하므로 pytest가 빠진 번들은 사내에서 쓸 수 없다 (R32).
vendor:
	$(PIP) download -r requirements-dev.txt -d vendor/ \
	  --only-binary=:all: --python-version 3.12 --platform manylinux2014_x86_64
	@echo "vendor/는 .gitignore되어 있다 — 반입은 git 단방향이므로 강제로 추적해야 한다:"
	@echo "  git add -f vendor/ && git commit"

verify-vendor:
	$(PYTHON) -m venv /tmp/offline-check
	/tmp/offline-check/bin/python -m pip install --no-index --find-links vendor/ \
	  -r requirements-dev.txt
	/tmp/offline-check/bin/python -c "import oracledb, httpx, pydantic_settings, pytest; print('ok')"
	rm -rf /tmp/offline-check
