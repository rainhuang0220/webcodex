import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { validDocxIdentity } from "../src/mcp-apps/docx-reader.mjs";

// The Host runs the generated bundle. This mounts the same reader source,
// with ZIP rendering replaced so a projection can be observed without a browser.
const source = readFileSync(new URL("../src/mcp-apps/docx-app.mjs", import.meta.url), "utf8")
  .replace(/^import .*;\r?\n/gm, "")
  .replace("export function mountDocxApp", "function mountDocxApp") + `
function readDocxDocument({ request }) {
  return request(0, 20000).then(value => { record.chunk = value; return new Uint8Array([80, 75, 3, 4]); });
}
function renderDocx() { return Promise.resolve(1); }
`;
const flush = () => new Promise(resolve => setImmediate(resolve));
const sha = "cd".repeat(32);
const docxDocument = {
  project: "agent:docx:demo",
  path: "reports/brief.docx",
  name: "brief.docx",
  sha256: sha,
  bytes: 128,
};
const full = { success: true, output: { docx_document: docxDocument } };
const page = {
  project: docxDocument.project, path: docxDocument.path, sha256: docxDocument.sha256,
  bytes_total: docxDocument.bytes, byte_offset: 0, next_byte_offset: null, complete: true,
};

function reader() {
  const nodes = {}, sent = [], listeners = new Map(), record = {};
  const node = () => ({
    textContent: "", hidden: false, title: "", disabled: false, dataset: {}, children: [], childNodes: [],
    style: {},
    replaceChildren(...children) { this.children = children; },
    querySelectorAll() { return []; },
  });
  const document = {
    documentElement: { dataset: {}, style: { setProperty() {} } },
    getElementById: id => nodes[id] ||= node(),
    createElement: () => node(),
  };
  const parent = { postMessage: message => { sent.push(message); } };
  const window = {
    parent, document,
    ResizeObserver: class { observe() {} disconnect() {} },
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name),
    setTimeout: () => 0,
    clearTimeout() {},
    btoa: value => Buffer.from(value, "binary").toString("base64"),
  };
  runInNewContext(source, { window, document, record, validDocxIdentity, Uint8Array, Promise });
  const deliver = message => listeners.get("message")({ source: parent, data: { jsonrpc: "2.0", ...message } });
  return {
    nodes, sent, record,
    chunks: () => sent.filter(message => message.method === "tools/call"),
    present(value) {
      let error = null;
      try { deliver({ method: "ui/notifications/tool-result", params: value }); }
      catch (caught) { error = caught; }
      return error;
    },
    reply(request, value) { deliver({ id: request.id, result: value }); },
  };
}

test("a canonical structured DOCX envelope still opens the document", () => {
  const view = reader();
  const error = view.present({ structuredContent: full });
  assert.equal(error, null);
  assert.equal(view.chunks().length, 1);
  assert.equal(view.chunks()[0].params.name, "read_app_artifact_chunk");
  assert.equal(view.chunks()[0].params.arguments.path, docxDocument.path);
  assert.equal(view.chunks()[0].params.arguments.sha256, sha);
  assert.equal(view.chunks()[0].params.arguments.bytes, docxDocument.bytes);
  assert.equal(view.nodes.filename.textContent, docxDocument.name);
  assert.doesNotMatch(view.nodes.status.textContent, /unavailable/);
});

test("standard text opens a DOCX when the host omits structured content and private metadata", () => {
  const view = reader();
  const error = view.present({ content: [{ type: "text", text: JSON.stringify(full) }] });
  assert.equal(error, null);
  assert.equal(view.chunks().length, 1);
  assert.equal(view.chunks()[0].params.arguments.path, docxDocument.path);
});

test("a non-canonical host structuredContent does not mask the private DOCX envelope", () => {
  const view = reader();
  const error = view.present({
    structuredContent: { projected: true },
    _meta: { "webcodex/docxDocument": full },
  });
  assert.equal(error, null);
  assert.equal(view.chunks().length, 1);
  assert.equal(view.chunks()[0].params.arguments.path, docxDocument.path);
  assert.equal(view.chunks()[0].params.arguments.sha256, sha);
  assert.equal(view.chunks()[0].params.arguments.bytes, docxDocument.bytes);
  assert.equal(view.nodes.filename.textContent, docxDocument.name);
  assert.doesNotMatch(view.nodes.status.textContent, /unavailable/);
});

test("a canonical host projection does not mask the private DOCX envelope", () => {
  const view = reader();
  const error = view.present({
    structuredContent: { success: true, output: { name: "short.docx" } },
    content: { type: "text" },
    _meta: { "webcodex/docxDocument": full },
  });
  assert.equal(error, null);
  assert.equal(view.chunks().length, 1);
  assert.equal(view.chunks()[0].params.arguments.path, docxDocument.path);
  assert.equal(view.chunks()[0].params.arguments.sha256, sha);
  assert.doesNotMatch(view.nodes.status.textContent, /unavailable/);
});

for (const key of ["result", "toolResult", "tool_result"]) {
  test(`nested ${key} server text outranks outer canonical DOCX projection`, () => {
    const view = reader();
    const error = view.present({
      structuredContent: { success: true, output: { name: "short.docx" } },
      [key]: { content: [{ type: "text", text: JSON.stringify(full) }] },
    });
    assert.equal(error, null);
    assert.equal(view.chunks().length, 1);
    assert.equal(view.chunks()[0].params.arguments.path, docxDocument.path);
  });
}

test("nested DOCX server failure outranks outer Host success", () => {
  const view = reader();
  const error = view.present({
    structuredContent: full,
    toolResult: { content: [{ type: "text", text: JSON.stringify({ success: false, error: "read denied" }) }] },
  });
  assert.equal(error, null);
  assert.equal(view.chunks().length, 0);
  assert.match(view.nodes.status.textContent, /unavailable/);
});

test("server text wins when a host projection drops the DOCX chunk offset", async () => {
  const view = reader();
  const error = view.present({ _meta: { "webcodex/docxDocument": full } });
  assert.equal(error, null);
  const request = view.chunks()[0];
  const canonical = { success: true, output: { artifact_chunk: page } };
  view.reply(request, {
    structuredContent: { success: true, output: { artifact_chunk: { ...page, byte_offset: 1 } } },
    content: [{ type: "text", text: JSON.stringify(canonical) }],
    _meta: { "webcodex/artifactChunk": { content_base64: "UEsDBA==" } },
  });
  await flush();
  assert.equal(view.record.chunk.page.byte_offset, 0);
  assert.equal(view.record.chunk.encoded, "UEsDBA==");
  assert.doesNotMatch(view.nodes.status.textContent, /unavailable|Invalid|changed/);
});
