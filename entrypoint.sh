#!/bin/sh
# "ff_postprocess.py --execute" runs a Python script, anything else
# ("import_amex.sh /data/activity.csv", "sh") is run as it is.
# -I: isolated (no PYTHON* variables, no user site), -u: unbuffered log lines,
# -B: no bytecode (the container usually runs read-only)
case "${1:-}" in
    "")   exec python -I -u -B ff_postprocess.py --help ;;
    *.py) exec python -I -u -B "$@" ;;
    *)    exec "$@" ;;
esac
