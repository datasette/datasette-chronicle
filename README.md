# datasette-chronicle

[![PyPI](https://img.shields.io/pypi/v/datasette-chronicle.svg)](https://pypi.org/project/datasette-chronicle/)
[![Changelog](https://img.shields.io/github/v/release/simonw/datasette-chronicle?include_prereleases&label=changelog)](https://github.com/simonw/datasette-chronicle/releases)
[![Tests](https://github.com/simonw/datasette-chronicle/workflows/Test/badge.svg)](https://github.com/simonw/datasette-chronicle/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/datasette-chronicle/blob/main/LICENSE)

Use sqlite-chronicle with tables in Datasette

## Installation

Install this plugin in the same environment as Datasette.

    datasette install datasette-chronicle

## Usage

Usage instructions go here.

## Development

To set up this plugin locally, first checkout the code. Then create a new virtual environment:

    cd datasette-chronicle
    python3 -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
