import * as L from 'leaflet'

// Real bug found and fixed the hard way: a plain `window.L = L` statement placed BETWEEN two
// `import` lines in the same file does NOT run between them. ES module imports are hoisted --
// every import's module body executes first, in dependency order, before ANY of the importing
// file's own top-level code runs, no matter where that code is textually positioned relative to
// the imports. Confirmed directly against the real built bundle: that statement ended up ~100,000
// characters AFTER leaflet-draw's own initialization code, far too late to affect it.
//
// A separate FILE, imported as its own line, does not have this problem -- sibling `import`
// statements in one file genuinely do execute in the order they're written, so importing this
// module (whose only job is this one assignment) BEFORE `import 'leaflet-draw'` guarantees the
// assignment completes first. This is what makes leaflet-draw's legacy UMD fallback (which
// mutates a global `L` it expects to find) land on the SAME object this app's own
// `import * as L from 'leaflet'` calls read from.
;(window as unknown as { L: typeof L }).L = L
