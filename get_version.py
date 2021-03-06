#!/usr/bin/env python
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""A convenience tool for querying datman's version."""
import sys
import os.path as op


def main():
    """Print current datman version."""
    sys.path.insert(0, op.abspath("."))
    from datman.__about__ import __version__

    print(__version__)


if __name__ == "__main__":
    main()
