#!/bin/bash

# Name of the virtual environment
ENV_NAME=".env"

# Create a virtual environment
python3 -m venv $ENV_NAME

# Check if virtual environment was created successfully
if [ ! -d "$ENV_NAME" ]; then
  echo "Failed to create virtual environment."
  exit 1
fi

# Activate the virtual environment
source $ENV_NAME/bin/activate

# Upgrade pip and install required packages
pip install --upgrade pip
pip install requests gitpython

echo "Virtual environment '$ENV_NAME' created and activated."
echo "Required packages installed."
echo "You can now run your backup script."

# Keep the environment active by executing an interactive shell
exec "$SHELL"