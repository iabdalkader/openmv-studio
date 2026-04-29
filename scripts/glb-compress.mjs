#!/usr/bin/env node
// Apply Draco compression to a GLB model.
// Usage: node optimize-glb.mjs <input.glb> <output.glb>
//
// Requires: npx @gltf-transform/cli
import { execSync } from "child_process";

const [input, output] = process.argv.slice(2);
if (!input || !output) {
  console.error("Usage: node optimize-glb.mjs <input.glb> <output.glb>");
  process.exit(1);
}

execSync(`npx @gltf-transform/cli draco "${input}" "${output}"`, {
  stdio: "inherit",
});
