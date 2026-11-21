// Lets plain Node import the Worker's `import x from "./x.json"` (wrangler bundles this natively).
export async function load(url, ctx, next) {
  if (url.endsWith(".json")) { const r = await next(url, { ...ctx, importAttributes: { type: "json" } }); return r; }
  return next(url, ctx);
}
