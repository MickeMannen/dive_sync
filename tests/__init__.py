"""The test suite is a regular package on purpose.

``garmin-fit-sdk``'s wheel installs a top-level package that is also called
``tests`` into site-packages. Without this file the repo's ``tests`` folder is
only a namespace package, which Python ranks below any regular package of the
same name wherever it sits on ``sys.path``; every ``from tests.test_x import
...`` in the suite would then resolve to Garmin's package and fail. With this
file the repo's folder (first on the path through ``run_tests.sh``'s
``PYTHONPATH=.`` and pytest's rootdir) wins.
"""
