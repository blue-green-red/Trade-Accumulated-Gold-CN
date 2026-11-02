import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  build: { outDir: "dist", target: "es2022", sourcemap: false },
  server: { proxy: { "/api": "http://127.0.0.1:18080" } },
});
