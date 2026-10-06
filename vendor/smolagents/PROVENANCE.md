# Vendored smolagents

This directory preserves the complete `src/smolagents` Python package, its
original package metadata, README and Apache-2.0 license from the local source
repository used by the demo.

- Upstream project: Hugging Face smolagents (`huggingface/smolagents`).
- Upstream baseline recorded by the backend:
  `227ef5e49ddd82339295939072f0223249aa8d38`.
- Local source snapshot used for this release:
  `117a748526c8f0fbe3d0fb22f156f2e05fff2cb2`.
- Package version: `1.27.0.dev0`.
- Original upstream source files, README, LICENSE and pyproject are retained
  without changes in this release.

The complete source package is kept because its public `__init__` imports
interconnected modules. Trimming that import surface would be a compatibility
refactor. Upstream documentation, examples, tests and repository automation
are intentionally not included.

The demo core does not import smolagents. This package supplies the optional
legacy `Tool` compatibility API and associated backend tests, and must not be
confused with the demo's workflow executor.
