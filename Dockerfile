# Firefly III helper scripts (standard library only) on a plain Python image.
# The base image is pinned by tag and digest; Dependabot moves both.
FROM python:3.13.18-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e

# the two CSV import scripts are bash and upload with curl
RUN apk add --no-cache bash curl

WORKDIR /app
ENV PATH="/app:${PATH}"
COPY ff_postprocess.py ff_rules.py ff_subscriptions.py ./
COPY import_amex.sh import_mm.sh import_config_AMEX.json import_config_MM.json entrypoint.sh ./
COPY samples ./samples

# nobody: the scripts only talk to the Firefly API and read mounted files
USER 65534:65534

ENTRYPOINT ["/app/entrypoint.sh"]
