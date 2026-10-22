#!/bin/sh
# Export or import a settings profile. See profiles.py for details.
#   ./profile.sh export NAME [--with-secrets]
#   ./profile.sh import profiles/NAME.json
DIR=$(cd "$(dirname "$0")" && pwd)
exec "$DIR/venv/bin/python" "$DIR/profiles.py" "$@"
