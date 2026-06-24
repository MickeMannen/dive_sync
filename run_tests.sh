#!/bin/bash
# Script to run all unit tests in the dive_sync project
PYTHONPATH=. pytest "$@"
