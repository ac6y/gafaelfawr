#!/bin/bash
#
# Start the Gafaelfawr application inside the Docker image.  Currently creates
# the database.  Eventually, this will call Alembic to handle database
# migrations.

set -eu

gafaelfawr init
uvicorn gafaelfawr.main:app --host 0.0.0.0 --port 8080