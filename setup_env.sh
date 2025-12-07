#!/bin/bash

# GitHub Repository Backup - Environment Setup Script
# This script sets up the Python environment using uv for faster package management

set -e  # Exit on error

# Color codes for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
ENV_NAME=".venv"
PYTHON_VERSION="3.9"

echo "=================================="
echo "GitHub Backup - Environment Setup"
echo "=================================="
echo ""

# Function to print colored messages
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    print_warn "uv is not installed. Installing uv..."

    # Install uv using the official installer
    if command -v curl &> /dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget &> /dev/null; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        print_error "Neither curl nor wget is available. Please install one of them first."
        exit 1
    fi

    # Source the uv environment
    export PATH="$HOME/.cargo/bin:$PATH"

    # Verify installation
    if ! command -v uv &> /dev/null; then
        print_error "Failed to install uv. Please install it manually from https://github.com/astral-sh/uv"
        exit 1
    fi

    print_info "uv installed successfully!"
else
    print_info "uv is already installed ($(uv --version))"
fi

# Create virtual environment with uv
print_info "Creating virtual environment '$ENV_NAME'..."
if [ -d "$ENV_NAME" ]; then
    print_warn "Virtual environment already exists. Removing it..."
    rm -rf "$ENV_NAME"
fi

uv venv "$ENV_NAME" --python "$PYTHON_VERSION"

# Check if virtual environment was created successfully
if [ ! -d "$ENV_NAME" ]; then
    print_error "Failed to create virtual environment."
    exit 1
fi

print_info "Virtual environment created successfully!"

# Activate the virtual environment
print_info "Activating virtual environment..."
source "$ENV_NAME/bin/activate"

# Install required packages using uv
print_info "Installing required packages with uv..."
uv pip install -r requirements.txt

# Verify installation
print_info "Verifying installation..."
python -c "import github; import git; print('All packages imported successfully!')" 2>/dev/null
if [ $? -eq 0 ]; then
    print_info "Package verification successful!"
else
    print_error "Package verification failed. Some packages may not be installed correctly."
    exit 1
fi

echo ""
echo "=================================="
print_info "Setup completed successfully!"
echo "=================================="
echo ""
echo "To activate the virtual environment manually, run:"
echo "  source $ENV_NAME/bin/activate"
echo ""
echo "To run the backup script:"
echo "  python github-backup.py token.txt -o repositories"
echo ""
echo "For help:"
echo "  python github-backup.py --help"
echo ""

# Keep the environment active by executing an interactive shell
exec "$SHELL"