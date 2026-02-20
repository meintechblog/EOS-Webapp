import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/status": "http://localhost:8080",
      "/stats": "http://localhost:8080",
      "/health": "http://localhost:8080",
      "/eos/set": "http://localhost:8080",
      "/eos/input": "http://localhost:8080",
      "/eos/param": "http://localhost:8080",
    },
  },
});
