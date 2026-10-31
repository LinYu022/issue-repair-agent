import { build } from "esbuild";
import { mkdir, copyFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
process.chdir(fileURLToPath(new URL(".", import.meta.url)));
await mkdir("dist", { recursive: true });
await build({
  absWorkingDir: process.cwd(),
  entryPoints: ["src/main.jsx"],
  bundle: true,
  minify: true,
  outfile: "dist/app.js",
  loader: { ".js": "jsx" },
  legalComments: "linked",
});
await copyFile("index.html", "dist/index.html");
