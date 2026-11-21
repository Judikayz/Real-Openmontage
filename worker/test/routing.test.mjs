import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.js";
const env = { ASSETS: { fetch: async (req) => new Response("asset:" + new URL(req.url).pathname) } };
test("/ redirects to /app instead of 404", async () => {
  const r = await worker.fetch(new Request("https://x.example/"), env, {});
  assert.equal(r.status, 302); assert.equal(new URL(r.headers.get("location")).pathname, "/app");
});
test("non-API paths go to static assets", async () => {
  const r = await worker.fetch(new Request("https://x.example/app"), env, {});
  assert.equal(await r.text(), "asset:/app");
});
