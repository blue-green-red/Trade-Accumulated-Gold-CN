import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";
export default defineConfig({
  root: fileURLToPath(new URL("./public-app", import.meta.url)),
  base: "/public/",
  publicDir: false,
  plugins: [vue()],
  build: {
    outDir: "../dist-public",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
  },
});
