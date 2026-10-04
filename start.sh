#!/bin/bash

# Lamahub - Local Start Script

cd "$(dirname "$0")"

# Default values
OLLAMA_HOST="localhost"
OLLAMA_PORT=""
BASE_PATH_ARG="${BASE_PATH:-}"
LOGIN_USER_ARG="${LOGIN_USER:-}"
LOGIN_PW_ARG="${LOGIN_PW:-}"
LOGIN_TIMEOUT_ARG="${LOGIN_TIMEOUT:-}"

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  -h, --host HOST         Ollama server host (default: localhost)"
    echo "  -p, --port PORT         Ollama server port (optional)"
    echo "  -b, --base-path PATH    Public base path for hosting under a subdirectory"
    echo "  -u, --login-user USER   Enable login with this username"
    echo "  -w, --login-pw PW       Password for the login user"
    echo "  -t, --login-timeout MIN Session idle timeout in minutes (default: 60)"
    echo "      --help              Show this help message"
}

# Show help if no arguments provided
if [[ $# -eq 0 ]]; then
    usage
    exit 0
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--host)
            OLLAMA_HOST="$2"
            shift 2
            ;;
        -p|--port)
            OLLAMA_PORT="$2"
            shift 2
            ;;
        -b|--base-path)
            BASE_PATH_ARG="$2"
            shift 2
            ;;
        -u|--login-user)
            LOGIN_USER_ARG="$2"
            shift 2
            ;;
        -w|--login-pw)
            LOGIN_PW_ARG="$2"
            shift 2
            ;;
        -t|--login-timeout)
            LOGIN_TIMEOUT_ARG="$2"
            shift 2
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

OLLAMA_HOST="${OLLAMA_HOST#https://}"
OLLAMA_HOST="${OLLAMA_HOST#http://}"

# Set Ollama URL from arguments or environment
if [ -n "$OLLAMA_PORT" ]; then
    export OLLAMA_URL="${OLLAMA_URL:-http://$OLLAMA_HOST:$OLLAMA_PORT}"
else
    export OLLAMA_URL="${OLLAMA_URL:-http://$OLLAMA_HOST}"
fi

if [ -n "$BASE_PATH_ARG" ]; then
    export BASE_PATH="$BASE_PATH_ARG"
fi

# Default to verbose logging so the Log tab captures everything; override by
# exporting LOG_LEVEL before running.
export LOG_LEVEL="${LOG_LEVEL:-DEBUG}"

# Enable login when a username and password are provided.
if [ -n "$LOGIN_USER_ARG" ] && [ -n "$LOGIN_PW_ARG" ]; then
    export LOGIN="true"
    export LOGIN_USER="$LOGIN_USER_ARG"
    export LOGIN_PW="$LOGIN_PW_ARG"
    if [ -n "$LOGIN_TIMEOUT_ARG" ]; then
        export LOGIN_TIMEOUT="$LOGIN_TIMEOUT_ARG"
    fi
elif [ -n "$LOGIN_USER_ARG" ] || [ -n "$LOGIN_PW_ARG" ]; then
    echo "Error: --login-user and --login-pw must be provided together"
    exit 1
fi

echo "Starting Lamahub"
echo "Ollama URL: $OLLAMA_URL"
if [ -n "$BASE_PATH" ]; then
    echo "Base path: $BASE_PATH"
fi
if [ "$LOGIN" = "true" ]; then
    echo "Login: enabled (user: $LOGIN_USER)"
fi
echo ""

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Installing dependencies..."
else
    source venv/bin/activate
fi

# Update the app
pip install -e .

# Run the UI server (use "lamahub --reload" for development)
exec lamahub --host 0.0.0.0 --port 5000
