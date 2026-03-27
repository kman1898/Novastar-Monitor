.PHONY: install run build clean test lint

# Install production dependencies
install:
	cd src && pip install -r requirements.txt

# Install dev dependencies (includes pyinstaller, pytest)
install-dev:
	pip install -r requirements-dev.txt

# Run the development server
run:
	cd src && python app.py

# Run with system tray (Windows/Linux)
run-tray:
	cd src && python launcher_pc.py

# Run with system tray (macOS)
run-tray-mac:
	cd src && python launcher_mac.py

# Build standalone executable
build:
	cd src && pyinstaller novastar_monitor.spec --clean

# Run tests
test:
	python -m pytest tests/ -v

# Clean build artifacts
clean:
	rm -rf src/build src/dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
