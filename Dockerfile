# The patch tag, not `3.14-slim`, and that is what keeps digest updates coming.
# Dependabot treats the tag as the version and will not open a pull request for
# a version one already exists for - and a closed one still counts. The 3 Aug
# `3.13-slim -> 3.14-slim` pull request was closed here, so every later rebuild
# of `3.14-slim` collided with it and went unoffered for seven weeks.
FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

WORKDIR /app

# Copy package metadata and source
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the package (no dev deps needed for runtime)
RUN pip install --no-cache-dir .

# MCP server uses stdio transport - no port to expose
ENTRYPOINT ["withings-mcp"]
