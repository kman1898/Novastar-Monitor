# Building NovaStar Monitor

## Prerequisites

- Python 3.10+
- pip

## Development Setup

```bash
# Clone the repo
git clone https://github.com/kman1898/novastar-monitor.git
cd novastar-monitor

# Install dependencies
cd src
pip install -r requirements.txt

# Run in development mode
python app.py
```

## Building Standalone Executables

### Install build dependencies

```bash
pip install -r requirements-dev.txt
```

### Windows

```bash
cd src
pyinstaller novastar_monitor.spec
```

Output: `dist/NovaStar Monitor/NovaStar Monitor.exe`

### macOS

```bash
cd src
pyinstaller novastar_monitor.spec
```

Output: `dist/NovaStar Monitor.app`

### Linux

```bash
cd src
pyinstaller novastar_monitor.spec
```

Output: `dist/NovaStar Monitor/NovaStar Monitor`

## Build with Makefile

```bash
make install    # Install dependencies
make run        # Run development server
make build      # Build executable
make clean      # Clean build artifacts
```
