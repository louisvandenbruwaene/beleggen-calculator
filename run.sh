#!/bin/bash
# Beleggingen Web Application - Start Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

# Initialize database
echo "Initializing database..."
python3 -c "from app import init_db; init_db()"

# Run the application
echo ""
echo "=========================================="
echo "  Beleggingen Web Application"
echo "  http://localhost:5000"
echo "=========================================="
echo ""

python3 app.py
