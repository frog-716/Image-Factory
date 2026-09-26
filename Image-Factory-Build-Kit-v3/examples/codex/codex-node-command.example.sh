#!/usr/bin/env bash
# DOCUMENTED TEXT-NODE EXAMPLE, NOT a proven native image adapter.
# Before actual use verify current `codex exec --help`, sandbox and job directory.
# Do not execute by sourcing this file. This sample deliberately exits.
printf '%s\n' 'Example only: implement adapter after local capability check.' >&2
exit 2
# Illustrative text-node form after approval:
# codex exec --json --sandbox workspace-write \
#   --output-schema ./output.schema.json -o ./result.json \
#   "Read frozen job.json; perform only this node; write allowed outputs."
# Never assume this same form enables native image generation without testing it.
