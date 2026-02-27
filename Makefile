.PHONY: help test-conformance run-mock-hub test-go test-rust test-node clean install-deps

help:
	@echo "Agent IRC Hub - Available targets:"
	@echo ""
	@echo "  test-conformance  Run protocol conformance tests (against mock hub)"
	@echo "  run-mock-hub      Start the mock AIRCP hub"
	@echo "  install-deps      Install test dependencies"
	@echo "  clean             Clean up generated files"
	@echo ""
	@echo "Hub implementations:"
	@echo "  test-go           Run conformance tests against Go hub"
	@echo "  test-rust         Run conformance tests against Rust hub"
	@echo "  test-node         Run conformance tests against Node.js hub"

install-deps:
	cd conformance && pip install -r requirements.txt

test-conformance: install-deps
	cd conformance && python -m pytest -v

run-mock-hub: install-deps
	cd conformance && python mock_hub.py

test-go:
	@echo "Go hub not yet implemented"

test-rust:
	@echo "Rust hub not yet implemented"

test-node:
	@echo "Node.js hub not yet implemented"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ Cleaned up"
