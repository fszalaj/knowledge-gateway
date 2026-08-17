---
name: cordis-composability
description: Review plugins, skills, agent tools, hooks, feature flags, and other dynamically added or removed components for reversible effects and declared dependencies. Use before implementing, installing, disabling, uninstalling, or releasing composable extensions.
---

# Review composability with the Cordis lens

Use the two dimensions defined in [A Programming Paradigm for Spatiotemporal Composability](https://github.com/cordiverse/paper):

- **Temporal composability:** removing a component completely reverts the effects it introduced.
- **Spatial composability:** dependencies are declared and managed explicitly instead of being discovered through failure.

The source is an actively revised preprint. Check its latest revision before relying on formal definitions. This skill applies the paper as a design lens; it does not require adopting the Cordis runtime or framework.

## Procedure

1. Define the component boundary and lifecycle: install, enable, run, disable, uninstall, upgrade, partial failure, and recovery.
2. Inventory every effect: files, symlinks, registrations, hooks, listeners, processes, connections, timers, credentials, permissions, caches, generated state, and global configuration.
3. Pair every effect with its exact inverse. If no safe inverse exists, describe the change as permanent and require an explicit migration or rollback plan.
4. List every dependency with provider, version or capability, availability condition, permission, and failure behavior. Move assumptions from prose into a manifest, configuration, or runtime check where possible.
5. Verify ordering: dependencies must be available before activation and consumers must stop before a dependency is removed.
6. Test repeated install/uninstall and enable/disable cycles, interruption during each lifecycle phase, and removal after a failed install.
7. Inspect for residue and undeclared coupling. A successful happy path is not sufficient.

## Output

Return:

- component and lifecycle boundary;
- effect-to-inverse table;
- declared dependency table;
- failure, upgrade, and rollback behavior;
- residue or hidden-coupling findings;
- GO/NO-GO verdict with required corrections.
