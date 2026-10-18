"""Errors shared by the image and video pipelines."""


class OptimizationCancelled(Exception):
    """The user stopped the batch while this file was processing."""


class ConflictError(Exception):
    """The requested action clashes with a batch that is still running."""


class NotFoundError(Exception):
    """A file or batch id is not in the current session."""
