"""AWS Lambda entry points.

Handlers are deliberately thin. They parse the event, call into `pipeline`, and
shape a response. All the logic worth testing lives elsewhere, which is why
`pyproject.toml` excludes this package from coverage requirements - there is
nothing here to cover.
"""
