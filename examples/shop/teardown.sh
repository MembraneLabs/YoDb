#!/usr/bin/env bash
# Remove the sample database (it stores nothing outside the container).
docker rm -f yodb-sample-pg >/dev/null 2>&1 && echo "removed yodb-sample-pg"
