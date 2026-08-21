"""``tape ui`` -- a local viewer for what only reeltime knows.

Not an observability dashboard. LangSmith and Braintrust ship polished UIs
backed by teams, and competing on breadth loses on every axis at once. What no
other tool can render is the fork tree, the divergence point, a context diff
with truncation called out, the chain tree, and doctor findings grouped by call
site -- because no other tool *has* those. That is the whole surface here.

See ``ui-design.md`` at the repo root for the design this implements, including
the two places it supersedes the build spec: no web framework, and no fork
button.
"""

from .server import DEFAULT_PORT, HOST, build, serve, serve_in_thread

__all__ = ["DEFAULT_PORT", "HOST", "build", "serve", "serve_in_thread"]
