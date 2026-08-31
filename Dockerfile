# The patch tag, not `3.14-slim`, and that is what keeps digest updates coming.
# Dependabot treats the tag as the version and will not open a pull request for
# a version one already exists for - and a closed one still counts. The 3 Aug
# `3.13-slim -> 3.14-slim` pull request was closed here, so every later rebuild
# of `3.14-slim` collided with it and went unoffered for seven weeks.
FROM python:3.14.7-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

WORKDIR /app

# Copy package metadata and source
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package (no dev deps needed for runtime)
RUN pip install --no-cache-dir .

# MCP server uses stdio transport - no port to expose
ENTRYPOINT ["withings-mcp"]
