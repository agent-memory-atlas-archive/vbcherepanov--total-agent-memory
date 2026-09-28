# Third-party licenses

total-agent-memory is licensed under the terms in `LICENSE`. The files below are
third-party works shipped unmodified inside the package. Each directory keeps the
upstream license text next to the files it covers.

## Local dashboard (`src/dashboard_static/vendor/`)

Served same-origin by `src/dashboard.py` at `/static/vendor/...`; the dashboard
loads no scripts from a CDN. Files are byte-identical to the npm registry
tarballs (integrity verified against the registry's `sha512` checksum).

| Package | Version | File | License | License file |
|---|---|---|---|---|
| [vis-network](https://github.com/visjs/vis-network) | 9.1.6 | `vis-network-9.1.6/vis-network.min.js` | Apache-2.0 OR MIT | `LICENSE-APACHE-2.0.txt`, `LICENSE-MIT.txt` |
| [three.js](https://github.com/mrdoob/three.js) | 0.155.0 | `three-0.155.0/three.min.js` | MIT | `LICENSE.txt` |
| [3d-force-graph](https://github.com/vasturiano/3d-force-graph) | 1.73.0 | `3d-force-graph-1.73.0/3d-force-graph.min.js` | MIT | `LICENSE.txt` |
| [D3](https://github.com/d3/d3) | 7.9.0 | `d3-7.9.0/d3.min.js` | ISC | `LICENSE.txt` |

The `vis-network` standalone build bundles its runtime dependencies (including
core-js, MIT, and uuid, MIT); their notices are preserved in the file header. The
`3d-force-graph` UMD build bundles its npm dependencies (`three-forcegraph`,
`three-render-objects`, `kapsule`, `accessor-fn` and their transitive
dependencies, published under MIT, ISC or BSD-3-Clause), as distributed by the
upstream package.

## Team dashboard fonts (`src/team_memory/static/fonts/`)

| Font | License | License file |
|---|---|---|
| [Inter](https://github.com/rsms/inter) | SIL Open Font License 1.1 | `inter-license.txt` |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | SIL Open Font License 1.1 | `jetbrains-mono-license.txt` |
