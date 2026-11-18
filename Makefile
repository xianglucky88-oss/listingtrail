PY=python3
run:
	PYTHONPATH=src $(PY) -m listingtrail.cli
run-pages:
	PYTHONPATH=src $(PY) -m listingtrail.cli --pages fixtures/pages.json --db pages.db --reset --detailed
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
