# Numeric Clamp

Copy this directory into the configured workflow package root, then select
`example.numeric-clamp@1.0.0` through the normal package configuration command.
Declared dependencies are resolved by the package loader.

The `example.numeric-clamp-tools@1` node binds `numeric_clamp` to an
`agents.execute@10` node. Connect its `materials` output to the context assembly
`materials` input. The target `example.numeric-clamp@1` is synchronous computation.
It has no object bindings, external services, resources or side effects.

All three tool parameters are required finite numbers. `minimum` must be no
greater than `maximum`. The JSON result contains `value` and `clamped`.
This package implements its own adapter and prompt projection through the public
`PureToolBindingAdapter` API, independently of the text metrics example.
